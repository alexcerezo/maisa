# Escaneo del sistema desplegado — Maisa / Albertitos

**Fecha del escaneo:** 2026-09-19 20:48 UTC
**Máquina:** `openthings` · Ubuntu 24.04.5 LTS · aarch64 (Neoverse-N1, 2 vCPU) · 11 GiB RAM
**Commit en `main`:** `8977d3c` — *docs(api): explicar el vocabulario del dominio, la paginacion y el flujo del visor*
**IP pública:** `82.70.78.22` · **IP LAN:** `10.0.0.75`

Este documento es una **fotografía del estado real** del sistema en el momento indicado: qué
está desplegado, cómo está configurado, qué responde y qué falta. No sustituye al runbook
(`docs/arranque_servicios.md`) ni al ADR (`docs/ADR-0001-middleware-bff.md`): los resume y los
verifica contra la máquina.

---

## 1. Resumen ejecutivo

| Pieza | Estado |
|---|---|
| MongoDB 7 (replica set `rs0`, 1 nodo) | 🟢 `Up (healthy)` — solo en `127.0.0.1:27017` |
| OCR (FastAPI + RapidOCR/ONNX) | 🟢 `Up (healthy)` — `0.0.0.0:8866` (no alcanzable desde Internet) |
| API/BFF (FastAPI) | 🟢 `Up (healthy)` — **única superficie pública** en `0.0.0.0:8010` |
| ERP simulado (proceso Python) | 🟢 `127.0.0.1:8009` (solo loopback) |
| Motor de decisión (Python) | 🟢 Ejecutado: 500 facturas, 0 líneas inválidas |
| Visor web (`maisa/ui/`) | 🔴 **Vacío** (solo `.gitkeep`) — `/` devuelve JSON informativo |
| Traza de entrega | 🟢 500 líneas, `PAGAR 448 / NO_PAGAR 9 / ESCALAR 43` |
| Red compartida `albertitos_net` | 🟢 Existe, con los 3 contenedores dentro |
| TLS / autenticación fuerte | 🔴 Ausentes — la API corre en **modo abierto** y es pública |

**Conclusión:** el núcleo (Mongo + OCR + API + motor) está **operativo y verificado de punta a
punta**. Lo que falta es de cara al exterior: visor, TLS y autenticación real.

---

## 2. Infraestructura anfitriona

| Recurso | Valor |
|---|---|
| SO | Ubuntu 24.04.5 LTS, kernel `6.17.0-1020-oracle` (aarch64) |
| CPU | 2 vCPU (Neoverse-N1) |
| RAM | 11 GiB total · 3,9 GiB usada · 7,7 GiB disponible · **sin swap** |
| Disco `/` | 45 GB · 28 GB usados (62 %) · 17 GB libres |
| Docker | `29.8.0` |
| Docker Compose | `5.5.1` (v2) |
| Python | 3.12.3 |
| Uptime | 1 semana, 1 día, 11 h |

---

## 3. Contenedores en ejecución

```
NAMES                   IMAGE                          STATUS                   PORTS
albertitos-api          albertitos-api:latest          Up 3 hours (healthy)     0.0.0.0:8010->8000/tcp
albertitos-mongo        mongo:7.0                      Up 6 hours (healthy)     127.0.0.1:27017->27017/tcp
albertitos-mongo-init   mongo:7.0                      Exited (0) 6 hours ago
ocr-api                 ocr-rapidocr-arm64:latest      Up 12 hours (healthy)    0.0.0.0:8866->8866/tcp
wechat-client           ricwang/docker-wechat:latest   Up 7 days                0.0.0.0:5800->5800/tcp, 0.0.0.0:5900->5900/tcp
vigilant_colden         hello-world                    Exited (0) 7 days ago
```

> `wechat-client` y `vigilant_colden` **no forman parte de Albertitos**: son residuos de otros
> proyectos en la misma máquina (compose `wechat-video-bot`). Consumen CPU/RAM y ocupan puertos,
> pero no interfieren con el sistema.

