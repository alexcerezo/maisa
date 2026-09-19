#!/usr/bin/env python3
"""Banco de pruebas "de oro" sobre las 500 facturas.

Congela la decision de cada factura en un fichero de referencia y despues avisa
si algo cambia. Sirve para lo que un test unitario no cubre: **enterarse de que
una regla nueva ha movido facturas que no debia mover**.

    # congelar el estado actual (una vez, o cuando el cambio sea el deseado)
    PYTHONPATH=src .venv/bin/python maisa/tools/oro.py --fijar

    # comprobar que nada se ha movido (lo que se corre en cada cambio)
    PYTHONPATH=src .venv/bin/python maisa/tools/oro.py --comprobar

    # que regla salta en cada factura (cobertura de la norma)
    PYTHONPATH=src .venv/bin/python maisa/tools/oro.py --cobertura

    # "y si...?": ensayar un cambio de dato o de norma SIN escribir nada
    PYTHONPATH=src .venv/bin/python maisa/tools/oro.py --simula regla tolerancia_importe=0.05
    PYTHONPATH=src .venv/bin/python maisa/tools/oro.py --simula erp PO-2026-0814=PAGADA
    PYTHONPATH=src .venv/bin/python maisa/tools/oro.py --factura factura_5518.pdf \
        --simula factura total=99.99

Exit 0 si coincide, 1 si hay deriva. `--fijar` siempre exit 0. `--simula` es una
consulta: siempre exit 0, aunque mueva facturas (eso es justo lo que se pregunta).
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sys
import tempfile
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

from maisa import lectura, norma, texto  # noqa: E402
from maisa import procesa as P  # noqa: E402

ORO = RAIZ / "tests" / "oro"
REFERENCIA = ORO / "outcomes_oro.jsonl"
META = ORO / "oro_meta.json"


def _ejecuta(salida: Path, facturas: Path, xlsx: Path, config: Path,
             snapshot: Path, lote: int, trabajadores: int, traza: bool) -> Path:
    """Corre el lote entero y devuelve la ruta del JSONL de entrega."""
    P.procesa(facturas, xlsx, config, snapshot, None, salida, trabajadores, lote,
              traza_hash=traza)
    return salida


def _lee_jsonl(ruta: Path) -> dict[str, str]:
    """``file_id -> result``, conservando el orden de aparicion."""
    filas: dict[str, str] = {}
    for num, linea in enumerate(ruta.read_text(encoding="utf-8").splitlines(), 1):
        linea = linea.strip()
        if not linea:
            continue
        obj = json.loads(linea)
        if not isinstance(obj, dict) or "file_id" not in obj or "result" not in obj:
            raise SystemExit(f"{ruta}:{num}: linea sin file_id/result")
        filas[str(obj["file_id"])] = str(obj["result"])
    return filas


def _reparto(filas: dict[str, str]) -> dict[str, int]:
    return dict(sorted(collections.Counter(filas.values()).items()))


def _huella(filas: dict[str, str]) -> str:
    """Hash estable del mapa completo: el resumen de una linea del estado."""
    blob = json.dumps(sorted(filas.items()), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _cobertura(ruta_traza: Path) -> dict[str, dict[str, int]]:
    """Por regla y **por factura**: en cuantas pasa, en cuantas suspende, en
    cuantas el hecho es duro y en cuantas es solo informativo. Se cuenta la
    factura una vez por regla, no el numero de hechos: `R1_identidad` emite dos
    hechos por factura (NIF e IBAN) y contarlos sueltos infla la tabla."""
    por_regla: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for linea in ruta_traza.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea:
            continue
        evento = json.loads(linea)
        if evento.get("tipo") != "decision":
            continue
        hechos: dict[str, list[dict]] = collections.defaultdict(list)
        for hecho in evento.get("datos", {}).get("hechos", []):
            hechos[hecho.get("regla") or "?"].append(hecho)
        for regla, grupo in hechos.items():
            if any(h.get("informativo") for h in grupo):
                por_regla[regla]["informativo"] += 1
            elif any(not h.get("ok") for h in grupo):
                por_regla[regla]["falla"] += 1
                if any(h.get("duro") for h in grupo):
                    por_regla[regla]["duro"] += 1
            else:
                por_regla[regla]["ok"] += 1
    return {k: dict(v) for k, v in sorted(por_regla.items())}


def fijar(args: argparse.Namespace) -> int:
    ORO.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        salida = _ejecuta(Path(tmp) / "outcomes.jsonl", args.facturas, args.xlsx,
                          args.config, args.snapshot, args.lote, args.trabajadores,
                          True)
        filas = _lee_jsonl(salida)
        ORO.joinpath("outcomes_oro.jsonl").write_text(
            salida.read_text(encoding="utf-8"), encoding="utf-8"
        )
        traza = salida.with_name("outcomes_traza.jsonl")
        META.write_text(json.dumps({
            "facturas": len(filas),
            "reparto": _reparto(filas),
            "huella": _huella(filas),
            "cobertura_reglas": _cobertura(traza),
        }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"referencia congelada: {REFERENCIA}")
    print(f"facturas : {len(filas)}")
    print(f"reparto  : {_reparto(filas)}")
    print(f"huella   : {_huella(filas)}")
    return 0


def comprobar(args: argparse.Namespace) -> int:
    if not REFERENCIA.exists():
        raise SystemExit(f"no hay referencia en {REFERENCIA}; corre antes --fijar")
    esperado = _lee_jsonl(REFERENCIA)
    with tempfile.TemporaryDirectory() as tmp:
        salida = _ejecuta(Path(tmp) / "outcomes.jsonl", args.facturas, args.xlsx,
                          args.config, args.snapshot, args.lote, args.trabajadores,
                          False)
        obtenido = _lee_jsonl(salida)

    deriva: list[str] = []
    for fid in sorted(set(esperado) | set(obtenido)):
        antes, ahora = esperado.get(fid), obtenido.get(fid)
        if antes != ahora:
            deriva.append(f"  {fid}: {antes} -> {ahora}")

    print(f"referencia : {len(esperado)} facturas, huella {_huella(esperado)[:12]}...")
    print(f"actual     : {len(obtenido)} facturas, huella {_huella(obtenido)[:12]}...")
    if not deriva:
        print(f"reparto    : {_reparto(obtenido)}")
        print("deriva     : NINGUNA")
        return 0
    print(f"deriva     : {len(deriva)} facturas")
    for linea in deriva[:40]:
        print(linea)
    if len(deriva) > 40:
        print(f"  ... y {len(deriva) - 40} mas")
    print(f"reparto    : {_reparto(obtenido)}")
    return 1


def cobertura(args: argparse.Namespace) -> int:
    if not META.exists():
        raise SystemExit(f"no hay metadatos en {META}; corre antes --fijar")
    meta = json.loads(META.read_text(encoding="utf-8"))
    tabla = meta.get("cobertura_reglas", {})
    print(f"{'regla':<20}{'pasa':>6}{'suspende':>10}{'duros':>7}{'inform.':>9}")
    print("-" * 52)
    for regla, cuenta in tabla.items():
        print(f"{regla:<20}{cuenta.get('ok', 0):>6}{cuenta.get('falla', 0):>10}"
              f"{cuenta.get('duro', 0):>7}{cuenta.get('informativo', 0):>9}")
    print("-" * 52)
    print(f"facturas   : {meta.get('facturas')}")
    print(f"reparto    : {meta.get('reparto')}")
    print(f"huella     : {meta.get('huella')}")
    return 0


# --------------------------------------------------------------------------
# --simula: el simulador de cambios ("y si...?")
#
# Responde a "que pasaria con esta factura si cambiara el dato X" ensayando el
# cambio sobre las ENTRADAS (maestro, asientos del ERP, politica) y volviendo a
# decidir con la MISMA lectura ya hecha. No relee PDFs ni llama al OCR: lo que
# se ensaya es la norma y los datos, no la extraccion. Es reversible y no
# escribe nada en disco.
# --------------------------------------------------------------------------

AMBITOS = ("regla", "erp", "maestro", "factura")

# clave en la politica -> (atributo del dataclass, conversion)
ESCALARES_POLITICA = {
    "tolerancia_importe": ("tolerancia", Decimal),
    "similitud_minima_nif": ("similitud_nif", float),
    "similitud_minima_iban": ("similitud_iban", float),
    "confianza_minima_campo": ("confianza_minima", float),
    "hoy": ("hoy", str),
    "version": ("version", str),
}
CAMPOS_ASIENTO = ("estado", "importe", "nif", "proveedor", "fecha")
CAMPOS_PROVEEDOR = ("iban", "nif", "razon_social", "ciudad", "condiciones")
CAMPOS_LECTURA = ("nif", "iban", "pedido", "fecha", "base", "iva", "total",
                  "num_factura")
ALIAS_LECTURA = {"importe": "total", "importe_total": "total"}
SENALES_LECTURA = ("texto_ilegible", "sospechosos", "metodo")
VERDADEROS = ("1", "true", "si", "s", "yes")


def _parsea_cambios(pares: list[str]) -> list[tuple[str, str]]:
    cambios: list[tuple[str, str]] = []
    for par in pares:
        if "=" not in par:
            raise SystemExit(f"cambio mal formado: {par!r}; se espera CLAVE=VALOR")
        clave, valor = par.split("=", 1)
        cambios.append((clave.strip(), valor.strip()))
    if not cambios:
        raise SystemExit("--simula necesita al menos un CLAVE=VALOR")
    return cambios


def _reindexa(maestro) -> None:
    """Rearma los indices derivados tras tocar el maestro."""
    por_nif: dict[str, list] = {}
    por_iban: dict[str, list] = {}
    for proveedor in maestro.proveedores.values():
        if proveedor.nif:
            por_nif.setdefault(proveedor.nif, []).append(proveedor)
        if proveedor.iban:
            por_iban.setdefault(proveedor.iban, []).append(proveedor)
    maestro.por_nif, maestro.por_iban = por_nif, por_iban


def _aplica_politica(pol, cambios) -> list[str]:
    hechos: list[str] = []
    for clave, valor in cambios:
        if clave == "calidad_texto_minima":
            raise SystemExit(
                "calidad_texto_minima vive en la ESCALERA DE LECTURA "
                "(lectura.lee), no en la capa de decision: cambiarlo obliga a "
                "releer los PDFs y el simulador no relee. Ver docs/simulador.md."
            )
        if clave in ESCALARES_POLITICA:
            atributo, conversion = ESCALARES_POLITICA[clave]
            try:
                nuevo = conversion(valor)
            except Exception as exc:  # valor no convertible
                raise SystemExit(f"regla {clave}={valor!r}: {exc}") from None
            setattr(pol, atributo, nuevo)
            hechos.append(f"politica.{atributo} = {nuevo}")
            continue
        partes = clave.split(".")
        if len(partes) == 2 and partes[0] == "precedencia":
            if partes[1] not in pol.precedencia:
                raise SystemExit(
                    f"precedencia: {partes[1]!r} no es un resultado conocido "
                    f"({', '.join(sorted(pol.precedencia))})"
                )
            pol.precedencia[partes[1]] = int(valor)
            hechos.append(f"precedencia.{partes[1]} = {int(valor)}")
            continue
        if len(partes) == 3 and partes[0] == "reglas":
            if valor not in pol.precedencia:
                raise SystemExit(
                    f"regla {clave}={valor!r}: {valor!r} no esta en "
                    f"[precedencia] ({', '.join(sorted(pol.precedencia))})"
                )
            pol.reglas.setdefault(partes[1], {})[partes[2]] = valor
            hechos.append(f"reglas.{partes[1]}.{partes[2]} = {valor}")
            continue
        if len(partes) == 2 and partes[0] == "hechos_duros":
            if valor not in pol.precedencia:
                raise SystemExit(
                    f"hecho duro {clave}={valor!r}: {valor!r} no esta en "
                    f"[precedencia] ({', '.join(sorted(pol.precedencia))})"
                )
            pol.hechos_duros[partes[1]] = valor
            hechos.append(f"hechos_duros.{partes[1]} = {valor}")
            continue
        raise SystemExit(
            f"regla: no se reconoce {clave!r}.\n"
            f"  escalares : {', '.join(sorted(ESCALARES_POLITICA))}\n"
            "  puntuales : reglas.<REGLA>.<clave>=<PAGAR|ESCALAR|NO_PAGAR>\n"
            "              hechos_duros.<NOMBRE>=<PAGAR|ESCALAR|NO_PAGAR>\n"
            "              precedencia.<PAGAR|ESCALAR|NO_PAGAR>=<entero>"
        )
    return hechos


def _aplica_erp(asientos, cambios) -> list[str]:
    hechos: list[str] = []
    for clave, valor in cambios:
        pedido, campo = clave.split(".", 1) if "." in clave else (clave, "estado")
        if campo not in CAMPOS_ASIENTO:
            raise SystemExit(
                f"erp: campo {campo!r} desconocido ({', '.join(CAMPOS_ASIENTO)})"
            )
        asiento = asientos.get(pedido)
        if asiento is None:
            raise SystemExit(
                f"erp: el pedido {pedido!r} no esta en el snapshot "
                f"({len(asientos)} asientos); usa --snapshot o revisa el codigo"
            )
        nuevo = Decimal(valor) if campo == "importe" else valor
        anterior = getattr(asiento, campo)
        setattr(asiento, campo, nuevo)
        hechos.append(f"asiento {pedido}.{campo}: {anterior} -> {nuevo}")
    return hechos


def _aplica_maestro(maestro, cambios) -> list[str]:
    hechos: list[str] = []
    for clave, valor in cambios:
        if "." not in clave:
            raise SystemExit("maestro: se espera PROVEEDOR_ID.CAMPO=VALOR")
        referencia, campo = clave.split(".", 1)
        if campo not in CAMPOS_PROVEEDOR:
            raise SystemExit(
                f"maestro: campo {campo!r} desconocido ({', '.join(CAMPOS_PROVEEDOR)})"
            )
        proveedor_id = referencia if referencia in maestro.proveedores else None
        if proveedor_id is None:
            for pid, proveedor in maestro.proveedores.items():
                if proveedor.nif == referencia:
                    proveedor_id = pid
                    break
        if proveedor_id is None:
            raise SystemExit(f"maestro: no hay proveedor {referencia!r} (ni por id ni por NIF)")
        anterior = getattr(maestro.proveedores[proveedor_id], campo)
        maestro.proveedores[proveedor_id] = replace(
            maestro.proveedores[proveedor_id], **{campo: valor}
        )
        hechos.append(f"proveedor {proveedor_id}.{campo}: {anterior!r} -> {valor!r}")
    _reindexa(maestro)
    return hechos


def _aplica_factura(lecturas, antes, asientos, file_id, cambios) -> list[str]:
    documento = lecturas.get(file_id)
    if documento is None:
        raise SystemExit(
            f"factura: {file_id!r} no esta en el lote ({len(lecturas)} facturas)"
        )
    leidos = documento.lectura
    hechos: list[str] = []
    for clave, valor in cambios:
        campo = ALIAS_LECTURA.get(clave, clave)
        if campo == "estado_erp":
            pedido = (antes[file_id].campos or {}).get("pedido")
            if not pedido:
                raise SystemExit(
                    f"factura: {file_id} no tiene pedido resuelto, no puedo "
                    "cambiar el estado del ERP; simula el pedido por su codigo"
                )
            asiento = asientos.get(pedido)
            if asiento is None:
                raise SystemExit(f"erp: el pedido {pedido!r} no esta en el snapshot")
            anterior = asiento.estado
            asiento.estado = valor
            hechos.append(f"asiento {pedido}.estado: {anterior} -> {valor}")
            continue
        if campo == "texto_ilegible":
            anterior = leidos.texto_ilegible
            leidos.texto_ilegible = valor.lower() in VERDADEROS
            hechos.append(f"{file_id}.texto_ilegible: {anterior} -> {leidos.texto_ilegible}")
            continue
        if campo == "sospechosos":
            leidos.sospechosos = [v for v in valor.split(",") if v]
            hechos.append(f"{file_id}.sospechosos = {leidos.sospechosos}")
            continue
        if campo == "metodo":
            leidos.metodo = valor
            hechos.append(f"{file_id}.metodo = {valor}")
            continue
        if campo not in CAMPOS_LECTURA:
            raise SystemExit(
                f"factura: campo {clave!r} desconocido.\n"
                f"  leidos   : {', '.join(CAMPOS_LECTURA)} (importe = total)\n"
                f"  senales  : {', '.join(SENALES_LECTURA)}\n"
                "  puente   : estado_erp=<estado> (toca el asiento del pedido)"
            )
        anterior = leidos.valores(campo)
        setattr(leidos, campo, [texto.Candidato(valor, "simulado", 1.0)])
        hechos.append(f"{file_id}.{campo}: {anterior} -> ['{valor}']")
    return hechos


def _firma_de_hecho(h) -> tuple:
    return (h.regla, h.ok, h.motivo, h.nombre, h.duro, h.informativo,
            json.dumps(h.datos, sort_keys=True, default=str))


def _firma_hechos(decision) -> list[tuple]:
    """Huella comparable de la traza de hechos de una decision.

    Incluye ``datos`` porque la traza no es solo "que se comprobo" sino "con
    que evidencia": un cambio en el asiento del ERP mueve el estado que
    sostiene el hecho aunque el texto del motivo no cambie.
    """
    return [_firma_de_hecho(h) for h in decision.hechos]


def _diff_hechos(antes, despues) -> tuple[list[tuple], list[tuple]]:
    firma_a, firma_b = _firma_hechos(antes), _firma_hechos(despues)
    quitados = [h for h in firma_a if h not in firma_b]
    puestos = [h for h in firma_b if h not in firma_a]
    return quitados, puestos


def _pinta_hecho(hecho: tuple, signo: str) -> str:
    regla, ok, motivo, nombre, duro, informativo, _datos = hecho
    etiqueta = regla + (f"/{nombre}" if nombre and nombre != regla else "")
    estado = "OK" if ok else "FALLA"
    marcas = []
    if duro:
        marcas.append("duro")
    if informativo:
        marcas.append("informativo")
    sufijo = f" [{', '.join(marcas)}]" if marcas else ""
    return f"      {signo} {etiqueta:<20} {estado:<6}{sufijo} {motivo}"


def _evidencia_cambiada(antes, despues) -> list[str]:
    """Datos que cambian en un hecho que sigue existiendo con el mismo motivo.

    Un cambio de estado en el ERP puede dejar el motivo intacto y mover solo la
    evidencia; sin esto el diff pareceria vacio.
    """
    por_clave: dict[tuple, dict] = {}
    for h in antes.hechos:
        por_clave.setdefault((h.regla, h.nombre), {})["antes"] = h
    for h in despues.hechos:
        por_clave.setdefault((h.regla, h.nombre), {})["ahora"] = h
    lineas: list[str] = []
    for (regla, nombre), par in por_clave.items():
        a, b = par.get("antes"), par.get("ahora")
        if a is None or b is None or a.datos == b.datos:
            continue
        etiqueta = regla + (f"/{nombre}" if nombre and nombre != regla else "")
        for clave in sorted(set(a.datos) | set(b.datos)):
            if a.datos.get(clave) == b.datos.get(clave):
                continue
            lineas.append(
                f"      ~ {etiqueta:<20} {clave}: {a.datos.get(clave)} -> {b.datos.get(clave)}"
            )
    return lineas


def _reparto_de(decisiones) -> dict[str, int]:
    cuenta: collections.Counter = collections.Counter(
        d.resultado for d in decisiones.values()
    )
    return {k: cuenta[k] for k in sorted(cuenta)}


def simula(args: argparse.Namespace) -> int:
    ambito, *resto = args.simula
    if ambito not in AMBITOS:
        raise SystemExit(
            f"ambito {ambito!r} desconocido; elige uno de {', '.join(AMBITOS)}"
        )
    cambios = _parsea_cambios(resto)
    if ambito == "factura" and not args.factura:
        raise SystemExit(
            "el ambito factura necesita --factura <file_id> "
            "(p.ej. --factura factura_5518.pdf --simula factura total=99.99)"
        )

    decisor, maestro, asientos = P.construye_decisor(
        args.xlsx, args.config, args.snapshot, None
    )
    rutas = sorted(args.facturas.glob("*.pdf"), key=lambda p: P.emit.clave_orden(p.name))
    documentos = lectura.lee_lote(rutas, trabajadores=args.trabajadores)
    lecturas = {d.lectura.file_id: d for d in documentos}
    # El lote entero decide la duplicidad de pedido: sin esto el simulador
    # arrancaria de un reparto que no es el de la entrega.
    P.marca_pedidos_repetidos(decisor, (d.lectura for d in documentos))
    antes = {fid: decisor.decide(doc.lectura) for fid, doc in lecturas.items()}

    aplicados: list[str] = []
    if ambito == "regla":
        aplicados = _aplica_politica(decisor.pol, cambios)
    elif ambito == "erp":
        aplicados = _aplica_erp(asientos, cambios)
    elif ambito == "maestro":
        aplicados = _aplica_maestro(maestro, cambios)
    else:
        aplicados = _aplica_factura(lecturas, antes, asientos, args.factura, cambios)

    nuevo = norma.Decisor(maestro, asientos, decisor.pol)
    # El cambio simulado puede mover el pedido de una factura (ambito erp), asi
    # que la duplicidad se vuelve a calcular sobre el estado nuevo.
    P.marca_pedidos_repetidos(nuevo, (d.lectura for d in documentos))
    despues = {fid: nuevo.decide(doc.lectura) for fid, doc in lecturas.items()}

    movidas = [fid for fid in lecturas if antes[fid].resultado != despues[fid].resultado]
    con_hechos = [fid for fid in lecturas
                  if _firma_hechos(antes[fid]) != _firma_hechos(despues[fid])]

    print(f"simulacion : {ambito} {' '.join(resto)}")
    print(f"lote       : {args.facturas} ({len(lecturas)} facturas, "
          f"lote {args.lote}, {args.trabajadores} trabajadores)")
    print(f"norma      : {decisor.pol.version} ({args.config})")
    print("cambios aplicados (en memoria, nada escrito en disco):")
    for linea in aplicados:
        print(f"  - {linea}")
    print(f"decision   : {_reparto_de(antes)}  ->  {_reparto_de(despues)}")
    print(f"movidas    : {len(movidas)} de {len(lecturas)} facturas cambian de resultado"
          f" | {len(con_hechos)} cambian su traza de hechos")

    if not movidas and not con_hechos:
        print("\nninguna factura cambia: el dato simulado no llega a ninguna regla.")
        return 0

    a_mostrar = [fid for fid in lecturas if fid in set(movidas) | set(con_hechos)]
    if movidas and not con_hechos:
        print("\nlas facturas se mueven sin que cambie ningun hecho: lo que cambia "
              "es la POLITICA aplicada a los mismos hechos (precedencia o "
              "resultado de una regla).")

    limite = 40
    for fid in a_mostrar[:limite]:
        a, b = antes[fid], despues[fid]
        cabecera = (f"{a.resultado} -> {b.resultado}"
                    if a.resultado != b.resultado else f"{a.resultado} (igual)")
        print(f"\n  {fid:<28} {cabecera}")
        quitados, puestos = _diff_hechos(a, b)
        for hecho in quitados:
            print(_pinta_hecho(hecho, "-"))
        for hecho in puestos:
            print(_pinta_hecho(hecho, "+"))
        if not quitados and not puestos:
            for linea in _evidencia_cambiada(a, b):
                print(linea)
            if a.resultado != b.resultado:
                print("      (mismos hechos, otro resultado: la politica decide "
                      "distinto sobre lo que ya estaba probado)")
                for hecho in b.hechos:
                    if hecho.ok or hecho.informativo:
                        continue
                    print(_pinta_hecho(_firma_de_hecho(hecho), "="))
        if a.motivos != b.motivos:
            print(f"      antes  : {'; '.join(a.motivos) or '(sin motivos)'}")
            print(f"      ahora  : {'; '.join(b.motivos) or '(sin motivos)'}")
    if len(a_mostrar) > limite:
        print(f"\n  ... y {len(a_mostrar) - limite} facturas mas")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--fijar", action="store_true", help="congela la referencia actual")
    p.add_argument("--comprobar", action="store_true", help="avisa si algo se ha movido")
    p.add_argument("--cobertura", action="store_true", help="tabla de reglas por factura")
    p.add_argument("--simula", nargs="+", metavar="AMBITO CLAVE=VALOR",
                   help="ensaya un cambio sin escribir nada: regla | erp | maestro | factura")
    p.add_argument("--factura", help="file_id sobre el que simular (ambito factura)")
    p.add_argument("--facturas", type=Path, default=P.FACTURAS_POR_DEFECTO)
    p.add_argument("--xlsx", type=Path, default=P.XLSX_POR_DEFECTO)
    p.add_argument("--config", type=Path, default=P.CONFIG_POR_DEFECTO)
    p.add_argument("--snapshot", type=Path, default=P.SNAPSHOT_POR_DEFECTO)
    p.add_argument("--lote", type=int, default=1)
    p.add_argument("--trabajadores", type=int, default=4)
    args = p.parse_args(argv)

    elegido = [args.fijar, args.comprobar, args.cobertura, bool(args.simula)]
    if sum(elegido) != 1:
        p.error("elige exactamente uno de --fijar, --comprobar, --cobertura, --simula")
    if args.fijar:
        return fijar(args)
    if args.comprobar:
        return comprobar(args)
    if args.simula:
        return simula(args)
    return cobertura(args)


if __name__ == "__main__":
    raise SystemExit(main())
