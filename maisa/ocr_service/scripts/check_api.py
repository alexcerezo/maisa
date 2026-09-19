#!/usr/bin/env python3
"""Validacion rapida de la API /ocr/text contra los documentos reales.

Imprime, por documento, la escala usada, el ratio de alfanumericos y el numero
de lineas, para confirmar que la escalada automatica no necesita ni una vuelta
extra en los documentos degradados.

Uso (dentro del contenedor, con los PDFs en /tmp/docs):
    python scripts/check_api.py
"""
import json
import sys
import urllib.request
from pathlib import Path

API = "http://localhost:8866/ocr/text"
DOCS = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/docs")


def alnum_ratio(text: str) -> float:
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    return sum(c.isalnum() for c in chars) / len(chars)


def post(path: Path) -> dict:
    boundary = "----check"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
        f"Content-Type: application/pdf\r\n\r\n"
    ).encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        API,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read())


def main() -> int:
    files = sorted(p for p in DOCS.iterdir() if p.suffix.lower() == ".pdf")
    if not files:
        print(f"no hay PDFs en {DOCS}")
        return 1

    ok = True
    for path in files:
        data = post(path)
        print(f"\n=== {path.name}  ({data['elapsed']:.2f} s, {data['pages']} pag) ===")
        for page in data["results"]:
            ratio = alnum_ratio(page["text"])
            extra = ""
            if page.get("attempts"):
                tried = ", ".join(f"{a['scale']}" for a in page["attempts"])
                extra = f"  [escalada: {tried}]"
                ok = False
            print(
                f"  pag {page['page']}: scale={page['scale']} "
                f"lineas={len(page['lines'])} alnum={ratio:.1%} "
                f"({page['elapsed']:.2f} s){extra}"
            )
            if path.name.startswith("fax"):
                for line in page["lines"]:
                    print(f"      - {line['text']}")
    print(f"\n{'OK: ningun documento necesito escalar' if ok else 'AVISO: hubo escalada'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