### Consumo de recursos (en el momento del escaneo)

| Contenedor | CPU | Memoria | Límite |
|---|---|---|---|
| `albertitos-api` | 0,16 % | 59 MiB | — |
| `albertitos-mongo` | 0,53 % | 104 MiB | — |
| `ocr-api` | 0,14 % | **1,63 GiB** | 4 GiB / 2 CPU |
| `wechat-client` | 0,81 % | 235 MiB | — |

### Imágenes

| Imagen | Tamaño | Antigüedad |
|---|---|---|
| `albertitos-api:latest` | 244 MB | 3 h |
| `ocr-rapidocr-arm64:latest` | 1,21 GB | 12 h |
| `mongo:7.0` | 1,13 GB | 5 días |
| `wechat-video-bot-wechat-client:latest` | 1,83 GB | 7 días |
| `ricwang/docker-wechat:latest` | 1,84 GB | 2 semanas |
| `curlimages/curl:latest` | 37,5 MB | 2 semanas |
| `hello-world:latest` | 22,6 kB | 5 meses |

---

## 4. Topología de red y puertos

### Redes Docker

| Red | Tipo | Uso |
|---|---|---|
| `albertitos_net` | bridge (externa) | **Compartida por los 3 servicios del sistema** |
| `ocr_service_default` | bridge | Red propia del OCR (le da puerto y aislamiento) |
| `paddleocr_default` | bridge | Proyecto ajeno |
| `wechat-video-bot_default` | bridge | Proyecto ajeno |
| `bridge` / `host` / `none` | — | Estándar de Docker |

`albertitos_net` es **externa**: la crea el operador una sola vez y ningún `docker compose down`
se la lleva por delante.

### Puertos a la escucha en el host

| Puerto | Escucha en | Servicio | ¿Internet? |
|---|---|---|---|
| **8010** | `0.0.0.0` | API/BFF (`albertitos-api` → 8000) | **Sí — única puerta** |
| 8866 | `0.0.0.0` | OCR (`ocr-api`) | No (el NSG no lo abre) |
| 27017 | `127.0.0.1` | MongoDB | No |
| 8009 | `127.0.0.1` | ERP simulado (`alberto_erp.py`) | No |
| 5800 / 5900 | `0.0.0.0` | wechat-client (VNC) — ajeno | No |
| 22 | `0.0.0.0` | SSH | — |

### DNS interno de `albertitos_net`

| Desde un contenedor | Alcanza |
|---|---|
| `mongo:27017` | MongoDB (replica set, `directConnection=true`) |
| `ocr-api:8866` (alias `ocr:8866`) | Servicio OCR |
| `albertitos-api:8000` | API/BFF (puerto interno **8000**, no 8010) |
| `host.docker.internal:8009` | ERP simulado (loopback del anfitrión) |

---

## 5. Servicios: configuración y salud real

### 5.1 MongoDB — `albertitos-mongo`

- **Imagen:** `mongo:7.0`, `restart: unless-stopped`.
- **Replica set:** `rs0`, un solo nodo → habilita *change streams* (UI en vivo) y prepara escalado
  sin cambiar la cadena de conexión.
- **Autenticación:** obligatoria. Usuario `root` para administración + usuario de app con
  `readWrite` **solo** sobre `albertitos`. `--keyFile` propio (`docker/mongo/entrypoint.sh`).
- **Publicación:** `127.0.0.1:27017` — **nunca** a la red.
- **Volúmenes nombrados:** `albertitos_mongo_data`, `albertitos_mongo_config` (persisten a `down`).
- **Inicialización:** `albertitos-mongo-init` arranca el replica set una sola vez y termina
  (`Exited (0)`, correcto).

Contenido de la base `albertitos` (usuario de app):

