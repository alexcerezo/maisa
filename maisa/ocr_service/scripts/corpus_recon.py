"""
Reconocimiento del corpus de facturas (Maisa / "500 Sombras de Alberto").

Antes de medir nada hay que saber QUE hay en el corpus. Este script clasifica
cada PDF y, sobre todo, responde a una pregunta que cambia todo el plan:

    ¿tienen los PDF una CAPA DE TEXTO?

Si la tienen, el texto embebido es el ground truth exacto y se puede medir el
error real del OCR (CER/WER) en vez de comparar dos motores y suponer que uno
tiene razon. Si no la tienen (escaneos), no hay verdad y hay que seguir con la
comparacion nube-vs-local.

Uso (DENTRO del contenedor, que es donde vive pypdfium2):

    python /tmp/corpus_recon.py /home/ocr/test_files/corpus
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

import pypdfium2 as pdfium

# Por debajo de esto se considera que el PDF no tiene capa de texto util.
# Un escaneo puede devolver 0-3 caracteres sueltos por ruido de metadatos.
MIN_CHARS_TEXT_LAYER = 40

# Familias de nombre observadas en el corpus.
FAMILIES = [
    (re.compile(r"^\d{4}-\d{2}-\d{2}_"), "fecha_Pxxx"),
    (re.compile(r"^FA-\d+_"), "FA-nnnn_categoria"),
    (re.compile(r"^factura"), "factura*"),
    (re.compile(r"^scan_?\d*"), "scan*"),
    (re.compile(r"^reimpresion"), "reimpresion*"),
]


def family_of(name: str) -> str:
    for pattern, label in FAMILIES:
        if pattern.match(name):
            return label
    return "otro"


def inspect(path: Path) -> dict:
    """Paginas, caracteres de capa de texto y primer renglon de cada PDF."""
    pages = 0
    chars = 0
    first = ""
    error = None
    try:
        doc = pdfium.PdfDocument(str(path))
    except Exception as exc:  # PDF corrupto o formato raro
        return {"pages": 0, "chars": 0, "first": "", "error": f"{type(exc).__name__}"}
    try:
        pages = len(doc)
        for i in range(pages):
            page = doc[i]
            try:
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_range() or ""
                finally:
                    textpage.close()
            finally:
                page.close()
            text = text.strip()
            chars += len(text)
            if not first and text:
                first = " ".join(text.split())[:70]
    except Exception as exc:
        error = f"{type(exc).__name__}"
    finally:
        doc.close()
    return {"pages": pages, "chars": chars, "first": first, "error": error}


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/home/ocr/test_files/corpus")
    pdfs = sorted(p for p in root.rglob("*.pdf"))
    if not pdfs:
        print(f"No hay PDFs en {root}", file=sys.stderr)
        return 2

    print(f"Corpus: {root}")
    print(f"PDFs  : {len(pdfs)}")
    print()

    rows = []
    for path in pdfs:
        info = inspect(path)
        info["name"] = path.name
        info["size"] = path.stat().st_size
        info["family"] = family_of(path.name)
        rows.append(info)

    con_texto = [r for r in rows if r["chars"] >= MIN_CHARS_TEXT_LAYER]
    sin_texto = [r for r in rows if r["chars"] < MIN_CHARS_TEXT_LAYER]

    print("=== ¿Hay capa de texto? ===")
    print(f"  con texto : {len(con_texto):4d}  ({100 * len(con_texto) / len(rows):.1f}%)")
    print(f"  sin texto : {len(sin_texto):4d}  ({100 * len(sin_texto) / len(rows):.1f}%)  <- escaneos, sin ground truth")
    print()

    print("=== Familias de nombre ===")
    for fam, count in Counter(r["family"] for r in rows).most_common():
        sub = [r for r in rows if r["family"] == fam]
        con = sum(1 for r in sub if r["chars"] >= MIN_CHARS_TEXT_LAYER)
        med = sorted(r["size"] for r in sub)[len(sub) // 2]
        print(f"  {fam:18s} {count:4d} ficheros | con texto {con:4d} | mediana {med:8d} B")
    print()

    print("=== Paginas ===")
    for pages, count in sorted(Counter(r["pages"] for r in rows).items()):
        print(f"  {pages} pag: {count}")
    print()

    print("=== Errores de lectura de PDF ===")
    errores = [r for r in rows if r["error"]]
    if not errores:
        print("  ninguno")
    for r in errores[:20]:
        print(f"  {r['name']}: {r['error']}")
    print()

    print("=== Muestra: CON capa de texto (ground truth disponible) ===")
    for r in con_texto[:6]:
        print(f"  {r['name']:34s} {r['chars']:6d} chars  | {r['first']}")
    print()

    print("=== Muestra: SIN capa de texto (escaneos) ===")
    for r in sin_texto[:8]:
        print(f"  {r['name']:34s} {r['size']:8d} B  pages={r['pages']}  chars={r['chars']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
