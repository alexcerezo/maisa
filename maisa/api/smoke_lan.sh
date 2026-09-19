#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Humo (smoke test) de la API/BFF de Albertitos.
#
#   ./maisa/api/smoke_lan.sh
#   ./maisa/api/smoke_lan.sh --lan-ip 10.0.0.75
#   ./maisa/api/smoke_lan.sh --pdf maisa/data/facturas/2026-0233-A_catering.pdf
#   ./maisa/api/smoke_lan.sh --engine local
#
# Recorre el flujo completo contra un despliegue ya arrancado (Docker o
# local):
#
#   1. /health y /health/ready por 127.0.0.1 Y por la IP de LAN (mongo y ocr
#      en verde, con sus latencias).
#   2. /api/estadisticas (reparto de decisiones y asientos vigentes).
#   3. /api/facturas?limit=5 -> se queda con un file_id real y pide su detalle
#      (decision, motivos y hechos).
#   4. El PDF de esa factura: content-type, disposition inline y sha256
#      comparado con el fichero original de `maisa/data/facturas`.
#   5. /api/asientos?limit=5 y /api/snapshots (datos reales de Mongo).
#   6. `/`: el visor si `UI_DIR` tiene `index.html`, o el mensaje JSON
#      informativo si no lo tiene (las dos son respuestas correctas).
#   7. POST /api/ocr con un PDF real (engine=auto|cloud|local), midiendo el
#      tiempo y enseñando un extracto del texto reconocido.
#
# Solo necesita `curl`. Si hay `jq` se usa para leer los campos; si no, se
# usan expresiones de texto equivalentes. NO escribe ficheros temporales.
#
# Sale con 0 si todo responde como se espera; con 1 si algo falla, diciendo
# QUE fallo, que se esperaba y que se recibio.
# ---------------------------------------------------------------------------
set -euo pipefail

AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FACTURAS_DIR_DEFECTO="$(cd "$AQUI/.." && pwd)/data/facturas"

PUERTO="${API_PORT:-8010}"
IP_LAN="${LAN_IP:-}"
PDF=""
ENGINE="auto"
TIMEOUT_HTTP=10
TIMEOUT_OCR=180

uso() {
  sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --puerto) PUERTO="${2:?falta el puerto}"; shift 2 ;;
    --lan-ip) IP_LAN="${2:?falta la IP}"; shift 2 ;;
    --pdf) PDF="${2:?falta la ruta del PDF}"; shift 2 ;;
    --engine) ENGINE="${2:?falta el motor}"; shift 2 ;;
    --timeout) TIMEOUT_HTTP="${2:?falta el tiempo}"; shift 2 ;;
    -h|--help) uso 0 ;;
    *) echo "argumento no reconocido: $1" >&2; uso 1 ;;
  esac
done

case "$ENGINE" in auto|cloud|local) ;; *) echo "--engine debe ser auto, cloud o local" >&2; exit 2 ;; esac

# IP de LAN por defecto: la primera direccion global de esta maquina.
if [ -z "$IP_LAN" ]; then
  IP_LAN="$(hostname -I 2>/dev/null | awk '{print $1}')"
fi
[ -n "$IP_LAN" ] || { echo "no he podido deducir la IP de LAN: usa --lan-ip <IP>" >&2; exit 2; }

# PDF de prueba: el que se pase, o el primero que haya en FACTURAS_DIR.
if [ -z "$PDF" ]; then
  PDF="$(find "$FACTURAS_DIR_DEFECTO" -maxdepth 1 -name '*.pdf' 2>/dev/null | sort | head -n1 || true)"
fi
[ -n "$PDF" ] && [ -f "$PDF" ] || {
  echo "no encuentro ningun PDF de prueba (buscaba en $FACTURAS_DIR_DEFECTO); usa --pdf <ruta>" >&2
  exit 2
}

TIENE_JQ=0
command -v jq >/dev/null 2>&1 && TIENE_JQ=1
[ "${SMOKE_SIN_JQ:-0}" = "1" ] && TIENE_JQ=0   # SMOKE_SIN_JQ=1 prueba la ruta sin jq

