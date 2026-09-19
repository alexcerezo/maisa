#!/usr/bin/env bash
# Prueba la API OCR con un archivo local.
# Uso: ./test_ocr.sh <ruta/al/archivo.(png|jpg|webp|pdf)> [--no-boxes]
set -euo pipefail

FILE="${1:?Uso: $0 <archivo> [--no-boxes]}"
URL="${OCR_URL:-http://localhost:8866}"
NO_BOXES="${2:-}"
QUERY="include_boxes=true"
[ "$NO_BOXES" = "--no-boxes" ] && QUERY="include_boxes=false"

if [ ! -f "$FILE" ]; then
  echo "No existe el archivo: $FILE" >&2
  exit 1
fi

echo "Enviando '$FILE' a $URL ..."

RESPONSE=$(curl -sS -X POST -F "file=@${FILE}" "$URL/ocr?${QUERY}")

# Muestra el JSON crudo con jq si esta disponible
if command -v jq >/dev/null 2>&1; then
  echo "$RESPONSE" | jq '.'
  exit 0
fi

echo "$RESPONSE" | python3 -c '
import json, sys

data = json.load(sys.stdin)

if "detail" in data and "lines" not in data:
    print("Error API:", data["detail"], file=sys.stderr)
    sys.exit(1)

print()
print(f"Archivo : {data.get(\"file\")}")
print(f"Paginas : {data.get(\"pages\")}   Tiempo: {data.get(\"elapsed\")}s")
stats = data.get("stats", {})
print(f"Lineas  : {stats.get(\"lines\")}   "
      f"Score medio: {stats.get(\"mean_score\")}   "
      f"Score minimo: {stats.get(\"min_score\")}")

for page in data.get("results", []):
    print()
    print(f"--- {page[\"page\"]}  ({page[\"size\"][\"width\"]}x{page[\"size\"][\"height\"]}, "
          f"{page[\"elapsed\"]}s) ---")
    for line in page["lines"]:
        print(f"  [{line[\"score\"]:.3f}]  {line[\"text\"]}")
'

