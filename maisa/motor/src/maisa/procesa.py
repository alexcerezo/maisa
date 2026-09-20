"""Proceso end-to-end: 500 facturas -> `outcomes.jsonl`.

Uso:
    python -m maisa.procesa --salida /tmp/out/outcomes.jsonl
    python -m maisa.procesa --verifica /tmp/out/outcomes.jsonl
    python -m maisa.procesa --traza-hash   # anade la traza encadenada (pitch)

`outcomes.jsonl` (la entrega) es exactamente el mismo con o sin `--traza-hash`:
la traza encadenada es material de auditoria, no de entrega.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from collections.abc import Iterable
from pathlib import Path

from . import emit, excel, lectura, norma, trace
from .erp import carga_snapshot

RAIZ = Path(__file__).resolve().parents[2]


def _primero(*candidatos: Path) -> Path:
    """Primer candidato que existe; si no existe ninguno, el primero.

    El motor vive en dos arboles distintos (el repositorio de entrega, con los
    datos en ``data/``, y el arbol de desarrollo, con el corpus en
    ``corpus/maisa/``). Resolver por existencia evita tener que pasar rutas
    absolutas en cada invocacion sin fijar una sola ubicacion.
    """
    for candidato in candidatos:
        if candidato.exists():
            return candidato
    return candidatos[0]


_CORPUS = (RAIZ.parent / "data", RAIZ.parent / "corpus" / "maisa")
XLSX_POR_DEFECTO = _primero(*(d / "FINAL_v7_DEFINITIVO_ahorasi.xlsx" for d in _CORPUS))
FACTURAS_POR_DEFECTO = _primero(*(d / "facturas" for d in _CORPUS))
CONFIG_POR_DEFECTO = RAIZ / "config/reglas.toml"
SNAPSHOT_POR_DEFECTO = _primero(*(d / "erp_snapshot.json" for d in _CORPUS),
                                Path("/tmp/asientos.json"))
SALIDA_POR_DEFECTO = _primero(RAIZ.parent / "outputs", Path("/tmp/out")) / "outcomes.jsonl"


def _percentiles_ms(segundos: list[float]) -> dict:
    """p50/p95 en milisegundos por rango mas cercano (sin dependencias extra)."""
    if not segundos:
        return {"p50": 0.0, "p95": 0.0}
    ordenados = sorted(segundos)
    ultimo = len(ordenados) - 1
    return {
        "p50": round(ordenados[round(0.50 * ultimo)] * 1000, 1),
        "p95": round(ordenados[round(0.95 * ultimo)] * 1000, 1),
    }


def construye_decisor(
    xlsx: Path, config: Path, snapshot: Path | None, erp_url: str | None,
    metricas_erp: dict | None = None,
):
    # La norma se valida lo primero, antes de leer el maestro y antes de tocar la
    # red: una config invalida tiene que costar milisegundos, no un Excel abierto
    # ni un login al ERP que luego se tira a la basura.
    pol = norma.Politica.carga(config)
    maestro = excel.carga(xlsx)
    if snapshot and Path(snapshot).exists():
        asientos = {a.pedido: a for a in carga_snapshot(Path(snapshot))}
    else:
        from .erp import ERP

        cliente = ERP(base=erp_url or "http://127.0.0.1:8009")
        cliente.login()
        asientos = {a.pedido: a for a in cliente.asientos()}
        if metricas_erp is not None:
            metricas_erp.update(cliente.metricas.como_dict())
    return norma.Decisor(maestro, asientos, pol), maestro, asientos


def marca_pedidos_repetidos(
    decisor: norma.Decisor, lecturas: Iterable[norma.Lectura]
) -> None:
    """Declara al decisor los pedidos que aparecen en mas de una factura.

    Duplicidad de pedido dentro del lote (Norma, punto 5): hace falta ver el
    lote entero, y el decisor solo ve un documento, asi que se lo declaramos
    antes de decidir. Se cuenta sobre el pedido ya resuelto (no sobre el texto),
    que es el unico que identifica un pago.

    Vive aqui, y no dentro de `procesa`, porque cualquier cosa que decida el
    lote entero tiene que aplicarlo: si el simulador se lo salta, su reparto de
    partida no es el de la entrega.
    """
    pedidos: dict[str, list[str]] = collections.defaultdict(list)
    for leida in lecturas:
        pedido = decisor.pedido_de(leida)
        if pedido:
            pedidos[pedido].append(leida.file_id)
    for pedido, ficheros in pedidos.items():
        if len(ficheros) > 1:
            decisor.marca_pedido_repetido(pedido, *sorted(ficheros))


def procesa(
    facturas: Path,
    xlsx: Path,
    config: Path,
    snapshot: Path | None,
    erp_url: str | None,
    salida: Path,
    trabajadores: int,
    lote: int,
    traza_hash: bool = False,
) -> list[dict]:
    metricas_erp: dict = {}
    decisor, maestro, asientos = construye_decisor(xlsx, config, snapshot, erp_url, metricas_erp)
    # Orden explicito por nombre y no `sorted(...)` sobre `Path`: `PurePath`
    # pliega mayusculas en Windows y no en POSIX, lo que reordenaba la entrega
    # segun el sistema. Ver `emit.clave_orden`.
    pdfs = sorted(facturas.glob("*.pdf"), key=lambda p: emit.clave_orden(p.name))
    ruta_traza = salida.with_name(salida.stem + "_traza.jsonl")
    # El registro encadena cada evento con el hash del anterior y hace flush
    # linea a linea: si el proceso muere a mitad, el log queda truncado y
    # `trace.verifica` lo dice en vez de darlo por bueno.
    registro = trace.Registro(ruta_traza) if traza_hash else None
    if registro is not None:
        registro.anota(
            trace.TIPO_LOTE, lote=lote, facturas=len(pdfs), xlsx=str(xlsx),
            config=str(config), snapshot=str(snapshot) if snapshot else None,
            erp_url=erp_url, version_norma=decisor.pol.version,
            trabajadores=trabajadores,
        )
    t0 = time.monotonic()
    docs = lectura.lee_lote(pdfs, trabajadores=trabajadores)

    # Duplicidad de pedido dentro del lote (Norma, punto 5). El decisor solo ve
    # un documento: el lote se lo declara antes de decidir.
    marca_pedidos_repetidos(decisor, (doc.lectura for doc in docs))

    filas: list[dict] = []
    traza: list[dict] = []
    contador: collections.Counter = collections.Counter()
    escalones: collections.Counter = collections.Counter()
    latencias_decision: list[float] = []
    for doc in docs:
        inicio_decision = time.perf_counter()
        dec = decisor.decide(doc.lectura)
        latencias_decision.append(time.perf_counter() - inicio_decision)
        # La entrega lleva solo file_id + result; los motivos y los hechos van
        # a la traza (abajo), que es donde se explica la decision.
        filas.append(emit.linea(doc.lectura.file_id, dec.resultado))
        contador[dec.resultado] += 1
        escalones[doc.escalon] += 1
        entrada = dec.como_dict()
        entrada.update(
            lote=lote,
            sha256=doc.sha256,
            escalon_lectura=doc.escalon,
            calidad_lectura=round(doc.calidad, 4),
            segundos_lectura=round(doc.segundos, 3),
            sospechosos=doc.lectura.sospechosos,
            sospechosos_meta=doc.lectura.sospechosos_meta,
            nota_documento=doc.lectura.nota,
        )
        traza.append(entrada)
        if registro is not None:
            file_id = doc.lectura.file_id
            registro.anota(
                trace.TIPO_LECTURA, file_id, sha256=doc.sha256, escalon=doc.escalon,
                cache=doc.cache, calidad=round(doc.calidad, 4),
                segundos=round(doc.segundos, 3), metodo_lectura=dec.metodo_lectura,
                sospechosos=doc.lectura.sospechosos,
                sospechosos_meta=doc.lectura.sospechosos_meta,
            )
            registro.anota(
                trace.TIPO_DECISION, file_id,
                **{k: v for k, v in entrada.items() if k != "file_id"},
            )

    ruta = emit.escribe_jsonl([salida], filas)
    problemas = emit.valida_jsonl(ruta, pdfs)
    problemas_traza: list[trace.Problema] = []
    if registro is not None:
        registro.anota(
            trace.TIPO_FIN, resultados=dict(contador), escalones=dict(escalones),
            segundos=round(time.monotonic() - t0, 3), sello_previo=registro.sello(),
            erp=metricas_erp or None,
            latencia_lectura_ms=_percentiles_ms([doc.segundos for doc in docs]),
            latencia_decision_ms=_percentiles_ms(latencias_decision),
        )
        registro.cierra()
        problemas_traza = trace.verifica(ruta_traza)
    else:
        ruta_traza.write_text(
            "\n".join(json.dumps(t, ensure_ascii=False) for t in traza) + "\n", encoding="utf-8"
        )
    print(f"facturas   : {len(pdfs)}")
    print(f"resultado  : {dict(contador)}")
    print(f"lectura    : {dict(escalones)}")
    print(f"maestro    : {len(maestro.proveedores)} proveedores, {len(maestro.pedidos)} pedidos")
    print(f"erp        : {len(asientos)} asientos" + (f", metricas {metricas_erp}" if metricas_erp else ""))
    repetidos = decisor.pedidos_repetidos()
    print(f"duplicados : {len(repetidos)} pedidos en mas de una factura del lote"
          + (f" ({', '.join(repetidos)})" if repetidos else ""))
    lat_lectura = _percentiles_ms([doc.segundos for doc in docs])
    lat_decision = _percentiles_ms(latencias_decision)
    print(
        f"latencia   : lectura p50={lat_lectura['p50']}ms p95={lat_lectura['p95']}ms"
        f" | decision p50={lat_decision['p50']}ms p95={lat_decision['p95']}ms"
    )
    print(f"salida     : {ruta}  (+ {ruta_traza.name})")
    print(f"validacion : {'OK' if not problemas else problemas}")
    if registro is not None:
        print(f"traza hash : {len(registro)} eventos, "
              f"{'OK' if not problemas_traza else problemas_traza}")
        print(f"sello      : {trace.sello(ruta_traza)}")
    print(f"tiempo     : {time.monotonic() - t0:.1f}s")
    return filas


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Decide las 500 facturas de Maisa.")
    p.add_argument("--facturas", type=Path, default=FACTURAS_POR_DEFECTO)
    p.add_argument("--xlsx", type=Path, default=XLSX_POR_DEFECTO)
    p.add_argument("--config", type=Path, default=CONFIG_POR_DEFECTO)
    p.add_argument("--snapshot", type=Path, default=SNAPSHOT_POR_DEFECTO)
    p.add_argument("--erp-url", default=None)
    p.add_argument("--salida", type=Path, default=SALIDA_POR_DEFECTO)
    p.add_argument("--lote", type=int, default=1)
    p.add_argument("--trabajadores", type=int, default=4)
    p.add_argument(
        "--traza-hash", action="store_true",
        help="escribe la traza encadenada por hash (auditoria del pitch; no cambia la entrega)",
    )
    p.add_argument("--verifica", type=Path, default=None)
    args = p.parse_args(argv)

    if args.verifica:
        pdfs = sorted(args.facturas.glob("*.pdf"))
        problemas = emit.valida_jsonl(args.verifica, pdfs)
        print("OK" if not problemas else "\n".join(problemas))
        return 0 if not problemas else 1

    try:
        procesa(
            args.facturas, args.xlsx, args.config, args.snapshot, args.erp_url,
            args.salida, args.trabajadores, args.lote, traza_hash=args.traza_hash,
        )
    except norma.ConfigInvalida as exc:
        print(f"\nCONFIG RECHAZADA: no se decide nada con este fichero.\n{exc}",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