FALLOS=0
OKS=0
DETALLES_FALLO=()

verde()  { printf '  \033[32mOK\033[0m    %-46s %s\n' "$1" "$2"; OKS=$((OKS + 1)); }
rojo()   { printf '  \033[31mFALLO\033[0m %-46s %s\n' "$1" "$2"; FALLOS=$((FALLOS + 1)); DETALLES_FALLO+=("$1: $2"); }

# Campo de un JSON: usa jq si esta, y si no una extraccion de texto. Busca
# primero dentro de "por_resultado" (para PAGAR/NO_PAGAR/ESCALAR) y si no en
# el nivel de arriba.
#   campo <json> <nombre> [indice-de-coincidencia]
campo() {
  local json="$1" nombre="$2" indice="${3:-1}"
  if [ "$TIENE_JQ" = 1 ]; then
    printf '%s' "$json" | jq -r --arg n "$nombre" \
      'if (type == "object" and has("por_resultado") and (.por_resultado | type == "object") and (.por_resultado | has($n)))
         then (.por_resultado[$n] | tostring)
       elif (type == "object" and has($n)) then (.[$n] | tostring)
       else "" end' 2>/dev/null || true
    return
  fi
  printf '%s' "$json" \
    | grep -o "\"$nombre\"[[:space:]]*:[[:space:]]*\(\"[^\"]*\"\|[0-9.]\+\|true\|false\|null\)" \
    | sed -n "${indice}p" \
    | sed -E "s/^\"$nombre\"[[:space:]]*:[[:space:]]*//; s/^\"//; s/\"$//" || true
}

# Pide una URL y deja el cuerpo en CUERPO, el codigo en CODIGO, el tiempo en
# SEGUNDOS y el content-type en TIPO.
CUERPO=""
CODIGO=""
SEGUNDOS=""
TIPO=""
pedir() {
  local url="$1"; shift
  local salida
  if ! salida="$(curl -sS --max-time "$TIMEOUT_HTTP" -w $'\n%{http_code} %{time_total} %{content_type}' "$@" "$url" 2>&1)"; then
    CUERPO=""; CODIGO="000"; SEGUNDOS="0"; TIPO=""
    printf '%s\n' "$salida" | grep -vE '^[0-9]{3} ' >&2 || true
    return 1
  fi
  local ultima="${salida##*$'\n'}"
  CUERPO="${salida%$'\n'*}"
  CODIGO="${ultima%% *}"
  SEGUNDOS="$(printf '%s' "$ultima" | awk '{print $2}')"
  TIPO="${ultima#* }"; TIPO="${TIPO#* }"
  return 0
}

# Valor de una cabecera de respuesta: cabecera <url> <nombre-de-cabecera>
cabecera() {
  curl -sS --max-time "$TIMEOUT_HTTP" -D - -o /dev/null "$1" 2>/dev/null \
    | grep -i "^$2:" | head -n1 | tr -d '\r' | sed -E "s/^[^:]+:[[:space:]]*//"
}

