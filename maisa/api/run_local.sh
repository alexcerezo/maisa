#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Arranque de la API/BFF de Albertitos en la MAQUINA ANFITRIONA (sin Docker).
#
#   ./maisa/api/run_local.sh                # http://0.0.0.0:8010
#   API_PORT=8020 ./maisa/api/run_local.sh  # otro puerto
#   RECARGA=1 ./maisa/api/run_local.sh      # autorecarga al editar el codigo
#
# Que hace:
#   1. Carga `maisa/.env` (credenciales de Mongo, que no se versiona) y, si
#      existe, `maisa/api/.env` (configuracion de la API) por encima.
#   2. Rellena los valores locales: en el anfitrion, Mongo y el OCR viven en
#      127.0.0.1, no en el DNS interno de Docker.
#   3. Lanza uvicorn con el entorno ya resuelto.
#
# NO escribe en ningun sitio: los datos se montan en modo lectura.
# ---------------------------------------------------------------------------
set -euo pipefail

AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$AQUI/../.." && pwd)"
MAISA="$REPO/maisa"

# --- 1. Carga de .env sin `source` -----------------------------------------
# `source` NO sirve aqui: la URI de Mongo lleva `&` sin comillas y bash corta la
# linea en el ampersand, dejando MONGO_URI sin definir. Se parsea linea a linea
# y se exporta solo lo que tiene forma de variable de entorno.
cargar_env() {
  local fichero="$1" linea clave valor
  [ -f "$fichero" ] || return 0
  while IFS= read -r linea || [ -n "$linea" ]; do
    linea="${linea%$'\r'}"
    case "$linea" in ''|'#'*) continue ;; esac
    case "$linea" in *=*) ;; *) continue ;; esac
    clave="${linea%%=*}"
    valor="${linea#*=}"
    case "$clave" in *[!A-Za-z0-9_]*) continue ;; esac
    # Comillas envolventes opcionales: se quitan y el resto va literal.
    case "$valor" in
      \"*\") valor="${valor#\"}"; valor="${valor%\"}" ;;
      \'*\') valor="${valor#\'}"; valor="${valor%\'}" ;;
    esac
    export "$clave=$valor"
  done < "$fichero"
}

cargar_env "$MAISA/.env"
cargar_env "$AQUI/.env"

# --- 2. Valores por defecto para una ejecucion local -----------------------
# El defecto del codigo apunta a los DNS de Docker (`mongo`, `ocr-api`); desde
# el anfitrion hay que hablar con 127.0.0.1.
# La URI NO se monta aqui: la construye `app/config.py` a partir de las piezas
# (usuario/contrasena de maisa/.env, con la contrasena codificada para URL).
# Solo hay que corregir el host, que en Docker es el DNS `mongo`.
export MONGO_HOST="${MONGO_HOST:-127.0.0.1}"
export MONGO_DB="${MONGO_DB:-albertitos}"
export MONGO_TIMEOUT_MS="${MONGO_TIMEOUT_MS:-1500}"
if [ -n "${MONGO_URI:-}" ]; then
  echo "aviso: MONGO_URI definida en el entorno; se usa tal cual (sin piezas)." >&2
fi

case "${OCR_URL:-}" in
  ''|*//ocr-api:*) export OCR_URL="http://127.0.0.1:8866" ;;
  *) export OCR_URL="${OCR_URL%/}" ;;
esac
export ERP_URL="${ERP_URL:-http://127.0.0.1:8009}"

export OUTPUTS_DIR="${OUTPUTS_DIR:-$MAISA/outputs}"
export FACTURAS_DIR="${FACTURAS_DIR:-$MAISA/data/facturas}"
# `dist` es el BUILD del panel (`npm run build`). Apuntando a `$MAISA/ui`, `/`
# serviria la plantilla de Vite sin construir y el navegador daria una pagina en
# blanco, porque el `index.html` del fuente tambien existe.
export UI_DIR="${UI_DIR:-$MAISA/ui/dist}"
export API_PORT="${API_PORT:-8010}"
export API_HOST="${API_HOST:-0.0.0.0}"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"

# --- 3. Entorno virtual ----------------------------------------------------
PYTHON="${PYTHON:-$REPO/.venv-api/bin/python}"
if [ ! -x "$PYTHON" ]; then
  cat >&2 <<FIN
No encuentro el entorno virtual en $PYTHON

Crealo con:
  uv venv .venv-api --python 3.12
  uv pip install --python .venv-api/bin/python -r maisa/api/requirements.txt -r maisa/api/requirements-dev.txt

O indica otro interprete:  PYTHON=/ruta/al/python $0
FIN
  exit 1
fi

# --- Resumen (sin credenciales) -------------------------------------------
# URI solo para el resumen: si el usuario la ha dado se sanea; si no, se
# reconstruye con las piezas y tambien se sanea antes de imprimirla.
URI_RESUMEN="${MONGO_URI:-mongodb://${MONGO_APP_USER:+$MONGO_APP_USER@}${MONGO_HOST}:${MONGO_PORT:-27017}/${MONGO_DB}}"
URI_SANEADA="$(printf '%s' "$URI_RESUMEN" | sed -E 's#://[^@/]*@#://***@#')"
cat <<FIN
albertitos-api (local)
  escucha        : http://${API_HOST}:${API_PORT}   (docs en /docs)
  mongo          : ${URI_SANEADA}  (db=${MONGO_DB}, solo lectura)
  ocr            : ${OCR_URL}
  outputs        : ${OUTPUTS_DIR}
  facturas       : ${FACTURAS_DIR}
  visor (ui)     : ${UI_DIR}
  api key        : $([ -n "${API_KEY:-}" ] && echo "configurada" || echo "NO configurada: modo abierto")
FIN

# --- 4. Arranque -----------------------------------------------------------
cd "$AQUI"
ARGS=(app.main:app --host "$API_HOST" --port "$API_PORT" --log-level "${LOG_LEVEL,,}")
if [ -n "${RECARGA:-}" ]; then
  ARGS+=(--reload --reload-dir "$AQUI/app")
fi
exec "$PYTHON" -m uvicorn "${ARGS[@]}"
