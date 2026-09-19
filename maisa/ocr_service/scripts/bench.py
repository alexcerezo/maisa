"""
Compara rendimiento y calidad entre modelos fp32 e INT8.

Mide por separado deteccion y reconocimiento (que es donde esta el tiempo real),
y compara el texto reconocido para cuantificar la perdida de precision.

Uso:
    python scripts/bench.py --image test_files/test.png
    python scripts/bench.py --image x.png --quantized quantized --runs 5
"""

from __future__ import annotations

import argparse
import statistics
import time
from pathlib import Path

import numpy as np
from PIL import Image

MODELS_DIR = Path("/usr/local/lib/python3.12/site-packages/rapidocr/models")


def build(label, det_path, rec_path, use_cls=True):
    from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR

    params = {
        "Det.engine_type": EngineType.ONNXRUNTIME,
        "Det.ocr_version": OCRVersion.PPOCRV5,
        "Det.model_type": ModelType.MOBILE,
        "Rec.engine_type": EngineType.ONNXRUNTIME,
        "Rec.ocr_version": OCRVersion.PPOCRV5,
        "Rec.model_type": ModelType.MOBILE,
        "Cls.engine_type": EngineType.ONNXRUNTIME,
        "Cls.ocr_version": OCRVersion.PPOCRV4,
        "Cls.model_type": ModelType.MOBILE,
        "Global.use_cls": use_cls,
    }
    if det_path:
        params["Det.model_path"] = str(det_path)
    if rec_path:
        params["Rec.model_path"] = str(rec_path)
    return RapidOCR(params=params)


def timed(engine, img, runs):
    times = []
    last = None
    for _ in range(runs + 1):  # +1 de calentamiento
        started = time.perf_counter()
        last = engine(img)
        elapsed = (time.perf_counter() - started) * 1000
        times.append(elapsed)
    times = times[1:]
    return last, times


def phase_split(engine, img, runs):
    """Tiempo de deteccion y de reconocimiento por separado."""
    from rapidocr.ch_ppocr_rec.typings import TextRecInput

    det, rec = engine.text_det, engine.text_rec

    det(img)
    dt = []
    for _ in range(runs):
        t = time.perf_counter()
        out = det(img)
        dt.append((time.perf_counter() - t) * 1000)

    boxes = out.boxes if out is not None else None
    if boxes is None or len(boxes) == 0:
        return statistics.median(dt), 0.0, 0

    crops = engine.crop_text_regions(img, boxes)
    rec(TextRecInput(img=crops))
    rt = []
    for _ in range(runs):
        t = time.perf_counter()
        rec(TextRecInput(img=crops))
        rt.append((time.perf_counter() - t) * 1000)

    return statistics.median(dt), statistics.median(rt), len(crops)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True, type=Path)
    ap.add_argument("--quantized", default="quantized", type=Path)
    ap.add_argument("--runs", default=3, type=int)
    ap.add_argument("--det", default="ch_PP-OCRv5_det_mobile.onnx")
    ap.add_argument("--rec", default="ch_PP-OCRv5_rec_mobile.onnx")
    args = ap.parse_args()

    img = np.asarray(Image.open(args.image).convert("RGB"))

    variants = [("fp32", MODELS_DIR / args.det, MODELS_DIR / args.rec)]
    for task, name in (("det", args.det), ("rec", args.rec)):
        cand = args.quantized / f"{Path(name).stem}_int8.onnx"
        if cand.exists():
            if task == "det":
                variants.append(("int8 (det+rec)", cand, args.quantized / f"{Path(args.rec).stem}_int8.onnx"))
            break

    results = {}
    print()
    print(f"Imagen: {args.image}  ({img.shape[1]}x{img.shape[0]})   runs={args.runs}")
    print("=" * 78)

    for label, det_path, rec_path in variants:
        if not Path(det_path).exists() or not Path(rec_path).exists():
            print(f"[!] {label}: modelos no encontrados, se omite.")
            continue
        engine = build(label, det_path, rec_path)
        det_ms, rec_ms, n_lines = phase_split(engine, img, args.runs)
        out, times = timed(engine, img, args.runs)

        txts = list(out.txts) if out.txts else []
        scores = [float(s) for s in (out.scores or [])]
        results[label] = {"txts": txts, "scores": scores, "total": statistics.median(times)}

        print(f"\n[{label}]")
        print(f"  Deteccion      : {det_ms:8.1f} ms")
        print(f"  Reconocimiento : {rec_ms:8.1f} ms   ({n_lines} lineas)")
        print(f"  TOTAL          : {statistics.median(times):8.1f} ms  "
              f"(min {min(times):.0f}, max {max(times):.0f})")
        print(f"  Score medio    : {statistics.mean(scores):.4f}" if scores else "  Sin texto")

    # ---- Comparativa ---- #
    if len(results) >= 2:
        base_label = variants[0][0]
        base = results[base_label]
        print()
        print("=" * 78)
        for label, data in results.items():
            if label == base_label:
                continue
            speedup = base["total"] / data["total"]
            print(f"\n  {label} vs {base_label}: {speedup:.2f}x "
                  f"({base['total']:.0f} ms -> {data['total']:.0f} ms)")

            n = min(len(base["txts"]), len(data["txts"]))
            same = sum(1 for i in range(n) if base["txts"][i] == data["txts"][i])
            print(f"  Texto identico : {same}/{n} lineas")

            if len(base["txts"]) != len(data["txts"]):
                print(f"  [!] Numero de lineas distinto: "
                      f"{len(base['txts'])} vs {len(data['txts'])}")
            if base["scores"] and data["scores"]:
                d = statistics.mean(data["scores"]) - statistics.mean(base["scores"])
                print(f"  Score medio    : {statistics.mean(base['scores']):.4f} -> "
                      f"{statistics.mean(data['scores']):.4f}  ({d:+.4f})")

            diffs = [i for i in range(n) if base["txts"][i] != data["txts"][i]]
            for i in diffs[:8]:
                print(f"    linea {i+1}:")
                print(f"      {base_label:5s}: {base['txts'][i]}")
                print(f"      {label:12s}: {data['txts'][i]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
