#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# smoke_lan.sh - Prueba de humo del OCR por LAN.
#
# Comprueba, en este orden:
#   1. GET /health por localhost (127.0.0.1)   -> el servicio vive aqui.
#   2. GET /health por la IP de LAN            -> el servicio sale a la LAN.
#   3. Motores disponibles (local cargado y nube configurada) desde /health.
#   4. POST /ocr/text con el PDF que se le pase -> mide el tiempo real.
#
# Uso:
#   ./smoke_lan.sh <ruta/al/documento.pdf> [IP_O_HOST]
#
# Variables de entorno:
#   OCR_HOST   IP o host por el que se prueba el acceso "desde fuera".
#              Por defecto se autodetecta la IP de LAN de esta maquina.
#   OCR_PORT   Puerto del servicio. Por defecto 8866.
#   OCR_ENGINE Motor a usar en la prueba de fuego: auto | local | cloud.
#              Por defecto `local`, que no depende de la red ni del token.
#   OCR_TIMEOUT Segundos maximos de espera en el POST. Por defecto 300.
#
# Dependencias: bash, curl. `jq` es opcional (mejora la salida; si no esta,
# se degrada a volcado crudo del JSON). No requiere python.
#
# Salida: exit 0 si las tres comprobaciones pasan; exit 1 con mensaje claro
# en caso contrario. Pensado para pegarlo tal cual en el informe.
# ---------------------------------------------------------------------------
set -euo pipefail

PORT="${OCR_PORT:-8866}"
ENGINE="${OCR_ENGINE:-local}"
TIMEOUT="${OCR_TIMEOUT:-300}"

# --- Utilidades de presentacion -------------------------------------------
rojo()  { printf '\033[31m%s\033[0m\n' "$*"; }
verde() { printf '\033[32m%s\033[0m\n' "$*"; }
info()  { printf '\033[36m==> %s\033[0m\n' "$*"; }

# curl con codigo HTTP y tiempo, sin que `-f` corte el cuerpo del error.
# Devuelve el cuerpo por stdout y "HTTP=<codigo> TIME=<segundos>" por stderr.
pedir() {
  local url="$1"; shift
  curl -sS -m "$TIMEOUT" -o - -w '\n__META__HTTP=%{http_code} TIME=%{time_total}' "$url" "$@"
}

# Extrae un campo plano de un JSON sin depender de jq.
campo() {
  local json="$1" clave="$2"
  printf '%s' "$json" | tr -d '\n' | sed -n "s/.*\"$clave\":\([^,}]*\).*/\1/p" | head -1
}

# --- Argumentos ------------------------------------------------------------
if [ "$#" -lt 1 ]; then
  rojo "Uso: $0 <ruta/al/documento.pdf|png|jpg|webp> [IP_O_HOST]"
  exit 1
fi
FICHERO="$1"
HOST="${2:-${OCR_HOST:-}}"

if [ ! -f "$FICHERO" ]; then
  rojo "ERROR: no existe el fichero '$FICHERO'"
  exit 1
fi

# Autodeteccion de la IP de LAN si no se indica nada: primera IP privada
# no-loopback de las interfaces del host.
if [ -z "$HOST" ]; then
  HOST="$(ip -4 -o addr show scope global 2>/dev/null \
          | awk '{print $4}' | cut -d/ -f1 \
          | grep -E '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)' \
          | head -1 || true)"
fi
if [ -z "$HOST" ]; then
  rojo "ERROR: no he podido autodetectar la IP de LAN. Pasala como 2do argumento."
  exit 1
fi

URL_LOCAL="http://127.0.0.1:${PORT}"
URL_LAN="http://${HOST}:${PORT}"

echo "-------------------------------------------------------------------"
echo " Prueba de humo OCR  |  local=${URL_LOCAL}  lan=${URL_LAN}"
echo "-------------------------------------------------------------------"

# --- 1) /health por localhost ---------------------------------------------
info "1/4  GET ${URL_LOCAL}/health"
SALIDA="$(pedir "${URL_LOCAL}/health" || true)"
META="$(printf '%s' "$SALIDA" | tail -1)"
CUERPO="$(printf '%s' "$SALIDA" | sed '$d')"
CODE="$(printf '%s' "$META" | sed -n 's/.*HTTP=\([0-9]*\).*/\1/p')"
TIME="$(printf '%s' "$META" | sed -n 's/.*TIME=\([0-9.]*\).*/\1/p')"

if [ "${CODE:-000}" != "200" ]; then
  rojo "FALLO: el OCR no responde en localhost (${URL_LOCAL}/health -> HTTP ${CODE:-000})."
  rojo "       El contenedor esta levantado?  docker ps | grep ocr-api"
  exit 1
fi
verde "OK   HTTP 200 en ${TIME}s"
printf '%s\n' "$CUERPO"

