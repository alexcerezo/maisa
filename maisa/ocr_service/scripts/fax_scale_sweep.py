#!/usr/bin/env python3
"""
Barrido de escala de renderizado para documentos degradados (fax).

Hipotesis: si el raster nativo tiene mas resolucion que la que usamos al
renderizar (pdf_scale=2.0 -> 144 dpi), estamos reduciendo la imagen y
destruyendo los trazos finos. Renderizar a mayor escala deberia recuperar
texto que ahora se pierde.

Mide: lineas detectadas (tras filtro text_score), score medio, tiempo y
"calidad aparente" del texto (proporcion de caracteres alfanumericos).

Uso:
    python fax_scale_sweep.py "/tmp/docs/fax.pdf" --scales 2.0 2.9 4.0
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np


def build_engine(rec_tier: str = "mobile"):
    from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR

    return RapidOCR(
        params={
            "Det.engine_type": EngineType.ONNXRUNTIME,
            "Det.ocr_version": OCRVersion.PPOCRV5,
            "Det.model_type": ModelType.MOBILE,
            "Det.limit_side_len": 960,
            "Det.limit_type": "min",
            "Rec.engine_type": EngineType.ONNXRUNTIME,
            "Rec.ocr_version": OCRVersion.PPOCRV5,
            "Rec.model_type": ModelType.MOBILE if rec_tier == "mobile" else ModelType.SERVER,
            "Cls.engine_type": EngineType.ONNXRUNTIME,
            "Cls.ocr_version": OCRVersion.PPOCRV4,
            "Cls.model_type": ModelType.MOBILE,
            "Global.use_cls": True,
            "Global.text_score": 0.5,
        }
    )


def render(path: Path, scale: float) -> np.ndarray:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    img = pdf[0].render(scale=scale).to_pil().convert("RGB")
    return np.asarray(img)


def alpha_ratio(lines: list[dict]) -> float:
    """Proporcion de caracteres alfanumericos: mide si el texto es real."""
    chars = [c for ln in lines for c in ln["text"]]
    if not chars:
        return 0.0
    return sum(c.isalnum() for c in chars) / len(chars)


def run(engine, img: np.ndarray) -> dict:
    t = time.perf_counter()
    out = engine(img)
    ms = (time.perf_counter() - t) * 1000

    lines = []
    boxes = getattr(out, "boxes", None)
    if boxes is not None and len(boxes) > 0:
        for box, text, score in zip(boxes, out.txts or [], out.scores or []):
            clean = (text or "").strip()
            if clean:
                lines.append({"text": clean, "score": float(score)})

    return {
        "lines": lines,
        "ms": ms,
        "n": len(lines),
        "mean": float(np.mean([l["score"] for l in lines])) if lines else 0.0,
        "alpha": alpha_ratio(lines),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path)
    ap.add_argument("--scales", type=float, nargs="+", default=[2.0, 2.9, 4.0])
    ap.add_argument("--rec", default="mobile", choices=["mobile", "server"])
    ap.add_argument("--show", type=int, default=8, help="Lineas a mostrar por prueba")
    args = ap.parse_args()

    engine = build_engine(args.rec)
    print(f"Documento : {args.path.name}")
    print(f"rec tier  : {args.rec}")
    print()
    print(f"{'escala':>7} {'px':>11} {'lineas':>7} {'score':>7} {'alfanum':>8} {'ms':>8}")
    print("-" * 54)

    best = None
    for scale in args.scales:
        img = render(args.path, scale)
        r = run(engine, img)
        h, w = img.shape[:2]
        print(
            f"{scale:>7.2f} {w:>5d}x{h:<5d} {r['n']:>7d} {r['mean']:>7.3f} "
            f"{r['alpha']:>7.1%} {r['ms']:>8.0f}"
        )
        if best is None or r["alpha"] > best[1]["alpha"]:
            best = (scale, r)

    if best:
        scale, r = best
        print()
        print(f"--- Mejor por alfanumericos: escala {scale} ({r['alpha']:.1%}) ---")
        for ln in r["lines"][: args.show]:
            print(f"    [{ln['score']:.3f}] {ln['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
