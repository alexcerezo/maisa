#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Humo (smoke test) de la API/BFF de Albertitos.
#
#   ./maisa/api/smoke_lan.sh
#   ./maisa/api/smoke_lan.sh --publico
#   ./maisa/api/smoke_lan.sh --base http://albertitos-api:8000
#   ./maisa/api/smoke_lan.sh --pdf maisa/data/facturas/2026-0233-A_catering.pdf --engine local
#   ./maisa/api/smoke_lan.sh --publico --subir
#
# El nombre del fichero es historico: cuando nacio, la API se repartia por la
# LAN. Hoy solo hay dos vias de consumo (Internet y la red Docker), asi que las
# bases a probar se pasan con --base (repetible) o con --publico. La LAN no se
# prueba a proposito: no es una via soportada.
#
# --publico prueba la via de reparto real, que es HTTPS: construye la base como
# https://$PUBLIC_IP.sslip.io, el nombre que atiende el proxy TLS (maisa/proxy).
# Con PUBLIC_BASE=<url> se prueba otra; para la API en claro, --base
# http://$PUBLIC_IP:8010.
#
# Recorre el flujo completo contra un despliegue ya arrancado (Docker o local):
#
#   1. /health y /health/ready en cada base (mongo, ocr y escritura en verde,
#      con sus latencias).
#   2. /api/estadisticas (reparto de decisiones y asientos vigentes).
#   3. /api/facturas?limit=5 -> se queda con un file_id real y pide su detalle
#      (decision, motivos y hechos) y su PDF (content-type, disposition inline
#      y sha256 comparado con el fichero original de `maisa/data/facturas`).
#   4. /api/asientos?limit=5 y /api/snapshots (datos reales de Mongo).
#   5. `/`: el visor si `UI_DIR` tiene `index.html`, o el mensaje JSON
#      informativo si no lo tiene (las dos son respuestas correctas).
#   6. POST /api/ocr con un PDF real (engine=auto|cloud|local), midiendo el
#      tiempo y enseñando un extracto del texto reconocido.
#   7. Solo con --subir: POST /api/facturas (201), el mismo fichero otra vez
#      (200 con duplicado=true), GET /api/expedientes/{file_id} y el PDF
#      servido desde GridFS con su sha256. **Escribe en Mongo**, y por eso es
#      opcional; al final dice como borrar lo que ha dejado.
#
# Solo necesita `curl`. Si hay `jq` se usa para leer los campos; si no, se
# usan expresiones de texto equivalentes. NO escribe ficheros temporales en
# disco (salvo el PDF del paso 7, que va a un temporal del sistema).
#
# Sale con 0 si todo responde como se espera; con 1 si algo falla, diciendo
# QUE fallo, que se esperaba y que se recibio.
# ---------------------------------------------------------------------------
set -euo pipefail

AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FACTURAS_DIR_DEFECTO="$(cd "$AQUI/.." && pwd)/data/facturas"

PUERTO="${API_PORT:-8010}"
BASES=()
PDF=""
ENGINE="auto"
TIMEOUT_HTTP=10
TIMEOUT_OCR=180
SUBIR=0
LOTE=1
QUIERE_PUBLICO=0

uso() {
  sed -n '2,45p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --puerto) PUERTO="${2:?falta el puerto}"; shift 2 ;;
    --base) BASES+=("${2:?falta la URL}"); shift 2 ;;
    --publico) QUIERE_PUBLICO=1; shift ;;
    --pdf) PDF="${2:?falta la ruta del PDF}"; shift 2 ;;
    --engine) ENGINE="${2:?falta el motor}"; shift 2 ;;
    --subir) SUBIR=1; shift ;;
    --lote) LOTE="${2:?falta el numero de lote}"; shift 2 ;;
    --timeout) TIMEOUT_HTTP="${2:?falta el tiempo}"; shift 2 ;;
    -h|--help) uso 0 ;;
    *) echo "argumento no reconocido: $1" >&2; uso 1 ;;
  esac
done

case "$ENGINE" in auto|cloud|local) ;; *) echo "--engine debe ser auto, cloud o local" >&2; exit 2 ;; esac
case "$LOTE" in 1|2) ;; *) echo "--lote debe ser 1 o 2" >&2; exit 2 ;; esac

# La IP publica no se puede deducir del servicio de metadatos de esta region
# (no publica el campo `publicIp`), asi que se pasa por entorno. Para sacarla:
#   oci network public-ip get --private-ip-id <ocid-del-private-ip> \
#       --query 'data."ip-address"' --raw-output        # ver §2.4 del README
if [ "${QUIERE_PUBLICO:-0}" = "1" ]; then
  [ -n "${PUBLIC_IP:-}" ] || {
    echo "--publico necesita saber la IP publica: PUBLIC_IP=<ip> ./smoke_lan.sh --publico" >&2
    echo "(como sacarla: §2.4 del README de la API)" >&2
    exit 2
  }
  # La via publica es HTTPS: delante hay un proxy TLS (maisa/proxy) que pide el
  # certificado para el nombre que lleva la IP embebida. Para probar la API en
  # claro o cualquier otro nombre, pasa la URL entera en PUBLIC_BASE.
  BASES+=("${PUBLIC_BASE:-https://$PUBLIC_IP.sslip.io}")