ESTADO="$(printf '%s' "$CUERPO" | sed -n 's/.*"status":"\([^"]*\)".*/\1/p' | head -1)"
[ "$ESTADO" = "ok" ] || rojo "AVISO: /health devuelve status='$ESTADO' (no 'ok'); los modelos pueden estar cargando."

# --- 2) /health por la IP de LAN ------------------------------------------
info "2/4  GET ${URL_LAN}/health   (acceso desde fuera de localhost)"
SALIDA_LAN="$(pedir "${URL_LAN}/health" || true)"
META_LAN="$(printf '%s' "$SALIDA_LAN" | tail -1)"
CUERPO_LAN="$(printf '%s' "$SALIDA_LAN" | sed '$d')"
CODE_LAN="$(printf '%s' "$META_LAN" | sed -n 's/.*HTTP=\([0-9]*\).*/\1/p')"
TIME_LAN="$(printf '%s' "$META_LAN" | sed -n 's/.*TIME=\([0-9.]*\).*/\1/p')"

if [ "${CODE_LAN:-000}" != "200" ]; then
  rojo "FALLO: el OCR NO es alcanzable por la LAN (${URL_LAN}/health -> HTTP ${CODE_LAN:-000})."
  rojo "       Causas tipicas: el binding es 127.0.0.1:${PORT} en vez de 0.0.0.0:${PORT},"
  rojo "       o hay un firewall intermedio. Comprueba:"
  rojo "         docker port ocr-api     # debe decir 0.0.0.0:${PORT}"
  exit 1
fi
verde "OK   HTTP 200 en ${TIME_LAN}s  ->  el servicio es alcanzable por LAN"
if [ "$CUERPO" = "$CUERPO_LAN" ]; then
  verde "     Respuesta identica a la de localhost (mismo JSON, byte a byte)"
else
  rojo "AVISO: la respuesta por LAN difiere de la de localhost."
fi

# --- 3) Motores disponibles ------------------------------------------------
info "3/4  Motores disponibles (segun /health)"
# Se trocea el JSON de /health para sacar los dos motores sin necesitar jq.
LOCAL_ENABLED="$(printf '%s' "$CUERPO" | sed -n 's/.*"local":{"enabled":\([a-z]*\).*/\1/p')"
LOCAL_LOADED="$(printf '%s' "$CUERPO" | sed -n 's/.*"local":{"enabled":[a-z]*,"loaded":\([a-z]*\).*/\1/p')"
CLOUD_ENABLED="$(printf '%s' "$CUERPO" | sed -n 's/.*"cloud":{"enabled":\([a-z]*\).*/\1/p')"
CLOUD_TOKEN="$(printf '%s' "$CUERPO" | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')"
CLOUD_CIRCUIT="$(printf '%s' "$CUERPO" | sed -n 's/.*"circuit":"\([^"]*\)".*/\1/p')"
MODOS="$(printf '%s' "$CUERPO" | sed -n 's/.*"engine":"\([^"]*\)".*/\1/p')"

echo "     engine por defecto : ${MODOS:-?}   (auto = nube con respaldo local)"
echo "     motor local        : enabled=${LOCAL_ENABLED:-?} loaded=${LOCAL_LOADED:-?}"
echo "     motor nube (VL)    : enabled=${CLOUD_ENABLED:-?} token=${CLOUD_TOKEN:-?} circuit=${CLOUD_CIRCUIT:-?}"

if [ "${LOCAL_ENABLED:-false}" != "true" ] && [ "${CLOUD_ENABLED:-false}" != "true" ]; then
  rojo "FALLO: no hay ningun motor disponible (ni local ni nube)."
  exit 1
fi

# --- 4) Prueba de fuego con un documento real ------------------------------
info "4/4  POST ${URL_LAN}/ocr/text?engine=${ENGINE}   con '${FICHERO}'"
SALIDA_OCR="$(pedir "${URL_LAN}/ocr/text?engine=${ENGINE}" -X POST -F "file=@${FICHERO}" || true)"
META_OCR="$(printf '%s' "$SALIDA_OCR" | tail -1)"
CUERPO_OCR="$(printf '%s' "$SALIDA_OCR" | sed '$d')"
CODE_OCR="$(printf '%s' "$META_OCR" | sed -n 's/.*HTTP=\([0-9]*\).*/\1/p')"
TIME_OCR="$(printf '%s' "$META_OCR" | sed -n 's/.*TIME=\([0-9.]*\).*/\1/p')"

if [ "${CODE_OCR:-000}" != "200" ]; then
  rojo "FALLO: /ocr/text devolvio HTTP ${CODE_OCR:-000}."
  printf '%s\n' "$CUERPO_OCR" | head -20 >&2
  if [ "$ENGINE" = "cloud" ]; then
    rojo "       Prueba con OCR_ENGINE=local para descartar el token/red de la nube."
  fi
  exit 1
fi

verde "OK   HTTP 200 en ${TIME_OCR}s   (tiempo total de pared medido con curl -w)"

if command -v jq >/dev/null 2>&1; then
  echo "     fichero     : $(printf '%s' "$CUERPO_OCR" | jq -r '.file // "?"')"
  echo "     motor usado : $(printf '%s' "$CUERPO_OCR" | jq -r '.engine // "?"')"
  echo "     paginas     : $(printf '%s' "$CUERPO_OCR" | jq -r '.pages // "?"')"
  echo "     elapsed     : $(printf '%s' "$CUERPO_OCR" | jq -r '.elapsed // "?"')s"
  echo "     stats       : $(printf '%s' "$CUERPO_OCR" | jq -c '.stats // {}')"
  echo "     --- texto reconocido (primeras 12 lineas) ---"
  printf '%s' "$CUERPO_OCR" | jq -r '.results[]?.text // empty' | head -12 | sed 's/^/     /'
else
  echo "     (jq no instalado: volcado crudo, recortado)"
  printf '%s' "$CUERPO_OCR" | head -c 800
  echo
fi

echo "-------------------------------------------------------------------"
verde "RESULTADO: OCR operativo por localhost Y por LAN (${HOST})."
echo "-------------------------------------------------------------------"