| Colección | Documentos |
|---|---|
| `asientos` | 516 (516 vigentes) |
| `erp_snapshots` | 1 (`snap-2026-09-19T08-25-58Z`) |
| `expedientes` | 0 |
| `eventos` | 0 |
| `pdfs.files` (GridFS) | 0 |
| `excel_filas` | 0 |

> `expedientes`, `eventos`, `pdfs` y `excel_filas` están **vacíos**: el motor todavía no persiste
> sus decisiones en Mongo (viven en `outputs/outcomes_traza.jsonl`). `POST /api/facturas` sí
> escribiría ahí, pero aún no se ha subido ninguna factura.

### 5.2 OCR — `ocr-api`

- **Imagen:** `ocr-rapidocr-arm64:latest` (FastAPI + RapidOCR/ONNX Runtime), `mem_limit: 4g`,
  `cpus: 2.0`, 2 hilos de inferencia (`OMP_NUM_THREADS=2`).
- **Redes:** `ocr_service_default` + `albertitos_net`.
- **Endpoints:** `/ocr`, `/ocr/text`, `/ocr/stream`, `/cloud`, `/health`.
- **Motores:** local (`PP-OCRv5_mobile` det/rec, `PP-OCRv4_mobile` cls, `onnxruntime`) y
  **nube** (`PaddleOCR-VL-1.6`, token configurado, circuito cerrado = sano). `engine: auto`.
- **Ajustes:** `pdf_scale: 4.0`, `auto_scale` hasta 5.0, subida máx. 300 MB, 24 MPx, tmp `/tmp/ocr-work`.

Respuesta de `GET /health`: `{"status":"ok", ...}` con ambos motores disponibles.

### 5.3 API/BFF — `albertitos-api`

- **Imagen:** `albertitos-api:latest`, usuario `apiuser`, `restart: unless-stopped`.
- **Publicación:** `0.0.0.0:8010 -> 8000` — **la única superficie pública**.
- **Rol:** capa de transporte/lectura/captura. **No decide nada**; lee la traza, sirve PDFs,
  consulta Mongo (solo lectura del catálogo) y hace de proxy del OCR.
- **Volúmenes (solo lectura):** `outputs/`, `data/facturas/`, `ui/`.
- **Credenciales:** de `maisa/.env` como `env_file` (`required: false`); las de `root` se anulan
  dentro del contenedor. `MONGO_URI` se vacía a propósito y la API construye la cadena con
  `MONGO_HOST=mongo` + `directConnection=true`.

**Endpoints publicados** (bajo `/api`):

| Método | Ruta | Función |
|---|---|---|
| GET | `/health` | Estado del servicio y dependencias |
| GET | `/health/ready` | `503` si falla algo crítico |
| GET | `/api/meta` | Versión y configuración no sensible |
| GET | `/api/facturas` | Listado paginado de facturas con su decisión |
| GET | `/api/facturas/{file_id}` | Detalle completo |
| GET | `/api/facturas/{file_id}/pdf` | PDF original (`inline`) |
| POST | `/api/facturas` | **Único camino de escritura** (PDF → GridFS + expediente) |
| GET | `/api/expedientes` | Listado de expedientes en Mongo |
| GET | `/api/asientos` | Catálogo del ERP (paginado) |
| GET | `/api/asientos/{asiento_id}` | Detalle de un asiento |
| GET | `/api/snapshots` | Descargas del ERP registradas |
| GET | `/api/estadisticas` | Recuentos por resultado/lote/método |
| POST | `/api/ocr` | Proxy del OCR |
| GET | `/docs` | Swagger UI |

**Comprobaciones en vivo:**

```
GET /health          -> 200  estado:"ok"  mongo.ok:true  ocr.ok:true  escritura.ok:true
GET /health/ready    -> 200
GET /api/estadisticas-> 200  (ver §6)
GET http://82.70.78.22:8010/health  -> 200
GET http://82.70.78.22:8010/       -> 200
```