# Comprobacion de salud: 200, mongo ok y ocr ok (con latencia).
revisar_salud() {
  local etiqueta="$1" base="$2"
  if ! pedir "$base/health"; then
    rojo "$etiqueta /health" "no hay respuesta en $base (¿esta arrancado y publicado?)"
    return
  fi
  [ "$CODIGO" = "200" ] || { rojo "$etiqueta /health" "HTTP $CODIGO (esperaba 200)"; return; }
  local mongo_ok ocr_ok lat_mongo lat_ocr
  if [ "$TIENE_JQ" = 1 ]; then
    mongo_ok="$(printf '%s' "$CUERPO" | jq -r '.dependencias.mongo.ok')"
    ocr_ok="$(printf '%s' "$CUERPO" | jq -r '.dependencias.ocr.ok')"
    lat_mongo="$(printf '%s' "$CUERPO" | jq -r '.dependencias.mongo.latencia_ms')"
    lat_ocr="$(printf '%s' "$CUERPO" | jq -r '.dependencias.ocr.latencia_ms')"
  else
    mongo_ok="$(printf '%s' "$CUERPO" | grep -o '"mongo":{[^}]*"ok":[a-z]*' | grep -o '"ok":[a-z]*' | head -n1 | cut -d: -f2)"
    ocr_ok="$(printf '%s' "$CUERPO" | grep -o '"ocr":{[^}]*"ok":[a-z]*' | grep -o '"ok":[a-z]*' | head -n1 | cut -d: -f2)"
    lat_mongo="$(printf '%s' "$CUERPO" | grep -o '"nombre":"mongo","latencia_ms":[0-9.]*' | grep -o '[0-9.]*$')"
    lat_ocr="$(printf '%s' "$CUERPO" | grep -o '"nombre":"ocr","latencia_ms":[0-9.]*' | grep -o '[0-9.]*$')"
  fi
  if [ "$mongo_ok" = "true" ] && [ "$ocr_ok" = "true" ]; then
    verde "$etiqueta /health" "200 en ${SEGUNDOS}s · mongo ${lat_mongo} ms · ocr ${lat_ocr} ms"
  else
    rojo "$etiqueta /health" "HTTP 200 pero mongo=$mongo_ok ocr=$ocr_ok (esperaba true/true)"
  fi

  if ! pedir "$base/health/ready"; then
    rojo "$etiqueta /health/ready" "sin respuesta"
    return
  fi
  local listo; listo="$(campo "$CUERPO" listo)"
  if [ "$CODIGO" = "200" ] && [ "$listo" = "true" ]; then
    verde "$etiqueta /health/ready" "200 en ${SEGUNDOS}s · listo=true"
  else
    rojo "$etiqueta /health/ready" "HTTP $CODIGO listo=$listo (esperaba 200 y listo=true)"
  fi
}

echo "humo albertitos-api · 127.0.0.1:$PUERTO y $IP_LAN:$PUERTO · OCR engine=$ENGINE"
echo "PDF de prueba: $PDF"
echo
echo "--- 1. salud ------------------------------------------------------------"
revisar_salud "local" "http://127.0.0.1:$PUERTO"
revisar_salud "LAN  " "http://$IP_LAN:$PUERTO"

echo
echo "--- 2. estadisticas -----------------------------------------------------"
BASE_LAN="http://$IP_LAN:$PUERTO"
if pedir "$BASE_LAN/api/estadisticas"; then
  total="$(campo "$CUERPO" total)"; pagar="$(campo "$CUERPO" PAGAR)"
  nopagar="$(campo "$CUERPO" NO_PAGAR)"; escalar="$(campo "$CUERPO" ESCALAR)"
  asientos="$(campo "$CUERPO" asientos_vigentes)"
  if [ "$CODIGO" != "200" ]; then
    rojo "/api/estadisticas" "HTTP $CODIGO (esperaba 200)"
  elif [ "${total:-0}" -le 0 ] 2>/dev/null; then
    rojo "/api/estadisticas" "total=$total (esperaba > 0)"
  elif [ $(( ${pagar:-0} + ${nopagar:-0} + ${escalar:-0} )) -ne "${total:-0}" ] 2>/dev/null; then
    rojo "/api/estadisticas" "el reparto no cuadra: $pagar+$nopagar+$escalar != $total"
  else
    verde "/api/estadisticas" "200 en ${SEGUNDOS}s · PAGAR $pagar / NO_PAGAR $nopagar / ESCALAR $escalar (total $total) · asientos_vigentes $asientos"
  fi
else
  rojo "/api/estadisticas" "sin respuesta en $BASE_LAN"
fi

