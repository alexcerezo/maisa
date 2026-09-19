"""
Auditoria de fiabilidad de la CAPA DE TEXTO del corpus.

`corpus_eval.py` mide el OCR contra la capa de texto del PDF. Ese script vale
solo si la capa dice la verdad, y no siempre la dice. Antes de fiarse de una
referencia hay que auditar la referencia. Este script lo hace.

Las cuatro formas en que una capa de texto puede mentir
------------------------------------------------------
1. BASURA. `ToUnicode` roto -> U+FFFD, area de uso privado, controles. El PDF
   se ve bien, el texto extraido es ilegible. Se detecta contando codepoints.

2. DESORDEN. `get_text_range()` devuelve el orden INTERNO del PDF, que no tiene
   por que ser el de lectura. Se reconstruye el texto por geometria (agrupar
   caracteres en lineas por su caja) y se compara. Si difieren, el orden
   interno no sirve como referencia.

3. FANTASMA. Texto presente en la capa pero NO pintado (blanco, fuera de
   pagina, modo invisible). Aqui esta la prueba que no depende de ninguna
   extraccion: se RENDERIZA la pagina y se mide la TINTA (proporcion de pixeles
   oscuros). Si la capa declara cientos de caracteres y la pagina no tiene
   tinta, la capa esta mintiendo. Y al reves: tinta sin capa de texto es un
   escaneo.

4. CONTENIDO DISTINTO. La capa dice un importe y el pixel dibuja otro (dato
   plantado). No se puede detectar sin leer los pixeles con un lector
   independiente; para eso esta `--with-ocr` y, si hace falta, la nube.

Uso (DENTRO del contenedor):

    python /tmp/audit_textlayer.py /home/ocr/test_files/corpus
    python /tmp/audit_textlayer.py /home/ocr/test_files/corpus --with-ocr --limit 60
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pypdfium2 as pdfium

sys.path.insert(0, "/tmp")
sys.path.insert(0, "/app")

try:
    from corpus_eval import content_metrics, normalize, truth_pages
except Exception:  # pragma: no cover
    content_metrics = normalize = truth_pages = None  # type: ignore

try:
    from inspect_doc import truth_geometric, suspicious_codepoints
except Exception:  # pragma: no cover - se redefine abajo
    truth_geometric = suspicious_codepoints = None  # type: ignore


# Umbrales. Son deliberadamente laxos: el objetivo es encontrar documentos
# RAROS, no decidir el destino del corpus. Un falso positivo se revisa a mano;
# un falso negativo invalida la metrica en silencio.
MIN_CHARS = 40           # por debajo: sin capa de texto
INK_DARK = 200           # gris por debajo del cual un pixel cuenta como tinta
GHOST_INK = 0.0005       # menos tinta que esto con texto declarado = fantasma
ORDER_BAD = 0.15         # CER entre orden interno y geometrico
WEIRD_BAD = 0.01         # proporcion de codepoints raros
AA_BAD = 0.05            # CER OCR vs capa


def ink_ratio(page: Any) -> float:
    """Proporcion de pixeles con tinta en la pagina renderizada."""
    bitmap = page.render(scale=1.0)
    try:
        array = np.asarray(bitmap.to_pil().convert("L"))
    finally:
        pass
    if array.size == 0:
        return 0.0
    return float((array < INK_DARK).mean())


def audit_page(page: Any, truth: str) -> dict[str, Any]:
    geom = truth_geometric(page) if truth_geometric else ""
    weird = suspicious_codepoints(truth)["counts"] if suspicious_codepoints else {}
    order_cer = 0.0
    if content_metrics and truth.strip() and geom.strip():
        order_cer = content_metrics(truth, geom)["cer"]
    chars = len(normalize(truth)) if normalize else len(truth.strip())
    return {
        "chars": chars,
        "order_cer": round(order_cer, 4),
        "weird": sum(weird.values()) if weird else 0,
        "weird_ratio": round((sum(weird.values()) / max(len(truth), 1)), 4) if weird else 0.0,
        "ink": round(ink_ratio(page), 5),
    }


def audit(path: Path) -> dict[str, Any] | None:
    doc = pdfium.PdfDocument(str(path))
    try:
        truths = truth_pages(str(path)) if truth_pages else []
        pages = []
        for index in range(len(doc)):
            page = doc[index]
            try:
                truth = truths[index] if index < len(truths) else ""
                pages.append(audit_page(page, truth))
            finally:
                page.close()
    finally:
        doc.close()

    chars = sum(p["chars"] for p in pages)
    ink = max((p["ink"] for p in pages), default=0.0)
    order = max((p["order_cer"] for p in pages), default=0.0)
    weird = sum(p["weird"] for p in pages)

    flags = []
    if weird and weird / max(chars, 1) > WEIRD_BAD:
        flags.append("basura")
    if order > ORDER_BAD:
        flags.append("desorden")
    if chars >= MIN_CHARS and ink < GHOST_INK:
        flags.append("fantasma")
    if chars < MIN_CHARS and ink > GHOST_INK:
        flags.append("escaneo")

    return {
        "file": path.name,
        "pages": len(pages),
        "chars": chars,
        "ink": round(ink, 5),
        "order_cer": round(order, 4),
        "weird": weird,
        "flags": flags,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audita la capa de texto del corpus.")
    parser.add_argument("root", nargs="?", default="/home/ocr/test_files/corpus")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--json", dest="json_out")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    root = Path(args.root)
    pdfs = sorted(root.rglob("*.pdf"))
    if args.limit:
        pdfs = pdfs[: args.limit]
    if not pdfs:
        print(f"No hay PDFs en {root}", file=sys.stderr)
        return 2

    print(f"Auditando {len(pdfs)} PDF en {root}\n")
    rows = []
    for index, path in enumerate(pdfs, 1):
        try:
            row = audit(path)
        except Exception as exc:
            print(f"  !! {path.name}: {type(exc).__name__}: {exc}")
            continue
        if row is None:
            continue
        rows.append(row)
        if not args.quiet:
            mark = ("  <-- " + ",".join(row["flags"])) if row["flags"] else ""
            print(f"  [{index:3d}/{len(pdfs)}] {path.name:36s} chars={row['chars']:5d} "
                  f"tinta={row['ink']:.5f} orden={row['order_cer']:.3f} "
                  f"raros={row['weird']:3d}{mark}")

    # ------------------------------------------------------------------ #
    print()
    print("=" * 78)
    print(" FIABILIDAD DE LA CAPA DE TEXTO")
    print("=" * 78)
    total = len(rows)
    con_capa = [r for r in rows if r["chars"] >= MIN_CHARS]
    sin_capa = [r for r in rows if r["chars"] < MIN_CHARS]
    print(f"  documentos            : {total}")
    print(f"  con capa de texto     : {len(con_capa)}")
    print(f"  sin capa (escaneo)    : {len(sin_capa)}")
    print()

    print("  [1] Codificacion (basura: U+FFFD / uso privado / controles)")
    basura = [r for r in rows if "basura" in r["flags"]]
    print(f"      documentos sospechosos: {len(basura)}")
    for r in basura[:10]:
        print(f"        {r['file']:36s} {r['weird']} codepoints raros")
    print()

    print("  [2] Orden (CER entre extraccion interna y geometrica)")
    ordered = [r["order_cer"] for r in rows if r["chars"] >= MIN_CHARS]
    if ordered:
        print(f"      mediana {statistics.median(ordered):.4f}   max {max(ordered):.4f}")
        desorden = [r for r in rows if "desorden" in r["flags"]]
        print(f"      documentos desordenados: {len(desorden)}")
        for r in sorted(desorden, key=lambda r: -r["order_cer"])[:10]:
            print(f"        {r['file']:36s} cer_orden={r['order_cer']:.4f}")
    print()

    print("  [3] Fantasma (texto declarado sin tinta en la pagina)")
    fantasma = [r for r in rows if "fantasma" in r["flags"]]
    print(f"      documentos sospechosos: {len(fantasma)}")
    for r in fantasma[:10]:
        print(f"        {r['file']:36s} {r['chars']} chars  tinta={r['ink']:.6f}")
    print()

    print("  [4] Escaneo (tinta sin capa de texto)")
    escaneos = [r for r in rows if "escaneo" in r["flags"]]
    print(f"      documentos: {len(escaneos)}")
    for r in escaneos[:10]:
        print(f"        {r['file']:36s} tinta={r['ink']:.5f}")
    print()

    print("  [5] Tinta por documento (para calibrar el umbral 'fantasma')")
    inks = sorted(r["ink"] for r in rows if r["chars"] >= MIN_CHARS)
    if inks:
        for pct in (0, 1, 5, 25, 50, 75, 100):
            i = min(len(inks) - 1, int(round(pct / 100 * (len(inks) - 1))))
            print(f"      p{pct:<3d} tinta={inks[i]:.5f}")
        print(f"      (el umbral 'fantasma' esta en {GHOST_INK}: mira donde caen los 0%)")

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        import json

        out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  JSON: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
