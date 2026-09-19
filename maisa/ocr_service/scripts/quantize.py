"""
Cuantizacion estatica INT8 de los modelos PP-OCRv5 para RapidOCR.

POR QUE ESTATICA Y NO DINAMICA
------------------------------
Inspeccionando los grafos (scripts/inspect_models.py) se ve que estos modelos son
CNN puros: Conv domina y MatMul/Gemm es residual (0% en deteccion, 2.5% en
reconocimiento). La cuantizacion dinamica de onnxruntime solo toca MatMul/Gemm,
asi que en estos modelos no serviria de nada. Hace falta cuantizacion ESTATICA,
que requiere un conjunto de calibracion con imagenes representativas.

QUE HACE
--------
1. Recolecta imagenes de calibracion (documentos reales si existen; si no,
   genera documentos sinteticos).
2. Ejecuta la deteccion fp32 para obtener lineas de texto y recortarlas.
3. Cuantiza det + rec (+ cls si se pide) usando los preprocesadores OFICIALES de
   RapidOCR, garantizando que las estadisticas de activacion coincidan con las
   de inferencia real.
4. Escribe los .onnx en el directorio de salida junto a un manifest.json.

Uso:
    python scripts/quantize.py [--calib-dir calibration] [--out quantized]
                              [--limit 60] [--format qdq|qoperator]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

MODELS_DIR = Path("/usr/local/lib/python3.12/site-packages/rapidocr/models")

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}


# --------------------------------------------------------------------------- #
# Generacion de calibracion sintetica (fallback si no hay documentos reales)
# --------------------------------------------------------------------------- #

_SYNTHETIC_LINES = [
    "FACTURA Nº {n}-{y}",
    "Cliente: Distribuciones Peñarroya S.L.",
    "CIF: B-87654321",
    "Fecha: {d:02d}/{m:02d}/{y}",
    "Dirección: Calle Alcalá 123, 4º B",
    "Servicio de digitalización documental",
    "Mantenimiento anual de equipos",
    "Subtotal        2.450,00 EUR",
    "IVA 21%           514,50 EUR",
    "TOTAL A PAGAR   2.964,50 EUR",
    "Referencia: REF-{n:05d}-{y}",
    "Observaciones: entrega en 48 horas",
    "Teléfono: +34 912 345 678",
    "Email: facturacion@empresa.es",
    "Página 1 de 3",
    "Estimado cliente, adjuntamos la factura",
    "correspondiente al periodo solicitado.",
    "Gracias por su confianza.",
    "Nº de pedido: PED-{n:04d}",
    "Importe pendiente: 1.234,56 EUR",
]


def generate_synthetic_docs(out_dir: Path, count: int, seed: int = 0) -> list[Path]:
    """Renderiza documentos sinteticos variados para calibrar."""
    from PIL import Image, ImageDraw, ImageFont

    rng = np.random.default_rng(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    for idx in range(count):
        width = int(rng.integers(700, 1700))
        height = int(rng.integers(500, 1400))
        # Fondo casi blanco, con algo de ruido como un escaneo real
        bg = int(rng.integers(243, 256))
        img = Image.new("RGB", (width, height), (bg, bg, bg))
        draw = ImageDraw.Draw(img)

        size = int(rng.integers(20, 46))
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:  # Pillow antiguo
            font = ImageFont.load_default()

        n_lines = int(rng.integers(6, 16))
        y = int(rng.integers(20, 60))
        while y < height - size - 30 and n_lines > 0:
            template = _SYNTHETIC_LINES[int(rng.integers(len(_SYNTHETIC_LINES)))]
            text = template.format(
                n=int(rng.integers(1, 9999)), y=2026, d=int(rng.integers(1, 29)), m=int(rng.integers(1, 13))
            )
            x = int(rng.integers(20, 60))
            ink = int(rng.integers(0, 60))
            draw.text((x, y), text, font=font, fill=(ink, ink, ink))
            y += size + int(rng.integers(6, 18))
            n_lines -= 1

        path = out_dir / f"synthetic_{idx:03d}.png"
        img.save(path)
        paths.append(path)

    return paths


# --------------------------------------------------------------------------- #
# Lectores de calibracion
# --------------------------------------------------------------------------- #


def _make_reader():
    from onnxruntime.quantization import CalibrationDataReader

    class ListReader(CalibrationDataReader):
        """Alimenta una lista de arrays como entradas del grafo."""

        def __init__(self, samples: list[dict[str, np.ndarray]]) -> None:
            self.samples = samples
            self.index = 0

        def get_next(self):
            if self.index >= len(self.samples):
                return None
            sample = self.samples[self.index]
            self.index += 1
            return sample

        def rewind(self) -> None:
            self.index = 0

    return ListReader


# --------------------------------------------------------------------------- #
# Cuantizacion
# --------------------------------------------------------------------------- #


def _quantize(src: Path, dst: Path, reader, quant_format, per_channel: bool) -> dict:
    from onnxruntime.quantization import QuantType, quantize_static

    started = time.perf_counter()
    quantize_static(
        model_input=str(src),
        model_output=str(dst),
        calibration_data_reader=reader,
        quant_format=quant_format,
        # Solo Conv: es donde esta todo el peso de estos modelos CNN.
        op_types_to_quantize=["Conv"],
        per_channel=per_channel,
        weight_type=QuantType.QInt8,
        activation_type=QuantType.QUInt8,
        extra_options={
            "ActivationSymmetric": False,
            "WeightSymmetric": True,
        },
    )
    return {
        "source": src.name,
        "output": dst.name,
        "src_mb": round(src.stat().st_size / 1e6, 2),
        "dst_mb": round(dst.stat().st_size / 1e6, 2),
        "seconds": round(time.perf_counter() - started, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Cuantizacion INT8 de PP-OCRv5")
    parser.add_argument("--calib-dir", default="calibration", type=Path)
    parser.add_argument("--out", default="quantized", type=Path)
    parser.add_argument(
        "--limit", default=60, type=int, help="Maximo de imagenes de calibracion"
    )
    parser.add_argument(
        "--synthetic", default=30, type=int, help="Documentos sinteticos a generar si no hay reales"
    )
    parser.add_argument(
        "--format", default="qdq", choices=["qdq", "qoperator"], help="Formato de cuantizacion"
    )
    parser.add_argument("--no-per-channel", action="store_true")
    parser.add_argument("--det", default="ch_PP-OCRv5_det_mobile.onnx")
    parser.add_argument("--rec", default="ch_PP-OCRv5_rec_mobile.onnx")
    args = parser.parse_args()

    from onnxruntime.quantization import QuantFormat
    from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR
    from rapidocr.ch_ppocr_det.utils import DetPreProcess

    quant_format = (
        QuantFormat.QDQ if args.format == "qdq" else QuantFormat.QOperator
    )
    per_channel = not args.no_per_channel

    # ---- 1. Recolectar imagenes de calibracion ---------------------------- #
    args.calib_dir.mkdir(parents=True, exist_ok=True)
    images = sorted(
        p for p in args.calib_dir.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES
    )
    if not images:
        print(f"[!] No hay imagenes en {args.calib_dir}/. Generando {args.synthetic} sinteticas.")
        images = generate_synthetic_docs(args.calib_dir, args.synthetic)
    images = images[: args.limit]
    print(f"[+] Imagenes de calibracion: {len(images)}")

    # ---- 2. Motor fp32 para deteccion y preprocesado ---------------------- #
    print("[+] Cargando motor fp32 para extraer recortes...")
    params = {
        "Det.engine_type": EngineType.ONNXRUNTIME,
        "Det.ocr_version": OCRVersion.PPOCRV5,
        "Det.model_type": ModelType.MOBILE,
        "Rec.engine_type": EngineType.ONNXRUNTIME,
        "Rec.ocr_version": OCRVersion.PPOCRV5,
        "Rec.model_type": ModelType.MOBILE,
        "Global.use_cls": False,
    }
    engine = RapidOCR(params=params)
    det = engine.text_det
    rec = engine.text_rec

    from PIL import Image as PILImage

    det_pre = DetPreProcess(engine.max_side_len, engine.min_side_len, None, None)

    det_samples: list[dict] = []
    rec_samples: list[dict] = []

    rec_max_ratio = rec.rec_image_shape[2] / rec.rec_image_shape[1]

    for i, path in enumerate(images, 1):
        try:
            img = np.asarray(PILImage.open(path).convert("RGB"))
        except Exception as exc:
            print(f"    [!] {path.name}: {exc}")
            continue

        # --- deteccion: entrada normalizada CHW ---
        tensor = det_pre(img)
        if tensor is None:
            continue
        det_samples.append({"x": np.expand_dims(tensor.astype(np.float32), 0)})

        # --- reconocimiento: recortes de las cajas detectadas ---
        try:
            out = det(img)
            boxes = getattr(out, "boxes", None)
            if boxes is None or len(boxes) == 0:
                continue
            crops = engine.crop_text_regions(img, boxes)
            for crop in crops:
                if crop is None or crop.size == 0:
                    continue
                norm = rec.resize_norm_img(crop, rec_max_ratio)
                rec_samples.append({"x": np.expand_dims(norm.astype(np.float32), 0)})
        except Exception as exc:
            print(f"    [!] rec {path.name}: {exc}")

        if i % 10 == 0:
            print(f"    ... {i}/{len(images)}  (det={len(det_samples)}, rec={len(rec_samples)})")

    print(f"[+] Muestras det: {len(det_samples)}   rec: {len(rec_samples)}")
    if not det_samples or not rec_samples:
        print("[X] No se reunieron muestras suficientes.", file=sys.stderr)
        return 1

    Reader = _make_reader()
    args.out.mkdir(parents=True, exist_ok=True)

    report: dict = {
        "format": args.format,
        "per_channel": per_channel,
        "calibration_images": len(images),
        "samples": {"det": len(det_samples), "rec": len(rec_samples)},
        "models": [],
    }

    jobs = [
        ("det", MODELS_DIR / args.det, det_samples),
        ("rec", MODELS_DIR / args.rec, rec_samples),
    ]

    for label, src, samples in jobs:
        if not src.exists():
            print(f"[!] Falta {src.name}, se omite.")
            continue
        dst = args.out / f"{src.stem}_int8.onnx"
        print(f"[+] Cuantizando {label}: {src.name} -> {dst.name} ({len(samples)} muestras)")
        info = _quantize(src, dst, Reader(samples), quant_format, per_channel)
        info["task"] = label
        report["models"].append(info)
        ratio = info["src_mb"] / max(info["dst_mb"], 1e-6)
        print(
            f"    OK  {info['src_mb']} MB -> {info['dst_mb']} MB "
            f"({ratio:.1f}x mas pequeno)  en {info['seconds']}s"
        )

    manifest = args.out / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n[+] Manifest: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
