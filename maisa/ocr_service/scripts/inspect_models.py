"""
Analiza los modelos ONNX de RapidOCR: recuento de operadores y formas de E/S.

Sirve para decidir la estrategia de cuantizacion:
  - Si dominan Conv  -> hace falta cuantizacion ESTATICA (con calibracion).
  - Si dominan MatMul/Gemm -> basta cuantizacion DINAMICA (sin calibracion).
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

import onnx

MODELS_DIR = Path(
    "/usr/local/lib/python3.12/site-packages/rapidocr/models"
)

TARGETS = [
    ("DET mobile", "ch_PP-OCRv5_det_mobile.onnx"),
    ("DET server", "ch_PP-OCRv5_det_server.onnx"),
    ("REC mobile", "ch_PP-OCRv5_rec_mobile.onnx"),
    ("REC server", "ch_PP-OCRv5_rec_server.onnx"),
    ("CLS mobile", "ch_ppocr_mobile_v2.0_cls_mobile.onnx"),
]


def _dim(dim) -> str:
    if dim.HasField("dim_value"):
        return str(dim.dim_value)
    return dim.dim_param or "?"


def describe(label: str, path: Path) -> None:
    model = onnx.load(str(path), load_external_data=False)
    graph = model.graph

    ops = collections.Counter(node.op_type for node in graph.node)
    total = sum(ops.values()) or 1
    conv = ops.get("Conv", 0)
    matmul = ops.get("MatMul", 0) + ops.get("Gemm", 0)
    size_mb = path.stat().st_size / 1e6

    print(f"\n{label}  ({path.name}, {size_mb:.1f} MB, {len(graph.node)} nodos)")
    print(f"  Conv        : {conv:4d}  ({conv / total * 100:5.1f}%)")
    print(f"  MatMul/Gemm : {matmul:4d}  ({matmul / total * 100:5.1f}%)")

    top = ", ".join(f"{op}={n}" for op, n in ops.most_common(6))
    print(f"  Top ops     : {top}")

    for value in graph.input:
        shape = [_dim(d) for d in value.type.tensor_type.shape.dim]
        print(f"  input  {value.name:12s} {shape}")
    for value in graph.output:
        shape = [_dim(d) for d in value.type.tensor_type.shape.dim]
        print(f"  output {value.name:12s} {shape}")

    bottleneck = "Conv" if conv > matmul else "MatMul/Gemm"
    strategy = "ESTATICA (con calibracion)" if conv > matmul else "DINAMICA (sin calibracion)"
    print(f"  -> Cuello de botella: {bottleneck}  =>  cuantizacion {strategy}")


def main() -> int:
    print(f"Directorio de modelos: {MODELS_DIR}")
    found = False
    for label, filename in TARGETS:
        path = MODELS_DIR / filename
        if not path.exists():
            print(f"\n{label}: no presente ({filename})")
            continue
        found = True
        describe(label, path)
    if not found:
        print("No se encontro ningun modelo.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
