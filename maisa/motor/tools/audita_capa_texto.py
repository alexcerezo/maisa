"""Caso 4 de la auditoria de la capa de texto: CONTENIDO DISTINTO.

``ocr_service/scripts/audit_textlayer.py`` cubre las tres formas en que una
capa de texto miente **sin leer los pixeles**: basura (codificacion rota),
desorden (orden interno distinto del de lectura) y fantasma (texto declarado
que no se pinta). Su propio docstring deja fuera la cuarta:

    "4. CONTENIDO DISTINTO. La capa dice un importe y el pixel dibuja otro
     (dato plantado). No se puede detectar sin leer los pixeles con un lector
     independiente; para eso esta ``--with-ocr``."

Este script es ese ``--with-ocr``. No vuelve a medir tinta ni geometria: pasa
el OCR por encima de documentos que **si** traen capa de texto, extrae los
campos de las dos lecturas con el mismo extractor (``maisa.texto``) y compara
los que mueven dinero.

Por que importa: 471 de las 500 facturas se deciden leyendo la capa nativa, sin
OCR. Si esa capa dijera un IBAN distinto del que dibuja la pagina, el motor
decidiria sobre un dato que nadie ve. Es el unico fallo que no se detecta por
regla, porque no hay nada incoherente que detectar.

Las divergencias NO son prueba de dato plantado
------------------------------------------------
El OCR local se equivoca: confunde un digito del pedido, pierde un campo en un
escaneo sucio. Por eso el informe separa las dos cosas y lo dice:

- ``iban``   -> si difiere, es lo mas grave que puede salir de aqui. Un IBAN
                solo lo lee mal el OCR si el OCR es malo; la capa no tiene por
                que equivocarse. Se revisa siempre a mano.
- ``total``  -> un importe distinto en los pixeles es dinero distinto.
- ``pedido`` -> el fallo conocido del OCR local (``2026`` -> ``2028``): es un
                error de lectura, no un dato plantado, y el motor lo escala.
- ``nif``, ``fecha`` -> mismo razonamiento.

Uso (en el host, con el servicio de vision levantado):

    ../../.venv/bin/python tools/audita_capa_texto.py --muestra 60
    ../../.venv/bin/python tools/audita_capa_texto.py --muestra 60 --salida /tmp/c4.json

Las 500 tardan ~70 min (9 s por factura en local). Una muestra de 60, ~10 min.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

from maisa.lectura import _paginas_respuesta, _peticion_ocr, capa_texto  # noqa: E402
from maisa.normaliza import a_decimal  # noqa: E402
from maisa.texto import Lectura, extrae  # noqa: E402

#: Campos que mueven dinero, en orden de gravedad. El orden importa: el informe
#: se ordena por el primero que diverge.
CAMPOS = ("iban", "total", "base", "iva", "pedido", "nif", "fecha")
GRAVEDAD = {"iban": "critico", "total": "critico", "base": "alto", "iva": "alto",
            "pedido": "medio", "nif": "medio", "fecha": "bajo"}


def normaliza(campo: str, valor: str) -> str:
    """Forma canonica de un campo para poder comparar dos lecturas."""
    v = valor.strip().upper()
    if campo == "iban":
        return "".join(c for c in v if c.isalnum())
    if campo in ("total", "base", "iva"):
        d = a_decimal(v)
        return str(d) if d is not None else v
    if campo == "pedido":
        return "".join(c for c in v if c.isalnum())
    return v


def compara(capa: Lectura, ocr: Lectura) -> list[dict]:
    """Divergencias por campo entre la capa nativa y los pixeles.

    Una divergencia es: la capa aporta un valor primario y el OCR aporta
    valores, y **ninguno** coincide. Si el OCR no leyo el campo no hay
    divergencia: es perdida de recuerdo, no discrepancia de contenido.
    """
    salida = []
    for campo in CAMPOS:
        de_capa = capa.valores(campo)
        de_ocr = ocr.valores(campo)
        if not de_capa or not de_ocr:
            continue
        canon_capa = {normaliza(campo, v) for v in de_capa}
        canon_ocr = {normaliza(campo, v) for v in de_ocr}
        if canon_capa & canon_ocr:
            continue
        salida.append({
            "campo": campo,
            "gravedad": GRAVEDAD[campo],
            "capa": de_capa[:3],
            "ocr": de_ocr[:3],
        })
    return salida


def audita(ruta: Path, timeout: float, conexion: float) -> dict:
    """Las dos lecturas de un documento y lo que discrepan."""
    texto, paginas = capa_texto(ruta)
    capa = extrae(texto, ruta.name, paginas, "texto_determinista")

    datos = _peticion_ocr(ruta, "local", timeout, conexion)
    paginas_ocr = _paginas_respuesta(datos)
    texto_ocr = "\n".join(paginas_ocr)
    ocr = extrae(texto_ocr, ruta.name, len(paginas_ocr) or paginas, "vision_ocr")

    divergencias = compara(capa, ocr)
    return {
        "file": ruta.name,
        "paginas": paginas,
        "chars_capa": len(texto),
        "chars_ocr": len(texto_ocr),
        "sospechosos": capa.sospechosos[:4],
        "ordenes": capa.ordenes[:4],
        "divergencias": divergencias,
        "campos_capa": {c: capa.valores(c)[:2] for c in CAMPOS},
        "campos_ocr": {c: ocr.valores(c)[:2] for c in CAMPOS},
    }


def elige_muestra(corpus: Path, n: int) -> tuple[list[Path], list[Path]]:
    """Muestra estratificada que **siempre** incluye los documentos con inyeccion.

    Los que llevan instrucciones u ordenes de resultado son los que el corpus
    construyo para manipular a un lector; si la capa mintiera en algun sitio,
    el sitio mas probable es ese. Entran todos, sin sorteo.
    """
    con_capa: list[Path] = []
    con_marca: list[Path] = []
    for ruta in sorted(corpus.rglob("*.pdf")):
        try:
            texto, paginas = capa_texto(ruta)
        except Exception:
            continue
        if paginas == 0 or len(texto.strip()) < 40:
            continue
        con_capa.append(ruta)
        lectura = extrae(texto, ruta.name, paginas, "texto_determinista")
        if lectura.sospechosos or lectura.ordenes:
            con_marca.append(ruta)

    resto = [r for r in con_capa if r not in set(con_marca)]
    faltan = max(0, n - len(con_marca))
    random.Random(20260919).shuffle(resto)
    return con_marca + resto[:faltan], con_capa


def main() -> int:
    parser = argparse.ArgumentParser(description="Audita el CONTENIDO de la capa de texto contra los pixeles.")
    parser.add_argument("--corpus", default=str(RAIZ.parent / "data" / "facturas"))
    parser.add_argument("--muestra", type=int, default=60, help="documentos con capa a auditar (ademas de los marcados)")
    parser.add_argument("--salida", help="JSON con el detalle por documento")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--conexion", type=float, default=10.0)
    args = parser.parse_args()

    corpus = Path(args.corpus)
    muestra, con_capa = elige_muestra(corpus, args.muestra)
    print(f"Corpus: {corpus}")
    print(f"Documentos con capa de texto: {len(con_capa)}")
    print(f"Muestra a auditar: {len(muestra)} (incluye los marcados con inyeccion)\n")

    filas, fallos = [], []
    for i, ruta in enumerate(muestra, 1):
        try:
            fila = audita(ruta, args.timeout, args.conexion)
        except Exception as exc:
            fallos.append({"file": ruta.name, "error": f"{type(exc).__name__}: {exc}"})
            print(f"  [{i:3d}/{len(muestra)}] {ruta.name:34s} !! {type(exc).__name__}: {exc}")
            continue
        filas.append(fila)
        if fila["divergencias"]:
            peor = fila["divergencias"][0]
            print(f"  [{i:3d}/{len(muestra)}] {ruta.name:34s} DIVERGE {peor['campo']:7s} "
                  f"capa={peor['capa'][0]!r} ocr={peor['ocr'][0]!r}")
        else:
            print(f"  [{i:3d}/{len(muestra)}] {ruta.name:34s} de acuerdo")

    # ------------------------------------------------------------------ #
    print()
    print("=" * 78)
    print(" CONTENIDO DE LA CAPA DE TEXTO vs LOS PIXELES")
    print("=" * 78)
    print(f"  auditados          : {len(filas)}")
    print(f"  fallos de lectura  : {len(fallos)}")
    con_div = [f for f in filas if f["divergencias"]]
    print(f"  con divergencia    : {len(con_div)}")
    print()

    por_campo: Counter = Counter()
    for fila in con_div:
        for d in fila["divergencias"]:
            por_campo[d["campo"]] += 1
    if por_campo:
        print("  Divergencias por campo")
        for campo, n in por_campo.most_common():
            print(f"      {campo:8s} {n:3d}  ({GRAVEDAD[campo]})")
        print()

    critico = [f for f in con_div if f["divergencias"][0]["gravedad"] == "critico"]
    if critico:
        print(f"  DIVERGENCIAS CRITICAS (IBAN o importe): {len(critico)}")
        for fila in critico:
            print(f"      {fila['file']}")
            for d in fila["divergencias"]:
                print(f"          {d['campo']:8s} capa={d['capa']}  ocr={d['ocr']}")
        print()

    marcados = [f for f in filas if f["sospechosos"] or f["ordenes"]]
    con_div_marca = [f for f in marcados if f["divergencias"]]
    print(f"  Documentos marcados auditados : {len(marcados)}")
    print(f"  de esos, con divergencia      : {len(con_div_marca)}")
    print()

    if fallos:
        print("  Fallos")
        for f in fallos:
            print(f"      {f['file']:34s} {f['error']}")
        print()

    if args.salida:
        destino = Path(args.salida)
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(json.dumps(
            {"muestra": len(muestra), "auditados": len(filas), "fallos": fallos, "filas": filas},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  JSON: {destino}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