echo
echo "--- 3. facturas ---------------------------------------------------------"
FILE_ID=""
if pedir "$BASE_LAN/api/facturas?limit=5"; then
  total_f="$(campo "$CUERPO" total)"
  devueltas="$(campo "$CUERPO" devueltas)"
  if [ "$TIENE_JQ" = 1 ]; then
    FILE_ID="$(printf '%s' "$CUERPO" | jq -r '.items[0].file_id // empty')"
  else
    FILE_ID="$(printf '%s' "$CUERPO" | grep -o '"file_id":"[^"]*"' | head -n1 | cut -d'"' -f4)"
  fi
  if [ "$CODIGO" = "200" ] && [ "$devueltas" = "5" ] && [ -n "$FILE_ID" ]; then
    verde "/api/facturas?limit=5" "200 en ${SEGUNDOS}s · total $total_f · devueltas 5 · primer file_id $FILE_ID"
  else
    rojo "/api/facturas?limit=5" "HTTP $CODIGO total=$total_f devueltas=$devueltas file_id='$FILE_ID'"
  fi
else
  rojo "/api/facturas?limit=5" "sin respuesta en $BASE_LAN"
fi

if [ -n "$FILE_ID" ]; then
  if pedir "$BASE_LAN/api/facturas/$FILE_ID"; then
    resultado="$(campo "$CUERPO" resultado)"
    motivos="$(printf '%s' "$CUERPO" | grep -c '"motivos"' || true)"
    hechos="$(printf '%s' "$CUERPO" | grep -c '"hechos"' || true)"
    if [ "$CODIGO" = "200" ] && [ -n "$resultado" ] && [ "$motivos" -ge 1 ] && [ "$hechos" -ge 1 ]; then
      verde "/api/facturas/$FILE_ID" "200 en ${SEGUNDOS}s · resultado=$resultado · con motivos y hechos"
    else
      rojo "/api/facturas/$FILE_ID" "HTTP $CODIGO resultado='$resultado' motivos=$motivos hechos=$hechos"
    fi
  else
    rojo "/api/facturas/$FILE_ID" "sin respuesta"
  fi

  # El PDF: se compara el sha256 con el original de maisa/data/facturas.
  ORIGINAL="$FACTURAS_DIR_DEFECTO/$FILE_ID"
  URL_PDF="$BASE_LAN/api/facturas/$FILE_ID/pdf"
  if ! pedir "$URL_PDF" --output /dev/null; then
    rojo "PDF de $FILE_ID" "sin respuesta"
  elif [ "$CODIGO" != "200" ]; then
    rojo "PDF de $FILE_ID" "HTTP $CODIGO (esperaba 200)"
  else
    disposicion="$(cabecera "$URL_PDF" content-disposition)"
    longitud="$(cabecera "$URL_PDF" content-length)"
    problema=""
    case "$TIPO" in application/pdf*) ;; *) problema="content-type '$TIPO' (esperaba application/pdf)" ;; esac
    if [ -z "$problema" ]; then
      case "$disposicion" in inline*) ;; *) problema="content-disposition '$disposicion' (esperaba inline)" ;; esac
    fi
    if [ -z "$problema" ] && [ -z "$longitud" ]; then
      problema="sin content-length"
    fi
    if [ -n "$problema" ]; then
      rojo "PDF de $FILE_ID" "$problema"
    else
      sha_servido="$(curl -sS --max-time "$TIMEOUT_HTTP" "$URL_PDF" | sha256sum | cut -d' ' -f1)"
      if [ -f "$ORIGINAL" ]; then
        sha_original="$(sha256sum "$ORIGINAL" | cut -d' ' -f1)"
        if [ "$sha_servido" = "$sha_original" ]; then
          verde "PDF de $FILE_ID" "200 en ${SEGUNDOS}s · application/pdf inline · ${longitud} B · sha256 ${sha_servido:0:16}… == original"
        else
          rojo "PDF de $FILE_ID" "sha256 servido ${sha_servido:0:16}… != original ${sha_original:0:16}…"
        fi
      else
        verde "PDF de $FILE_ID" "200 en ${SEGUNDOS}s · application/pdf inline · ${longitud} B · sha256 ${sha_servido:0:16}… (sin original con que comparar)"
      fi
    fi
  fi
fi

