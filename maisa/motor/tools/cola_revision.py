#!/usr/bin/env python3
"""Recorta la cola de revision humana **sin decidir ningun pago**.

La cola de revision son las facturas que la norma manda a `ESCALAR`. En el
corpus de 500 hay 43; de esas, las que se escalan porque el OCR no consiguio
leer un identificador son las unicas donde una segunda lectura puede aportar
algo (las demas traen capa de texto y se escalan por otros motivos: pedido
repetido, anomalia, pago duplicado...).

Este comando **no toca `outcomes.jsonl`**. La decision de la entrega la sigue
tomando `norma.Decisor` con la lectura local; lo que hace es escribir un
sidecar, `outcomes_cola.jsonl`, con la evidencia de la segunda lectura para las
escaladas que son probablemente un falso positivo:

    {"file_id": "scan_017.pdf", "result": "ESCALAR",
     "segunda_lectura": {"confirmable": true, "desvio": false, "campos": {...},
                         "motivos": [...]}}

`confirmable: true` significa «el local no leia un identificador, la nube lo
trae y el maestro lo confirma»: el revisor puede cerrar la incidencia sin
abrirla, con la evidencia delante. `desvio: true` es lo contrario y **nunca** es
confirmable: el documento trae un IBAN que no es el del proveedor de su pedido,
que es justo la senal de fraude; el recorte no puede taparla.

    cd maisa && PYTHONPATH=src ../.venv/bin/python tools/cola_revision.py
    cd maisa && PYTHONPATH=src ../.venv/bin/python tools/cola_revision.py --forzar

Sin `--forzar` reutiliza las lecturas de nube ya pagadas por
`tools/bench_motores.py` (`.cache/motores/<sha>.cloud.json`) y, si no estan, las
pide y las guarda en `.cache/segunda/`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

from maisa import emit, lectura, procesa as P, segunda_lectura as S, texto  # noqa: E402

CACHE = RAIZ / ".cache" / "segunda"
#: La cache del banco de pruebas: si ya se pago la lectura de nube, se reutiliza.
CACHE_BENCH = RAIZ / ".cache" / "motores"
#: Escalones en los que el OCR es quien lee el documento (incluido el que sale
#: de la cache). En `capa_texto` la escalada viene de otra regla y una segunda
#: lectura no aporta nada.
ESCALONES_OCR = ("vision_ocr", "vision_nube", "cache_ocr", "degradado")


def _texto_de_bench(sha: str) -> str | None:
    ruta = CACHE_BENCH / f"{sha}.cloud.json"
    if not ruta.exists():
        return None
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    return datos.get("texto") or None


def segunda_lectura(pdf: Path, paginas: int, forzar: bool) -> texto.Lectura | None:
    """La lectura de la nube sola, cacheada. `None` si la nube no responde."""
    sha = lectura.sha256_pdf(pdf)
    ruta = CACHE / f"{sha}.cloud.json"
    if ruta.exists() and not forzar:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        if datos.get("texto"):
            return texto.extrae(datos["texto"], pdf.name, paginas, "vision_ocr")

    crudo = _texto_de_bench(sha) if not forzar else None
    if crudo is None:
        crudo = lectura.ocr_contenedor(pdf, motor="cloud")
        if not crudo:
            return None
        CACHE.mkdir(parents=True, exist_ok=True)
        ruta.write_text(
            json.dumps({"sha256": sha, "file_id": pdf.name, "texto": crudo},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    return texto.extrae(crudo, pdf.name, paginas, "vision_ocr")


def recorta(
    facturas: Path,
    xlsx: Path,
    config: Path,
    snapshot: Path | None,
    erp_url: str | None,
    salida: Path,
    trabajadores: int,
    forzar: bool = False,
) -> tuple[list[dict], dict]:
    """Devuelve las lineas del sidecar y los contadores del informe."""
    decisor, maestro, _ = P.construye_decisor(xlsx, config, snapshot, erp_url)
    pdfs = sorted(facturas.glob("*.pdf"), key=lambda p: emit.clave_orden(p.name))
    por_nombre = {p.name: p for p in pdfs}
    docs = lectura.lee_lote(pdfs, trabajadores=trabajadores)
    # El decisor acumula estado (pedidos repetidos) y solo ve un documento: el
    # lote se lo declara antes de decidir, igual que en la entrega.
    P.marca_pedidos_repetidos(decisor, (doc.lectura for doc in docs))

    vocab = S.vocabulario(maestro)
    escaladas = []
    escaladas_total = 0
    for doc in docs:
        dec = decisor.decide(doc.lectura)
        if dec.resultado != "ESCALAR":
            continue
        escaladas_total += 1
        if doc.escalon in ESCALONES_OCR:
            escaladas.append((doc, dec))

    # Segunda lectura solo de las escaladas que el OCR leyo: son las unicas donde
    # puede aportar algo, y son 11 de las 500.
    segundas = {}
    for doc, _ in escaladas:
        segundas[doc.lectura.file_id] = segunda_lectura(
            por_nombre[doc.lectura.file_id], doc.paginas_ocr or 1, forzar
        )

    # La lectura hibrida se decide con un decisor **propio**, sobre el lote
    # hibrido entero: es lo que hace `tools/bench_motores.py`, y si aqui se
    # decidiera de otra forma la cola dejaria de reproducir la medicion.
    hibridas = {}
    for doc, _ in escaladas:
        nube = segundas[doc.lectura.file_id]
        if nube is None:
            continue
        hibrida, _ = S.rellena_identificadores(doc.lectura, nube, maestro, vocab)
        hibridas[doc.lectura.file_id] = hibrida
    decisor_h, _, _ = P.construye_decisor(xlsx, config, snapshot, erp_url)
    P.marca_pedidos_repetidos(decisor_h, hibridas.values())
    resultados_h = {fid: decisor_h.decide(leida).resultado
                    for fid, leida in hibridas.items()}

    filas: list[dict] = []
    contador = {"escaladas": escaladas_total, "con_ocr": len(escaladas),
                "confirmables": 0, "desvios": 0, "sigue_escalando": 0,
                "sin_evidencia": 0, "sin_segunda_lectura": 0}

    for doc, dec in escaladas:
        file_id = doc.lectura.file_id
        nube = segundas[file_id]
        if nube is None:
            contador["sin_segunda_lectura"] += 1
            continue

        evidencia = S.evidencia(
            doc.lectura, nube, maestro, vocab, resultado_hibrido=resultados_h.get(file_id)
        )
        if evidencia is None:
            contador["sin_evidencia"] += 1
            continue
        if evidencia["desvio"]:
            contador["desvios"] += 1
        elif evidencia["confirmable"]:
            contador["confirmables"] += 1
        else:
            contador["sigue_escalando"] += 1
        filas.append({
            "file_id": file_id,
            "result": "ESCALAR",
            "escalon": doc.escalon,
            "motivos_decision": dec.motivos,
            "resultado_hibrido": resultados_h.get(file_id),
            "segunda_lectura": evidencia,
        })

    emit.escribe_jsonl([salida], filas)
    return filas, contador


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--facturas", type=Path, default=P.FACTURAS_POR_DEFECTO)
    ap.add_argument("--xlsx", type=Path, default=P.XLSX_POR_DEFECTO)
    ap.add_argument("--config", type=Path, default=P.CONFIG_POR_DEFECTO)
    ap.add_argument("--snapshot", type=Path, default=P.SNAPSHOT_POR_DEFECTO)
    ap.add_argument("--erp-url", default=None)
    ap.add_argument("--salida", type=Path,
                    default=P.SALIDA_POR_DEFECTO.with_name("outcomes_cola.jsonl"))
    ap.add_argument("--trabajadores", type=int, default=8)
    ap.add_argument("--forzar", action="store_true",
                    help="rehace las lecturas de nube en vez de reutilizarlas")
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()

    t0 = time.monotonic()
    filas, contador = recorta(
        args.facturas, args.xlsx, args.config, args.snapshot, args.erp_url,
        args.salida, args.trabajadores, args.forzar,
    )

    print("=" * 78)
    print("COLA DE REVISION")
    print("=" * 78)
    print(f"  escaladas por la norma:            {contador['escaladas']}")
    print(f"    de ellas, leidas por OCR:        {contador['con_ocr']}"
          "   (el resto trae capa de texto: la segunda lectura no aplica)")
    print(f"      confirmables sin abrir:        {contador['confirmables']}")
    print(f"      desvio de pago (NO recortar):  {contador['desvios']}")
    print(f"      aporta pero sigue escalando:   {contador['sigue_escalando']}")
    print(f"      sin evidencia que aportar:     {contador['sin_evidencia']}")
    print(f"      la nube no respondio:          {contador['sin_segunda_lectura']}")
    print()
    print(f"  cola a revisar a mano: {contador['escaladas']} -> "
          f"{contador['escaladas'] - contador['confirmables']}")
    print(f"  sidecar: {args.salida}  ({len(filas)} lineas, "
          f"{time.monotonic() - t0:.1f} s)")

    if filas:
        print()
        print("=" * 78)
        print("EVIDENCIA POR FACTURA")
        print("=" * 78)
        for fila in filas:
            rev = fila["segunda_lectura"]
            if rev["desvio"]:
                marca = "DESVIO "
            elif rev["confirmable"]:
                marca = "OK     "
            else:
                marca = "REVISA "
            print(f"  {marca}{fila['file_id']}  ({fila['escalon']}, "
                  f"hibrido={fila['resultado_hibrido']})")
            for motivo in rev["motivos"]:
                print(f"           {motivo}")
            for campo, valores in rev["campos"].items():
                print(f"           {campo}: {', '.join(valores)}")

    if args.json:
        args.json.write_text(
            json.dumps({"contadores": contador, "filas": filas}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\ninforme crudo: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