fi

# Sin --base ni --publico se prueba el loopback, que es donde escucha el
# contenedor en esta maquina. Es diagnostico, no una via de consumo.
[ "${#BASES[@]}" -gt 0 ] || BASES=("http://127.0.0.1:$PUERTO")

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

# Comprobacion de salud: 200, mongo ok, ocr ok y escritura ok (con latencia).
revisar_salud() {
  local etiqueta="$1" base="$2"
  if ! pedir "$base/health"; then
    rojo "$etiqueta /health" "no hay respuesta en $base (¿esta arrancado y publicado?)"
    return
  fi
  [ "$CODIGO" = "200" ] || { rojo "$etiqueta /health" "HTTP $CODIGO (esperaba 200)"; return; }
  local mongo_ok ocr_ok escr_ok lat_mongo lat_ocr lat_escr
  if [ "$TIENE_JQ" = 1 ]; then
    mongo_ok="$(printf '%s' "$CUERPO" | jq -r '.dependencias.mongo.ok')"
    ocr_ok="$(printf '%s' "$CUERPO" | jq -r '.dependencias.ocr.ok')"
    escr_ok="$(printf '%s' "$CUERPO" | jq -r '.dependencias.escritura.ok')"
    lat_mongo="$(printf '%s' "$CUERPO" | jq -r '.dependencias.mongo.latencia_ms')"
    lat_ocr="$(printf '%s' "$CUERPO" | jq -r '.dependencias.ocr.latencia_ms')"
    lat_escr="$(printf '%s' "$CUERPO" | jq -r '.dependencias.escritura.latencia_ms')"
  else
    mongo_ok="$(printf '%s' "$CUERPO" | grep -o '"mongo":{[^}]*"ok":[a-z]*' | grep -o '"ok":[a-z]*' | head -n1 | cut -d: -f2)"
    ocr_ok="$(printf '%s' "$CUERPO" | grep -o '"ocr":{[^}]*"ok":[a-z]*' | grep -o '"ok":[a-z]*' | head -n1 | cut -d: -f2)"
    escr_ok="$(printf '%s' "$CUERPO" | grep -o '"escritura":{[^}]*"ok":[a-z]*' | grep -o '"ok":[a-z]*' | head -n1 | cut -d: -f2)"
    lat_mongo="$(printf '%s' "$CUERPO" | grep -o '"nombre":"mongo","latencia_ms":[0-9.]*' | grep -o '[0-9.]*$')"
    lat_ocr="$(printf '%s' "$CUERPO" | grep -o '"nombre":"ocr","latencia_ms":[0-9.]*' | grep -o '[0-9.]*$')"
    lat_escr="$(printf '%s' "$CUERPO" | grep -o '"nombre":"escritura","latencia_ms":[0-9.]*' | grep -o '[0-9.]*$')"
  fi
  if [ "$mongo_ok" = "true" ] && [ "$ocr_ok" = "true" ] && [ "$escr_ok" = "true" ]; then
    verde "$etiqueta /health" "200 en ${SEGUNDOS}s · mongo ${lat_mongo} ms · ocr ${lat_ocr} ms · escritura ${lat_escr} ms"
  else
    rojo "$etiqueta /health" "HTTP 200 pero mongo=$mongo_ok ocr=$ocr_ok escritura=$escr_ok (esperaba true en las tres)"
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

echo "humo albertitos-api · bases: ${BASES[*]} · OCR engine=$ENGINE"
echo "PDF de prueba: $PDF"
echo
echo "--- 1. salud ------------------------------------------------------------"
BASE="${BASES[0]}"
for base in "${BASES[@]}"; do
  revisar_salud "$(printf '%-22s' "$base")" "$base"
done

echo
echo "--- 2. estadisticas -----------------------------------------------------"
if pedir "$BASE/api/estadisticas"; then
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
  rojo "/api/estadisticas" "sin respuesta en $BASE"
fi

echo
echo "--- 3. facturas ---------------------------------------------------------"
FILE_ID=""
if pedir "$BASE/api/facturas?limit=5"; then
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
  rojo "/api/facturas?limit=5" "sin respuesta en $BASE"
fi

if [ -n "$FILE_ID" ]; then
  if pedir "$BASE/api/facturas/$FILE_ID"; then
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
  URL_PDF="$BASE/api/facturas/$FILE_ID/pdf"
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
if pedir "$BASE/api/asientos?limit=5"; then
  total_a="$(campo "$CUERPO" total)"; devueltas_a="$(campo "$CUERPO" devueltas)"
  if [ "$CODIGO" = "200" ] && [ "$devueltas_a" = "5" ] && [ "${total_a:-0}" -gt 0 ] 2>/dev/null; then
    verde "/api/asientos?limit=5" "200 en ${SEGUNDOS}s · total $total_a · devueltas 5"
  else
    rojo "/api/asientos?limit=5" "HTTP $CODIGO total=$total_a devueltas=$devueltas_a"
  fi
else
  rojo "/api/asientos?limit=5" "sin respuesta (¿Mongo alcanzable y autenticado?)"
fi

if pedir "$BASE/api/snapshots"; then
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
if pedir "$BASE/"; then
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
if pedir "$BASE/api/ocr?engine=$ENGINE" --max-time "$TIMEOUT_OCR" -F "file=@$PDF;type=application/pdf"; then
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
  rojo "POST /api/ocr?engine=$ENGINE" "sin respuesta en $BASE (¿OCR alcanzable por la red compartida?)"
fi

echo
echo "--- 7. subida de una factura nueva (solo con --subir) -------------------"
if [ "$SUBIR" != "1" ]; then
  echo "  (omitido: anade --subir para probar POST /api/facturas; escribe en Mongo)"
else
  # Nombre unico y marcado, para poder borrarlo despues sin dudas.
  NOMBRE="smoke-$(date -u '+%Y%m%dT%H%M%SZ').pdf"
  TEMPORAL="$(mktemp -t smoke-factura-XXXXXX).pdf"
  cp "$PDF" "$TEMPORAL"
  SHA_LOCAL="$(sha256sum "$TEMPORAL" | cut -d' ' -f1)"

  # 7.1 la primera subida crea el expediente
  if pedir "$BASE/api/facturas?lote=$LOTE" --max-time "$TIMEOUT_OCR" -F "file=@$TEMPORAL;type=application/pdf;filename=$NOMBRE"; then
    devuelto="$(campo "$CUERPO" file_id)"
    estado="$(campo "$CUERPO" estado_proceso)"
    if [ "$CODIGO" = "201" ] && [ "$devuelto" = "$NOMBRE" ]; then
      verde "POST /api/facturas" "201 en ${SEGUNDOS}s · file_id $devuelto · estado $estado · lote $LOTE"
    else
      rojo "POST /api/facturas" "HTTP $CODIGO file_id='$devuelto' (esperaba 201 y file_id=$NOMBRE)"
    fi
  else
    rojo "POST /api/facturas" "sin respuesta en $BASE (¿SUBIDAS_HABILITADAS=0?)"
  fi

  # 7.2 el mismo fichero otra vez no debe duplicar nada
  if pedir "$BASE/api/facturas?lote=$LOTE" --max-time "$TIMEOUT_OCR" -F "file=@$TEMPORAL;type=application/pdf;filename=$NOMBRE"; then
    duplicado="$(campo "$CUERPO" duplicado)"
    if [ "$CODIGO" = "200" ] && [ "$duplicado" = "true" ]; then
      verde "POST /api/facturas (repetido)" "200 en ${SEGUNDOS}s · duplicado=true (idempotente por contenido)"
    else
      rojo "POST /api/facturas (repetido)" "HTTP $CODIGO duplicado='$duplicado' (esperaba 200 y duplicado=true)"
    fi
  else
    rojo "POST /api/facturas (repetido)" "sin respuesta"
  fi

  # 7.3 el expediente queda persistido en Mongo
  if pedir "$BASE/api/expedientes/$NOMBRE"; then
    id_exp="$(campo "$CUERPO" _id)"
    if [ "$CODIGO" = "200" ] && [ "$id_exp" = "$NOMBRE" ]; then
      verde "GET /api/expedientes/$NOMBRE" "200 en ${SEGUNDOS}s · persistido en Mongo"
    else
      rojo "GET /api/expedientes/$NOMBRE" "HTTP $CODIGO _id='$id_exp'"
    fi
  else
    rojo "GET /api/expedientes/$NOMBRE" "sin respuesta"
  fi

  # 7.4 el PDF sale de GridFS con los mismos bytes que se subieron
  URL_GRIDFS="$BASE/api/facturas/$NOMBRE/pdf"
  if pedir "$URL_GRIDFS" --output /dev/null; then
    if [ "$CODIGO" != "200" ]; then
      rojo "PDF desde GridFS" "HTTP $CODIGO (esperaba 200)"
    else
      sha_gridfs="$(curl -sS --max-time "$TIMEOUT_HTTP" "$URL_GRIDFS" | sha256sum | cut -d' ' -f1)"
      if [ "$sha_gridfs" = "$SHA_LOCAL" ]; then
        verde "PDF desde GridFS" "200 en ${SEGUNDOS}s · application/pdf inline · sha256 ${sha_gridfs:0:16}… == subido"
      else
        rojo "PDF desde GridFS" "sha256 ${sha_gridfs:0:16}… != subido ${SHA_LOCAL:0:16}…"
      fi
    fi
  else
    rojo "PDF desde GridFS" "sin respuesta"
  fi

  rm -f "$TEMPORAL"
  echo "        Limpieza: borra el expediente de prueba con"
  echo "          docker exec albertitos-mongo sh -c 'mongosh --quiet -u \"\$MONGO_INITDB_ROOT_USERNAME\" -p \"\$MONGO_INITDB_ROOT_PASSWORD\" --authenticationDatabase admin albertitos --eval \"db.expedientes.deleteOne({_id:\\\\\"$NOMBRE\\\\\"})\"'"
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
