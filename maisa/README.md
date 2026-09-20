# maisa

Estructura de proyecto según `spec_y_plan.md`. Conciliación a tres bandas
(PDF ↔ ERP ↔ Excel) para decidir `PAGAR` / `NO_PAGAR` / `ESCALAR`.

## Dónde está el motor que se ejecuta

> **El motor vivo es `motor/` (Python). El binario Rust de `src/` es legado.**

El motor Rust tiene las reglas implementadas y sus 57 tests en verde, pero el
lector del maestro (`src/excel.rs`) y la observabilidad (`src/obs.rs`) son
placeholders de dos líneas, y su modo lote está declarado como *"lo que se puede
probar sin OCR, sin ERP y sin Excel"*. Por eso `outputs/outcomes.jsonl` nunca
llegó a llenarse: solo se ha ejecutado contra el lote de ejemplo de 10 líneas.
Para entregar el domingo hacía falta un motor que **ejecute sobre el corpus
real**, así que se integró `motor/`, que resuelve las 500 facturas en ~3,6 s y
sin red.

- `motor/README.md` — cómo se ejecuta y qué decide.
- `motor/docs/albertitos_plan.md` — el plan de entrega: arquitectura, ADRs y
  trade-offs, escalabilidad y resiliencia.
- `TRASPASO.md` — se mantiene íntegro como documentación de ese diseño: sus
  contratos (los tres fallos distintos del ERP, el enum cerrado de eventos, la
  trampa de `Decimal` en BSON, el XML en ISO-8859-1) siguen siendo válidos y
  están implementados en `motor/`.

El Rust **no se borra**: no compila sobre el corpus y reescribirlo no cabía en
el plazo, pero su análisis de dominio es el que fijó la semántica de los estados.

## Estructura

- `data/` — PDFs de entrada (`facturas/`, `facturas_lote2/`), Excel de contexto,
  snapshot cacheado del ERP (`erp_snapshot.json`) y etiquetas manuales (`golden/`).
  - `data/corpus/` — el kit del reto tal cual lo reparte la organización
    (`alberto_erp.py`, `MANUAL_ERP_2009.md`, `Makefile`, spec). Es lo que hace
    reproducible el arranque del ERP sin depender de la wifi del evento.
- `traces/` — una carpeta por factura con OCR, evidencia y decisión (trazabilidad).
- `outputs/` — `outcomes.jsonl` y `outcomes_lote2.jsonl` (entregables). Además,
  `outcomes_traza.jsonl` (auditoría, se regenera) y `outcomes_cola.jsonl`
  (opcional: la evidencia de la segunda lectura para recortar la cola de revisión,
  **no** es una entrega; ver `motor/README.md`).
- `ocr_service/` — servicio OCR real (FastAPI + RapidOCR/ONNX, endpoints
  `/ocr`, `/ocr/text`, `/ocr/stream`, `/health`, `/cloud`): `app/` (servidor y
  cliente de nube), `Dockerfile`, `docker-compose.yml` (`ocr-api` en el 8866),
  `scripts/` de diagnóstico y `test_files/`. Ver `ocr_service/README.md`.
- `api/` — middleware/BFF (FastAPI): **la única superficie publicada a la LAN**
  (`albertitos-api`, `0.0.0.0:8010`). Lee la traza y los PDFs, consulta Mongo en
  solo lectura, hace de proxy del OCR y sirve el visor en el mismo origen. Ver
  `api/README.md` y `docs/ADR-0001-middleware-bff.md`.
- `docs/` — documentación de proyecto: `ENTREGA.md` (cómo se publica y qué no),
  `ADR-0001-middleware-bff.md` (por qué la API/BFF es la única superficie publicada y
  Mongo sigue en `127.0.0.1`) y `arranque_servicios.md` (runbook para levantar todo desde
  cero).
- `motor/` — **el motor de decisión que se ejecuta** (Python): reglas, tests,
  banco de oro, herramientas y documentación. Ver `motor/README.md`.
- `src/` — binario Rust (legado): `main.rs` (orquestación) + módulos `domain`,
  `erp`, `ocr`, `excel`, `parser`, `validators`, `reconciler`, `rules`, `obs`.
- `config/reglas.toml` — reglas del motor Rust (legado). Las del motor vivo
  están en `motor/config/reglas.toml`.
- `ui/` — visor HTML estático de trazas (bonus).
- `validate_jsonl.py` — validador de entrega.

## Arranque

### Servicios en Docker (MongoDB, OCR y API)

Los proyectos de Compose son independientes, así que comparten una red externa
con nombre fijo, `albertitos_net`. Se crea **una sola vez**:

```
docker network create albertitos_net
```

Después:

```
# MongoDB 7 (replica set de 1 nodo, autenticación obligatoria).
# Publica el 27017 SOLO en 127.0.0.1: no se expone a la LAN.
cd maisa
cp .env.example .env        # y rellenar los secretos
docker compose up -d mongo mongo-init
python3 importar_asientos_mongo.py   # 516 asientos; idempotente

# Servicio de OCR (FastAPI + RapidOCR) en el 8866.
cd ocr_service
cp .env.example .env        # si no existe: el compose declara env_file: .env
docker compose up -d --build

# API/BFF: la ÚNICA superficie publicada a la LAN (0.0.0.0:8010).
cd ../api
cp .env.example .env        # poner la contraseña real en MONGO_URI (entre comillas)
docker compose up -d --build
```

Dentro de `albertitos_net` los servicios se alcanzan por DNS: `mongo:27017`,
`ocr-api:8866` (alias `ocr:8866`). Desde la máquina anfitriona, en cambio,
Mongo está en `127.0.0.1:27017` y el OCR en `127.0.0.1:8866`.

**MongoDB no se expone nunca.** El frontend y el resto del equipo consumen la
API (`http://<IP-DE-LA-MAQUINA>:8010`), que es de solo lectura sobre Mongo y
sirve el visor en el mismo origen. El porqué está en
`docs/ADR-0001-middleware-bff.md`; el arranque paso a paso, con las
comprobaciones y los problemas conocidos, en `docs/arranque_servicios.md`.

### Motor de decisión

```
# Motor de decisión (Python). No necesita red ni servicios: la caché de OCR de
# los 29 escaneados va versionada en motor/.cache/ocr/.
python -m pip install -r motor/requirements.txt
cd maisa
PYTHONPATH=motor/src python -m maisa.procesa \
  --facturas data/facturas \
  --xlsx     data/FINAL_v7_DEFINITIVO_ahorasi.xlsx \
  --snapshot data/erp_snapshot.json \
  --config   motor/config/reglas.toml \
  --lote 1 --trabajadores 4 \
  --salida   outputs/outcomes.jsonl
```

Si aparece un PDF escaneado que no esté en la caché, el motor recurre al
servicio de visión (por defecto `http://127.0.0.1:8866`) y rellena la caché.
Para atender ese caso hace falta el contenedor `ocr-api`
(`ocr_service/docker-compose.yml`).

### Legado: el binario Rust

```
# 1. Bridge del ERP del reto (servicio externo, puerto 8009 por defecto)
python alberto_erp.py --rapido
#    Si no esta en esa direccion, apunta la URL sin tocar codigo:
#    $env:ERP_BASE_URL = "http://127.0.0.1:8009"   (Windows PowerShell)
#    export ERP_BASE_URL="http://127.0.0.1:8009"   (bash)

# 2. Servicio de OCR (FastAPI + RapidOCR). Escucha en el 8866.
cd ocr_service && uvicorn app.server:app --host 127.0.0.1 --port 8866
#    O en contenedor: cd ocr_service && docker compose up -d --build

# 3. Binario Rust (rutas relativas a la raiz de `maisa/`)
cargo run --release -- --pdf-dir data/facturas --out outputs/outcomes.jsonl
```

> El paso 3 **no completa** el lote: los módulos de lectura siguen en esqueleto.
> Se conserva para no perder el diseño y sus 57 tests.

## Configuración (variables de entorno)

Todas las direcciones y credenciales se leen del entorno: no hay ninguna ruta
de máquina ni contraseña fija en el código. La única excepción es `OCR_URL`, que
el motor vivo lleva como constante.

| Variable | Para qué | Por defecto |
|---|---|---|
| `ERP_BASE_URL` | dirección del bridge del ERP | `http://127.0.0.1:8009` |
| `ERP_USUARIO` / `ERP_CLAVE` | credenciales del bridge (`descargar_erp.py`) | las del reto |
| `OCR_URL` | endpoint del servicio de OCR (constante en `motor/src/maisa/lectura.py`; el legado Rust sí la lee del entorno) | `http://127.0.0.1:8866` |
| `MONGO_URI` | conexión a MongoDB | `mongodb://localhost:27017` |
| `MONGO_ROOT_USER` / `MONGO_ROOT_PASSWORD` / `MONGO_APP_PASSWORD` | Mongo (ver `.env`) | — |

En producción define `ERP_CLAVE` (y las de Mongo): el script avisa por `stderr`
cuando está usando las credenciales por defecto.

Ver `spec_y_plan.md` para el detalle completo (modelo de datos, contratos,
motor de reglas, resiliencia y plan por bloques).