Dependencias críticas: `mongo, ocr` (ninguna caída). Timeout de Mongo 1500 ms, health 2 s.

**Configuración efectiva** (`/api/meta`): `api_version 1.0.0`, `modo_abierto: true`,
`api_key_requerida: false`, `subidas.habilitadas: true`, subida máx. 50 MB, paginación 50/500,
CORS limitado a `localhost/127.0.0.1:8010` y `:5173`.

### 5.4 ERP simulado

- Proceso Python fuera de Docker (`alberto_erp.py --puerto 8009`), escucha en `127.0.0.1:8009`.
- Responde `200` en `/`. La API **no lo consulta**: su URL es meramente informativa.

---

## 6. Datos y resultados del motor

### Traza y entrega

| Fichero | Líneas | Notas |
|---|---|---|
| `outputs/outcomes.jsonl` | 500 | Entrega: solo `file_id` + `result` |
| `outputs/outcomes_traza.jsonl` | 500 | Traza completa (decisión, motivos, hechos, campos) |
| `outputs/outcomes_lote2.jsonl` | 0 | Lote 2 no procesado |

### Recuentos (`GET /api/estadisticas`)

| Métrica | Valor |
|---|---|
| Total facturas | 500 |
| `PAGAR` | 448 |
| `NO_PAGAR` | 9 |
| `ESCALAR` | 43 |
| Resultados desconocidos | 0 |
| Líneas inválidas | 0 |
| `coincide_con_traza` | **true** (entrega y traza cuadran) |
| Asientos vigentes (ERP) | 516 |
| Método de lectura | `texto_determinista` 471 · `vision_ocr` 29 |
| Versión de norma | `norma_v3.1` (500) |

### Corpus

- `data/facturas/` — 500 PDFs (lote 1).
- `data/facturas_lote2/` — lote 2 (pendiente).
- `data/erp_snapshot.json`, `data/FINAL_v7_DEFINITIVO_ahorasi.xlsx`, `data/golden/`,
  `data/corpus/` (kit del reto).
- `traces/` — carpeta de trazabilidad (`trazabilidad.md`).

---

## 7. Estructura del repositorio

```
alexcerezo-maisa/
├── maisa/                      # Proyecto principal
│   ├── docker-compose.yml      # MongoDB 7 + mongo-init
│   ├── .env / .env.example     # Credenciales Mongo (no versionado)
│   ├── motor/                  # ⭐ MOTOR VIVO (Python): reglas, tests, tools
│   ├── src/                    # Binario Rust (LEGADO, no compila sobre el corpus)
│   ├── api/                    # Middleware/BFF FastAPI (superficie pública)
│   ├── ocr_service/            # Servicio OCR FastAPI + RapidOCR
│   ├── ui/                     # Visor estático (VACÍO)
│   ├── data/ · outputs/ · traces/ · config/
│   └── docs/                   # ADR, runbook, entrega y este escaneo
├── hackspain-ocr/              # Workspace OCR del reto
├── recon/                      # Scripts, logs, briefings
├── _scratch/                   # Trabajo temporal
└── .github/workflows/ci.yml    # CI
```

> **Motor vivo = `motor/` (Python).** El binario Rust de `src/` es legado: sus 57 tests pasan,
> pero el lector del maestro (`excel.rs`) y la observabilidad (`obs.rs`) son placeholders. Se
> conserva por su análisis de dominio, no por su ejecución.

---

## 8. Estado del repositorio (git)

- Rama actual: `main`, sincronizada con `origin/main` en `8977d3c`.
- **Hay cambios sin commitear** en `maisa/api/*`, `docker/mongosh/02-schema-init.js`,
  `docs/*`, `diseño_logico.md` (el árbol de trabajo no coincide con el último commit).
- Ramas remotas relevantes: `feat/unificacion-motor-python`, `fix/entrega-en-la-raiz`,
  `refactor/migrar-workspace-ocr`, además de ramas de dependabot.
