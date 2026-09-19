#!/usr/bin/env python3
"""
Renderiza una pagina como arte ASCII a partir de la densidad de tinta.

Sirve para inspeccionar la ESTRUCTURA de un documento (donde hay texto, donde
hay ruido, si esta torcido) sin necesidad de ver la imagen.

Uso:
    python ascii_view.py /tmp/docs/fax.pdf [--width 110] [--page 0]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

RAMP = " .:-=+*#%@"  # de menos a mas tinta


def load_gray(path: Path, scale: float, page: int) -> np.ndarray:
    if path.suffix.lower() == ".pdf":
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(path))
        return np.asarray(pdf[page].render(scale=scale).to_pil().convert("L"))
    from PIL import Image

    return np.asarray(Image.open(path).convert("L"))


def render(gray: np.ndarray, width: int, gamma: float = 0.55) -> list[str]:
    h, w = gray.shape
    # Los caracteres del terminal son ~2x mas altos que anchos.
    rows = max(1, int(round(width * (h / w) / 2.0)))
    ink = 255.0 - gray.astype(np.float32)

    # Reescalado por bloques (media) a la rejilla de caracteres.
    ys = np.linspace(0, h, rows + 1).astype(int)
    xs = np.linspace(0, w, width + 1).astype(int)
    grid = np.zeros((rows, width), dtype=np.float32)
    for i in range(rows):
        for j in range(width):
            block = ink[ys[i] : ys[i + 1], xs[j] : xs[j + 1]]
            grid[i, j] = block.mean() if block.size else 0.0

    peak = grid.max()
    if peak > 0:
        grid = (grid / peak) ** gamma  # realza la tinta debil
    idx = np.clip((grid * (len(RAMP) - 1)).round().astype(int), 0, len(RAMP) - 1)
    return ["".join(RAMP[k] for k in row) for row in idx]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path)
    ap.add_argument("--width", type=int, default=110)
    ap.add_argument("--scale", type=float, default=2.0)
    ap.add_argument("--page", type=int, default=0)
    ap.add_argument("--gamma", type=float, default=0.55)
    ap.add_argument("--crop", type=int, nargs=4, metavar=("X0", "Y0", "X1", "Y1"),
                    help="Recorta una region (coordenadas en px de la imagen renderizada).")
    args = ap.parse_args()

    gray = load_gray(args.path, args.scale, args.page)
    if args.crop:
        x0, y0, x1, y1 = args.crop
        gray = gray[y0:y1, x0:x1]

    print(f"# {args.path.name}  {gray.shape[1]}x{gray.shape[0]} px")
    print(f"# escala de grises: min={gray.min()} mediana={int(np.median(gray))} max={gray.max()}")
    hist = np.bincount(gray.ravel(), minlength=256)
    tot = gray.size
    print(f"# % pixeles oscuros (<64): {100.0 * hist[:64].sum() / tot:.2f}%"
          f"  claros (>192): {100.0 * hist[192:].sum() / tot:.2f}%")
    print()
    for line in render(gray, args.width, args.gamma):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
