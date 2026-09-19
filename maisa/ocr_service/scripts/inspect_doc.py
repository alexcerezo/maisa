"""
Inspeccion de un documento: ¿es fiable su capa de texto?

`corpus_eval.py` mide el OCR contra la capa de texto del PDF. Eso solo vale si
la capa de texto DICE LA VERDAD, y no siempre la dice. Este script audita la
capa antes de creersela, por tres vias independientes:

  1. CODIFICACION. Codepoints raros (U+FFFD, area de uso privado, controles)
     delatan un `ToUnicode` roto: el PDF se ve bien pero el texto extraido es
     basura. Aqui la capa no es "incorrecta", es ILEGIBLE.

  2. ORDEN. `get_text_range()` devuelve el texto en el orden INTERNO del PDF,
     que no tiene por que ser el orden de lectura. Se reconstruye el texto por
     GEOMETRIA (agrupar caracteres por linea segun su caja, ordenar por
     arriba->abajo e izquierda->derecha) y se comparan las dos versiones. Si
     discrepan mucho, la extraccion por orden interno esta desordenada y la
     geometrica es la verdad utilizable.

  3. RENDER. Se guarda la pagina como PNG. Ninguna capa de texto puede mentir
     sobre lo que se VE: si la capa y el OCR discrepan, el PNG decide quien
     tiene razon. Es la comprobacion que no depende de software.

Uso (DENTRO del contenedor):

    python /tmp/inspect_doc.py --out /tmp/insp <pdf> [<pdf> ...]
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium

sys.path.insert(0, "/tmp")
try:
    from corpus_eval import levenshtein, normalize, content_metrics, truth_pages
except Exception:  # pragma: no cover
    truth_pages = None  # type: ignore
    normalize = levenshtein = content_metrics = None  # type: ignore

sys.path.insert(0, "/app")
try:
    from app.server import _Source, _run_local_page  # noqa: E402
except Exception:  # pragma: no cover
    _Source = _run_local_page = None  # type: ignore


# --------------------------------------------------------------------------- #
# 1. Codificacion
# --------------------------------------------------------------------------- #


def suspicious_codepoints(text: str) -> dict[str, Any]:
    """Cuenta codepoints que delatan una capa de texto rota."""
    counts = {"replacement": 0, "pua": 0, "control": 0}
    samples: dict[str, list[str]] = {}
    for ch in text:
        code = ord(ch)
        if ch == "\ufffd":
            kind = "replacement"
        elif 0xE000 <= code <= 0xF8FF or 0xF0000 <= code <= 0xFFFFD:
            kind = "pua"
        elif unicodedata.category(ch) == "Cc" and ch not in "\n\r\t":
            kind = "control"
        else:
            continue
        counts[kind] += 1
        if len(samples.setdefault(kind, [])) < 6:
            samples[kind].append(f"U+{code:04X}")
    return {"counts": counts, "samples": samples}


# --------------------------------------------------------------------------- #
# 2. Orden geometrico
# --------------------------------------------------------------------------- #


def _char_boxes(page: Any) -> list[tuple[str, tuple[float, float, float, float]]]:
    textpage = page.get_textpage()
    try:
        total = textpage.count_chars()
        out = []
        for index in range(total):
            ch = textpage.get_text_range(index, 1)
            try:
                box = textpage.get_charbox(index)
            except Exception:
                box = None
            if box is None or not ch or ch in "\r\n":
                continue
            out.append((ch, tuple(box)))
        return out
    finally:
        textpage.close()


def truth_geometric(page: Any) -> str:
    """
    Texto reconstruido por posicion: agrupa caracteres en lineas por su caja.

    Se usa la coordenada Y del CENTRO de cada caracter para agrupar, y se corta
    la linea cuando el centro se aleja mas de media altura de caracter. Es
    robusto a superindices y a lineas ligeramente torcidas.
    """
    boxes = _char_boxes(page)
    if not boxes:
        return ""

    items = []
    for ch, (left, bottom, right, top) in boxes:
        height = max(top - bottom, 0.1)
        items.append({"ch": ch, "left": left, "right": right, "cy": (top + bottom) / 2,
                      "h": height})

    # Ordenar por Y descendente (en PDF el eje Y crece hacia arriba).
    items.sort(key=lambda it: -it["cy"])

    lines: list[list[dict]] = []
    for item in items:
        if lines:
            current = lines[-1]
            ref = sum(i["cy"] for i in current) / len(current)
            tolerance = 0.6 * max(item["h"], max(i["h"] for i in current))
            if abs(item["cy"] - ref) <= tolerance:
                current.append(item)
                continue
        lines.append([item])

    out = []
    for line in lines:
        line.sort(key=lambda it: it["left"])
        pieces: list[str] = []
        previous = None
        for item in line:
            if previous is not None:
                gap = item["left"] - previous["right"]
                if gap > 0.25 * max(item["h"], previous["h"]):
                    pieces.append(" ")
            pieces.append(item["ch"])
            previous = item
        text = "".join(pieces).strip()
        if text:
            out.append(text)
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# 3. OCR
# --------------------------------------------------------------------------- #


def ocr_page(path: Path, index: int = 0) -> dict[str, Any]:
    if _Source is None or _run_local_page is None:
        return {"error": "app.server no importable"}
    source = _Source(path)
    try:
        return _run_local_page(source, index, source.scales(None), False)
    finally:
        source.close()


# --------------------------------------------------------------------------- #
# Informe
# --------------------------------------------------------------------------- #


def inspect(path: Path, out_dir: Path) -> None:
    print("=" * 78)
    print(f" {path.name}   ({path.stat().st_size} B)")
    print("=" * 78)

    doc = pdfium.PdfDocument(str(path))
    try:
        pages = len(doc)
        page = doc[0]

        raw = truth_pages(str(path))[0] if truth_pages else ""
        geom = truth_geometric(page)

        # --- 1. codificacion ---
        bad = suspicious_codepoints(raw + geom)
        print("\n[1] Codificacion de la capa de texto")
        print(f"    caracteres extraidos : {len(raw)}")
        total_bad = sum(bad["counts"].values())
        if total_bad == 0:
            print("    codepoints raros     : ninguno (capa bien codificada)")
        else:
            print(f"    codepoints raros     : {bad['counts']}  <- CAPA SOSPECHOSA")
            for kind, sample in bad["samples"].items():
                print(f"      {kind:12s}: {sample}")

        # --- 2. orden ---
        print("\n[2] Orden de extraccion vs orden geometrico")
        if content_metrics and raw and geom:
            m = content_metrics(raw, geom)
            print(f"    CER orden-interno vs geometrico : {m['cer']:.4f} "
                  f"(ordenado {m['cer_sorted']:.4f})")
            if m["cer"] <= 0.01:
                print("    => los dos ordenes coinciden: la extraccion NO esta desordenada")
            else:
                print("    => DIFIEREN: el orden interno no es el de lectura")
                print("       (cer_sorted bajo significa que estan los mismos caracteres)")
        else:
            print("    no disponible")

        # --- 3. OCR ---
        print("\n[3] OCR local (motor PP-OCRv5)")
        entry = ocr_page(path)
        if "error" in entry:
            print(f"    {entry['error']}")
            ocr_text = ""
        else:
            ocr_text = entry["text"]
            print(f"    lineas={len(entry['lines'])} escala={entry['scale']} "
                  f"alpha={entry['alpha_ratio']} {entry['elapsed']:.2f}s")

        if content_metrics:
            print("\n[4] Comparaciones")
            if raw:
                m = content_metrics(raw, ocr_text)
                print(f"    OCR vs capa (orden interno) : cer={m['cer']:.4f} ord={m['cer_sorted']:.4f}")
            if geom:
                m = content_metrics(geom, ocr_text)
                print(f"    OCR vs capa (geometrica)    : cer={m['cer']:.4f} ord={m['cer_sorted']:.4f}")

        # --- texto, lado a lado ---
        print("\n[5] Texto de las tres fuentes")
        raw_lines = [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]
        geom_lines = [ln.strip() for ln in (geom or "").splitlines() if ln.strip()]
        ocr_lines = [ln.strip() for ln in (ocr_text or "").splitlines() if ln.strip()]
        width = max(len(raw_lines), len(geom_lines), len(ocr_lines), 1)
        print(f"    {'CAPA (interno)':38s} | {'CAPA (geometrica)':38s} | OCR")
        for i in range(min(width, 30)):
            a = raw_lines[i][:38] if i < len(raw_lines) else ""
            b = geom_lines[i][:38] if i < len(geom_lines) else ""
            c = ocr_lines[i][:40] if i < len(ocr_lines) else ""
            print(f"    {a:38s} | {b:38s} | {c}")

        # --- 6. render ---
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = "".join(c if c.isalnum() or c in "-_." else "_" for c in path.stem)[:50]
        for index in range(min(pages, 3)):
            p = doc[index]
            try:
                width_px = p.get_width() * 2.0
                scale = 1400 / width_px if width_px > 1400 else 2.0
                bitmap = p.render(scale=scale)
                png = out_dir / f"{stem}_p{index + 1}.png"
                bitmap.to_pil().save(png)
                print(f"\n[6] Render guardado: {png}")
            finally:
                p.close()
    finally:
        doc.close()
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Audita la capa de texto de un PDF.")
    parser.add_argument("targets", nargs="+")
    parser.add_argument("--out", default="/tmp/insp", help="directorio para los PNG")
    args = parser.parse_args()

    paths = []
    for target in args.targets:
        p = Path(target)
        if p.is_dir():
            paths.extend(sorted(p.rglob("*.pdf")))
        else:
            paths.append(p)

    out_dir = Path(args.out)
    for path in paths:
        try:
            inspect(path, out_dir)
        except Exception as exc:
            print(f"!! {path.name}: {type(exc).__name__}: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