- `.env` está en `.gitignore` y no se versiona (correcto).

---

## 9. Riesgos y pendientes

| # | Severidad | Hallazgo | Detalle |
|---|---|---|---|
| 1 | 🔴 Crítico | **API pública en modo abierto y con escritura** | `modo_abierto: true`, `API_KEY` sin definir, `SUBIDAS_HABILITADAS=1`. Cualquiera que alcance `82.70.78.22:8010` puede **leer y subir facturas**. |
| 2 | 🔴 Crítico | **Sin TLS** | Todo el tráfico (incluidas subidas de PDF) viaja en claro por Internet. |
| 3 | 🟠 Alto | **Puerto 8866 del OCR publicado en `0.0.0.0`** | No lo abre el NSG, pero es superficie innecesaria; el frontend entra por `/api/ocr`. |
| 4 | 🟠 Alto | **Persistencia del motor en Mongo incompleta** | `expedientes`, `eventos`, `excel_filas` vacíos; las decisiones solo viven en JSONL. |
| 5 | 🟡 Medio | **Visor (`maisa/ui/`) vacío** | La tubería está probada, pero `/` devuelve JSON en vez del visor. |
| 6 | 🟡 Medio | **Lote 2 sin procesar** | `outcomes_lote2.jsonl` está vacío. |
| 7 | 🟡 Medio | **`GET /api/asientos/{id}` no filtra por `vigente`** | Equivalente con un solo snapshot; ambiguo con varios. |
| 8 | ⚪ Bajo | **Contenedores ajenos en la misma máquina** | `wechat-client` (235 MiB, puertos 5800/5900) y `vigilant_colden`. |
| 9 | ⚪ Bajo | **Sin swap** | 11 GiB de RAM sin swap; picos del OCR (1,6 GiB) podrían ser ajustados. |
| 10 | ⚪ Bajo | **Árbol de trabajo sucio** | Cambios sin commitear en `api/`, `docs/` y `docker/`. |
| 11 | ⚪ Bajo | **Sin caché/ETag en el listado** | La traza cabe en memoria (500 facturas); no escala sin paginar desde Mongo. |

---

## 10. Cómo verificar este escaneo

```bash
# Contenedores y salud
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
curl -s http://127.0.0.1:8010/health
curl -s http://127.0.0.1:8010/health/ready -o /dev/null -w '%{http_code}\n'
curl -s http://127.0.0.1:8010/api/estadisticas
curl -s http://127.0.0.1:8866/health

# Desde Internet (la única puerta)
curl -s -o /dev/null -w '%{http_code}\n' http://82.70.78.22:8010/health

# Puertos que NO deben responder desde fuera (esperado: 000)
curl -s -m 5 -o /dev/null -w '%{http_code}\n' http://82.70.78.22:8866/health
curl -s -m 5 -o /dev/null -w '%{http_code}\n' http://82.70.78.22:27017/

# Traza y entrega
wc -l maisa/outputs/outcomes.jsonl maisa/outputs/outcomes_traza.jsonl

# Tests de la API (92 esperados)
.venv-api/bin/python -m pytest maisa/api

# Smoke completo
PUBLIC_IP=82.70.78.22 ./maisa/api/smoke_lan.sh --publico --engine local --subir
```

---

## 11. Referencias

- `maisa/docs/arranque_servicios.md` — runbook paso a paso, problemas conocidos, estado verificado.
- `maisa/docs/ADR-0001-middleware-bff.md` — por qué la API/BFF es la única superficie publicada.
- `maisa/api/README.md` — vocabulario del dominio, endpoints y ejemplos reales.
- `maisa/ocr_service/README.md` — detalle del servicio OCR.
- `maisa/motor/README.md` — el motor de decisión vivo.
- `maisa/TRASPASO.md` — diseño de contratos (sigue vigente para el motor).