echo
echo "--- 4. datos de Mongo ---------------------------------------------------"
if pedir "$BASE_LAN/api/asientos?limit=5"; then
  total_a="$(campo "$CUERPO" total)"; devueltas_a="$(campo "$CUERPO" devueltas)"
  if [ "$CODIGO" = "200" ] && [ "$devueltas_a" = "5" ] && [ "${total_a:-0}" -gt 0 ] 2>/dev/null; then
    verde "/api/asientos?limit=5" "200 en ${SEGUNDOS}s · total $total_a · devueltas 5"
  else
    rojo "/api/asientos?limit=5" "HTTP $CODIGO total=$total_a devueltas=$devueltas_a"
  fi
else
  rojo "/api/asientos?limit=5" "sin respuesta (¿Mongo alcanzable y autenticado?)"
fi

if pedir "$BASE_LAN/api/snapshots"; then
  total_s="$(campo "$CUERPO" total)"
  if [ "$CODIGO" = "200" ] && [ "${total_s:-0}" -ge 1 ] 2>/dev/null; then
    verde "/api/snapshots" "200 en ${SEGUNDOS}s · $total_s snapshot(s)"
  else
    rojo "/api/snapshots" "HTTP $CODIGO total=$total_s (esperaba >= 1)"
  fi
else
  rojo "/api/snapshots" "sin respuesta"
fi

echo
echo "--- 5. visor estatico ---------------------------------------------------"
if pedir "$BASE_LAN/"; then
  # `/` sirve el visor si UI_DIR tiene index.html y, si no, un mensaje JSON
  # informativo: las dos respuestas son correctas.
  case "$TIPO" in
    text/html*) verde "/ (visor)" "200 en ${SEGUNDOS}s · text/html · ${#CUERPO} B" ;;
    application/json*) verde "/ (sin visor)" "200 en ${SEGUNDOS}s · JSON informativo (UI_DIR sin index.html)" ;;
    *) rojo "/ (visor)" "content-type '$TIPO' (esperaba text/html o application/json)" ;;
  esac
else
  rojo "/ (visor)" "sin respuesta"
fi

echo
echo "--- 6. OCR encadenado con el microservicio ------------------------------"
TAMANO="$(stat -c '%s' "$PDF" 2>/dev/null || wc -c < "$PDF")"
if pedir "$BASE_LAN/api/ocr?engine=$ENGINE" --max-time "$TIMEOUT_OCR" -F "file=@$PDF;type=application/pdf"; then
  motor="$(campo "$CUERPO" motor)"
  segundos_ocr="$(campo "$CUERPO" segundos_ocr)"
  if [ "$CODIGO" = "200" ] && [ -n "$motor" ]; then
    verde "POST /api/ocr?engine=$ENGINE" "200 en ${SEGUNDOS}s (ocr ${segundos_ocr}s) · motor=$motor · ${TAMANO} B enviados"
    texto="$(campo "$CUERPO" texto)"
    [ -n "$texto" ] || rojo "POST /api/ocr texto" "la respuesta no traia texto reconocido"
    echo "        extracto: $(printf '%s' "$texto" | tr '\n' ' ' | cut -c1-160)…"
  else
    rojo "POST /api/ocr?engine=$ENGINE" "HTTP $CODIGO motor='$motor'"
    printf '%s\n' "$CUERPO" | head -c 400
  fi
else
  rojo "POST /api/ocr?engine=$ENGINE" "sin respuesta en $BASE_LAN (¿OCR alcanzable por la red compartida?)"
fi

echo
echo "=========================================================================="
if [ "$FALLOS" -eq 0 ]; then
  echo "TODO OK: $OKS comprobaciones en verde."
  exit 0
fi
echo "FALLOS: $FALLOS de $((OKS + FALLOS)) comprobaciones."
for detalle in "${DETALLES_FALLO[@]}"; do
  echo "  - $detalle"
done
echo "Pistas: ¿esta el contenedor arriba y healthy? (docker ps) ¿el puerto es $PUERTO?"
echo "        ¿mongo y el OCR responden? (GET /health lo dice dependencia a dependencia)"
exit 1
