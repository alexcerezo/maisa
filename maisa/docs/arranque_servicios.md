# Arranque de servicios — Maisa / Albertitos

Runbook para levantar el sistema completo desde cero en esta máquina y dejarlo
alcanzable **desde Internet** (la API en `https://82.70.78.22.sslip.io`, y también por
`http://82.70.78.22:8010`) y desde dentro de Docker. La LAN no es una vía de consumo: solo
se usa el DNS interno de `albertitos_net` entre contenedores. Está escrito para ejecutarse
de arriba abajo, sin preguntar nada: cada paso lleva el comando literal y el motivo por el
que va ahí.

> **La vía de reparto es HTTPS.** Todo se reparte por `https://82.70.78.22.sslip.io` (§3.6).
> El panel no se aloja aparte: lo sirve **la propia API** en el mismo origen (§3.8), así que no
> hay CORS ni *mixed content* que resolver. El `8010` en claro sigue abierto, pero es para
> diagnóstico y para el smoke test, no para el frontend.

Decisiones de arquitectura que sostienen este runbook: `maisa/docs/ADR-0001-middleware-bff.md`.
Detalle de la API: `maisa/api/README.md`. Detalle del OCR: `maisa/ocr_service/README.md`.
Detalle del HTTPS: `maisa/proxy/README.md`.

---

## 1. Inventario y puertos

| Servicio | Contenedor | Imagen | Red(es) | Publicado | Alcanzable desde Internet |
|---|---|---|---|---|---|
| MongoDB 7 (replica set `rs0`, 1 nodo) | `albertitos-mongo` | `mongo:7.0` | `albertitos_net` | `127.0.0.1:27017->27017` | **NO** |
| Inicializador del replica set | `albertitos-mongo-init` | `mongo:7.0` | `albertitos_net` | — | — |
| OCR (FastAPI + RapidOCR) | `ocr-api` | `ocr-rapidocr-arm64:latest` | `albertitos_net` + red propia del OCR (§7.9) | `0.0.0.0:8866->8866` | **NO** (el NSG no abre 8866) |
| API/BFF (FastAPI) | `albertitos-api` | `albertitos-api:latest` | `albertitos_net` | `0.0.0.0:8010->8000` | **Sí** (en claro; diagnóstico y smoke) |
| Proxy TLS (Caddy) | `albertitos-proxy` | `caddy:2.8-alpine` | `albertitos_net` | `0.0.0.0:80->80`, `0.0.0.0:443->443` | **Sí — la puerta del sistema (HTTPS)** |
| ERP simulado (fuera de Docker) | — | proceso Python | — | `127.0.0.1:8009` | **NO** (solo loopback del host) |

«Publicado» es lo que expone Docker en el host; «alcanzable desde Internet» es lo que el NSG
deja pasar. El OCR y el ERP están publicados en el host pero **no** son alcanzables desde
fuera, que es lo correcto (`maisa/api/README.md` §2.2).

Puertos elegidos: **8010** para la API porque 8009 es el ERP y 8866 el OCR; **8866** para el
OCR porque es el histórico de Paddle Serving y evita el 8080 habitual; **80 y 443** para el
proxy TLS, que son los que Let's Encrypt necesita para validar el nombre.

El proxy **no** sustituye al `8010`: Caddy reenvía a `albertitos-api:8000` por el DNS interno,
y el `8010` sigue publicado para diagnóstico. La API no sabe que hay un proxy delante salvo
por `FORWARDED_ALLOW_IPS` (§3.8).

DNS interno de `albertitos_net` (lo que se usa **desde dentro** de un contenedor):
`mongo:27017`, `ocr-api:8866` (alias `ocr:8866`).

---

## 2. Prerrequisitos

| # | Requisito | Comando |
|---|---|---|
| 1 | Docker con Compose v2 | `docker compose version` |
| 2 | **Red compartida externa** | `docker network create albertitos_net` |
| 3 | Credenciales de Mongo | `cd maisa && cp .env.example .env` y **rellenar** `MONGO_ROOT_*` y `MONGO_APP_*` con secretos reales |
| 4 | Configuración de la API (**opcional**) | Solo si hay que cambiar algún valor por defecto: `cd maisa/api && cp .env.example .env`. La contraseña **no** se pone aquí: el compose la toma de `maisa/.env` como `env_file` (ver §3.5). |
| 5 | Configuración del OCR | `cd maisa/ocr_service && cp .env.example .env` |
| 6 | Python 3 (solo biblioteca estándar) para el importador de asientos | `python3 --version` |

**Los `.env` están en `.gitignore`.** En un clon nuevo no existen: sin `maisa/.env` Mongo
arranca sin credenciales y la API se degrada; sin `maisa/ocr_service/.env` el compose del OCR
ni arranca (ver §7.5).

> ## ⚠️ Si la plantilla se copia sin rellenar, la API se degrada en silencio
>
> Dejar los `cambiame_*` de `.env.example` en `maisa/.env` **no** impide arrancar: Mongo se
> inicializa con esas credenciales y el importador funciona. Pero si el volumen ya existía
> de un arranque anterior, los usuarios de la BD son los de **entonces** y el `.env` de ahora
> solo sirve para que el cliente envíe la contraseña equivocada: `albertitos-api` arranca,
> sale `Up (healthy)` y devuelve `503` en `/health/ready` (ver §7.3 y §7.10).

La red `albertitos_net` es **externa** en los tres ficheros de compose
(`external: true`). Se crea **una sola vez**; ningún `docker compose up` la crea, y sin ella
cualquier `up` aborta.

---

## 3. Orden de arranque, y por qué ese orden

```
   red  ->  Mongo  ->  mongo-init (healthy)  ->  asientos  ->  OCR  ->  API  ->  proxy TLS  ->  (ERP)  ->  (visor)
```

| # | Paso | Por qué va aquí |
|---|---|---|
| 1 | Red | Todo lo demás la declara externa. Si no existe, ningún `up` arranca. |
| 2 | Mongo | Es la única pieza con estado. Las demás la consumen. |
| 3 | `mongo-init` | `rs.initiate()` **no** puede ir en `/docker-entrypoint-initdb.d` (el entrypoint oficial ejecuta esos scripts contra un mongod temporal sin replica set). Va en un contenedor aparte que espera a que Mongo esté `healthy`. |
| 4 | Importar asientos | Necesita el replica set ya iniciado y las credenciales de `.env`. Si la API arrancase antes, `/api/asientos` daría `total: 0` sin que nada fallara. |
| 5 | OCR | La API lo consulta en `/health/ready` y lo usa en `POST /api/ocr`. |
| 6 | API | Es la última pieza con estado propia: es la que **informa** del resto, porque su `/health` dice qué está caído. Al crearla se leen las credenciales de `maisa/.env`, así que si el `.env` cambia hay que **recrearla** (§7.3). |
| 7 | Proxy TLS | Necesita la API arriba para reenviarle: si arranca antes, Caddy sirve `502` (el certificado lo saca igual, porque el reto ACME no depende de la API). |
| 8 | ERP | Solo lo necesita el motor para refetchear el snapshot. Escucha en loopback del host: no se publica. |
| 9 | Visor | No es un servicio: son ficheros en `maisa/ui/`. La API los sirve en `/`. |

### 3.1 Red (una sola vez)

```bash
docker network create albertitos_net
```

### 3.2 MongoDB + replica set

```bash
cd maisa
cp .env.example .env          # rellenar MONGO_ROOT_* y MONGO_APP_*
docker compose up -d mongo mongo-init
docker compose ps -a          # mongo "healthy"; mongo-init "Exited (0)"
```

`mongo-init` **debe** terminar en `Exited (0)`: es un arranque de un solo uso
(`restart: "no"`). Si aparece en `running`, está esperando al PRIMARY.

### 3.3 Asientos del ERP

```bash
cd maisa
python3 importar_asientos_mongo.py
```

- Carga `data/erp_snapshot.json` (516 asientos) en la colección `asientos`, **por
  `docker compose exec mongo mongoimport`** con `--mode=upsert --upsertFields=_id`.
- **Es idempotente**: reejecutarlo actualiza, no duplica, y retira la vigencia del snapshot
  anterior (`vigente: false`).
- Usa el usuario **root** de `maisa/.env` (`MONGO_ROOT_USER` / `MONGO_ROOT_PASSWORD`) contra
  `--authenticationDatabase admin`; no hace falta pasar nada por parámetro.
- `--dry-run` valida y resume sin escribir.

### 3.4 OCR

```bash
cd maisa/ocr_service
cp .env.example .env          # si no existe: el compose declara env_file: .env
docker compose up -d --build
```

### 3.5 API/BFF

```bash
cd maisa/api
docker compose -f docker-compose.yml up -d --build
```

No hace falta ningún `.env` en `maisa/api/`: el compose inyecta `maisa/.env` con
`env_file: ../.env` y de ahí saca `MONGO_APP_USER` / `MONGO_APP_PASSWORD` (y `MONGO_DB`).
La API **no** recibe una URI completa, la construye con las piezas
(`maisa/api/app/config.py` → `mongo_uri_desde_piezas()`):

```
mongodb://<MONGO_APP_USER>:<MONGO_APP_PASSWORD>@<MONGO_HOST>:<MONGO_PORT>/<MONGO_DB>?replicaSet=rs0&directConnection=true&authSource=<MONGO_DB>
```

Por eso el `MONGO_URI` del compose se deja **vacío a propósito**: el de `maisa/.env` apunta a
`127.0.0.1`, que dentro del contenedor es el propio contenedor. Si necesitas forzar una URI
concreta, defínela en `maisa/api/.env` (que entonces sí hay que crear) o expórtala antes del
`up`. El detalle completo, en `maisa/api/docker-compose.yml` (comentarios de cabecera) y en
`maisa/api/README.md` §3.

`directConnection=true` no es un adorno: el miembro del replica set se anuncia como
`127.0.0.1:27017` y sin esa opción el driver intentaría reconectar contra sí mismo desde
dentro del contenedor.

### 3.6 Proxy TLS (HTTPS)

```bash
docker compose -f maisa/proxy/docker-compose.yml up -d
docker compose -f maisa/proxy/docker-compose.yml logs -f caddy   # buscar "certificate obtained successfully"
```

Caddy pide el certificado a Let's Encrypt para `82.70.78.22.sslip.io` (DNS comodín que
resuelve a la IP escrita en el nombre, así que **no** hace falta dominio propio) y reenvía a
`albertitos-api:8000` por el DNS interno de `albertitos_net`. El detalle, en
`maisa/proxy/README.md`.

Dos cosas que tienen que estar antes, o el certificado **no** se emite:

1. **NSG abierto en 80 y 443.** El reto HTTP-01 llega desde Internet; sin la regla, Caddy
   reintenta en bucle sin emitir nada. El comando `oci` exacto está en
   `maisa/proxy/README.md` §2.
2. **`FORWARDED_ALLOW_IPS` en la API.** Va en `maisa/api/docker-compose.yml` con la subred
   de `albertitos_net` (`172.20.0.0/16`). uvicorn solo cree `X-Forwarded-Proto` si el
   origen está en esa lista; sin esto la API genera URLs `http://` y el navegador las
   bloquea igual, ahora por *mixed content* inverso.

```bash
curl -sS -o /dev/null -w 'status=%{http_code} tls=%{ssl_verify_result}\n' \
  https://82.70.78.22.sslip.io/health          # status=200 tls=0
```

El `8010` en claro **no** se cierra: sigue siendo la vía de diagnóstico y la que usa el smoke
test contra `127.0.0.1`. Lo que cambia es qué URL se reparte al frontend.

---

### 3.7 ERP simulado (solo si hace falta refetchear)

```bash
cd maisa/data/corpus
python3 alberto_erp.py --puerto 8009     # o: make erp
curl -s http://127.0.0.1:8009/erp/estado # o: make erp-status
```

### 3.8 Visor

El panel **ya está en el repo** (`maisa/ui/`), así que este paso no es traerlo sino **construirlo**.
No hay ningún servidor de desarrollo en el despliegue: quien sirve el panel es la propia API.

```bash
cd maisa/ui
npm ci && npm run build          # deja maisa/ui/dist
```

`maisa/ui` está montado en el contenedor como `/datos/ui:ro` y `UI_DIR` apunta a
`/datos/ui/dist`, que es lo que Vite deja. Con `dist/index.html` presente, `GET /` sirve el panel y
el navegador habla con la API **en el mismo origen**: sin CORS y sin credenciales. Se ve en
`http://127.0.0.1:8010/` y, por HTTPS, en `https://82.70.78.22.sslip.io/`; las rutas del panel
(`/facturas`, `/trazabilidad`, `/escalabilidad`) también.

**No hay que reiniciar nada.** La decisión se toma **en cada petición**
(`maisa/api/app/main.py` → `_montar_ui()`): basta con que aparezca o desaparezca el `index.html`,
sin recrear el contenedor (ver §7.11). Y si no hay build —un clon recién bajado, o un `dist`
borrado— `GET /` devuelve un mensaje informativo en vez de un 404, y
`GET /api/meta` → `configuracion.ui.disponible: false` lo dice. Eso es degradación, no un fallo.

Para saber si el contenedor sirve **el build que crees**, compara el hash del bundle que responde
con el del disco:

```bash
curl -s http://127.0.0.1:8010/trazabilidad | grep -o 'index-[A-Za-z0-9]*\.js'
ls maisa/ui/dist/assets/index-*.js
```

Detalle del panel: `maisa/ui/README.md` §3.1.

---

## 4. Comprobaciones pieza a pieza

| Pieza | Comando | Resultado esperado |
|---|---|---|
| Mongo vivo | `docker compose -f maisa/docker-compose.yml ps` | `albertitos-mongo` → `Up (healthy)` |
| Replica set | `docker exec albertitos-mongo sh -c 'mongosh --quiet -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin --eval "rs.status().ok"'` | `1` |
| Replica set (forma de §13.9) | `docker compose -f maisa/docker-compose.yml exec mongo mongosh -u "$MONGO_ROOT_USER" -p "$MONGO_ROOT_PASSWORD" --authenticationDatabase admin --eval "rs.status().ok"` | `1` |
| Asientos cargados | `curl -s 'http://127.0.0.1:8010/api/asientos?limit=1'` | `"total": 516`. Si la API no autentica contra Mongo: `503` con `"codigo": "mongo_no_disponible"` (§7.3) |
| OCR vivo | `curl -s http://127.0.0.1:8866/health` | `{"status":"ok", ...}` con `engines.local.loaded` y/o `engines.cloud` |
| OCR por su puerto publicado | `cd maisa/ocr_service && ./smoke_lan.sh ../data/facturas/2026-01-25_P001.pdf` | 4/4 OK, con `RESULTADO: OCR operativo por localhost Y por LAN`. Prueba el OCR **directamente**, no a través de la API; el script y su nombre son del servicio de OCR y usan la IP privada del anfitrión como comprobación local |
| API viva | `curl -s http://127.0.0.1:8010/health` | `"estado": "ok"` y `mongo.ok`/`ocr.ok`/`escritura.ok` a `true` |
| API lista | `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8010/health/ready` | `200` (503 si falta una dependencia crítica: `mongo,ocr`) |
| API desde Internet (HTTPS) | `curl -s https://82.70.78.22.sslip.io/health` | mismo JSON que por `127.0.0.1`. **Es la vía de reparto real** |
| API desde Internet (claro) | `curl -s http://82.70.78.22:8010/health` | el mismo JSON. Sigue abierta para diagnóstico; el panel **no** la usa: se sirve por HTTPS desde la propia API (§3.8) |
| Certificado TLS | `curl -sS -o /dev/null -w '%{http_code} %{ssl_verify_result}\n' https://82.70.78.22.sslip.io/health` | `200 0` (`0` = certificado válido y verificado) |
| Redirección http → https | `curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' http://82.70.78.22.sslip.io/api/meta` | `308 https://82.70.78.22.sslip.io/api/meta` |
| Proxy del OCR | `curl -s -X POST 'http://127.0.0.1:8010/api/ocr' -F file=@maisa/data/facturas/2026-01-08_P001.pdf` | `200` en 1–5 s según el motor elegido (reenvía a `ocr-api:8866`) |
| Visor | `curl -s -o /dev/null -w '%{http_code} %{content_type}\n' http://127.0.0.1:8010/` | `200 text/html` sirviendo `index.html` si existe; si no, `200 application/json` con el mensaje informativo (§7.11) |
| **Todo de una pasada** | `PUBLIC_IP=<IP pública> ./maisa/api/smoke_lan.sh --publico --engine local` | `10 de 10 OK` (14 con `--subir`). Recorre salud, estadísticas, facturas, PDF con `sha256`, asientos, snapshots, visor y `POST /api/ocr`; sale `1` diciendo qué falló |
| **Todo de una pasada, por HTTPS** | `PUBLIC_IP=82.70.78.22 ./maisa/api/smoke_lan.sh --base https://82.70.78.22.sslip.io --engine local` | el mismo `10 de 10 OK`, pero pasando por Caddy. `curl` verifica el certificado, así que un fallo aquí es un fallo real de TLS, no un falso positivo |

El `healthcheck` del contenedor usa `/health` y **no** `/health/ready` a propósito: un
contenedor debe reiniciarse si el proceso no responde, no porque Mongo esté un momento caído.
Consecuencia: **`Up (healthy)` no significa que Mongo esté accesible**. La comprobación que
cierra el arranque es `/health/ready` (`200`) y `mongo.ok: true` dentro de `/health` (§7.10).

---

## 5. Endpoints y variables de la API

Los datos van bajo `/api`. `/health`, `/health/ready` y `/` quedan fuera del prefijo.
`GET /docs` es la documentación interactiva (Swagger UI).

| Método | Ruta | Qué hace |
|---|---|---|
| `GET` | `/health` | Estado del servicio y de cada dependencia (`ok`/`error` + latencia). **Siempre 200** mientras el proceso viva. |
| `GET` | `/health/ready` | `200` si todas las dependencias críticas responden; `503` si falta alguna. |
| `GET` | `/api/facturas` | Listado paginado y filtrable desde la traza. |
| `GET` | `/api/facturas/{file_id}` | Detalle completo: motivos + hechos + campos + métricas de lectura. |
| `GET` | `/api/facturas/{file_id}/pdf` | PDF original, `Content-Disposition: inline`. |
| `GET` | `/api/asientos` | Listado paginado de `asientos` (Mongo). |
| `GET` | `/api/asientos/{asiento_id}` | Detalle de un asiento. |
| `GET` | `/api/snapshots` | Descargas del ERP (`erp_snapshots`), más recientes primero. |
| `GET` | `/api/estadisticas` | Recuento por resultado y por lote + asientos vigentes. |
| `POST` | `/api/ocr` | Proxy del OCR (multipart). |
| `GET` | `/api/meta` | Versión de la API, `version_norma` del motor y configuración no sensible. |
| `GET` | `/` | Visor estático si `UI_DIR` tiene contenido; si no, mensaje informativo. |

Parámetros de `/api/facturas`: `resultado` (`PAGAR`\|`NO_PAGAR`\|`ESCALAR`), `lote`, `proveedor`
(≤ 64), `q` (≤ `MAX_QUERY_LEN`), `limit`, `offset`. `/api/asientos` acepta `vigente`, `q`,
`limit` y `offset`; `/api/snapshots`, `limit` y `offset`.

```console
$ curl -s http://127.0.0.1:8010/health
$ curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8010/health/ready

$ curl -s 'http://127.0.0.1:8010/api/facturas?limit=2'
$ curl -s 'http://127.0.0.1:8010/api/facturas?resultado=NO_PAGAR&limit=1'
$ curl -s 'http://127.0.0.1:8010/api/facturas?q=PO-2026-0814'
$ curl -s 'http://127.0.0.1:8010/api/facturas?lote=1&proveedor=Papeler%C3%ADa'
$ curl -s http://127.0.0.1:8010/api/facturas/2026-04-08_P007.pdf
$ curl -s -D - -o /dev/null http://127.0.0.1:8010/api/facturas/2026-01-08_P001.pdf/pdf

$ curl -s 'http://127.0.0.1:8010/api/asientos?limit=1&vigente=true'
$ curl -s http://127.0.0.1:8010/api/snapshots
$ curl -s http://127.0.0.1:8010/api/estadisticas
$ curl -s http://127.0.0.1:8010/api/meta

$ curl -s -X POST 'http://127.0.0.1:8010/api/ocr?engine=cloud' -F file=@maisa/data/facturas/2026-01-08_P001.pdf
$ curl -s -X POST 'http://127.0.0.1:8010/api/ocr?engine=local&detalle=true' -F file=@factura.png
```

`GET /api/estadisticas` es la comprobación de humo más informativa: con el sistema en pie
devuelve `total: 500`, `por_resultado: {PAGAR: 448, NO_PAGAR: 9, ESCALAR: 43}` y
`asientos_vigentes: 516`. Salidas completas de ejemplo en `maisa/api/README.md` §2.

Si se define `API_KEY`, **todos** los endpoints salvo `/health`, `/health/ready`, `/docs` y
`/openapi.json` exigen la cabecera `X-API-Key`. Sin ella, la API arranca en **modo abierto**
(lo avisa en el log y en `/api/meta` → `modo_abierto: true`). El puerto 8010 está publicado en
Internet y la API **también escribe** (`POST /api/facturas`), así que hoy cualquiera puede
subir PDFs: define `API_KEY` antes de dejarlo así (`maisa/api/README.md` §2.4 y §3.8).

### 5.1 Variables de entorno

Todas las variables de la API están descritas en `maisa/api/README.md` §3 y, con su valor por
defecto, en `maisa/api/.env.example`. El compose les da valor para la red Docker, así que
**ninguna es obligatoria**. Las que importan de verdad:

| Variable | Defecto en Docker | Para qué |
|---|---|---|
| `API_PORT` | `8010` | Puerto publicado en `0.0.0.0`. 8009 es el ERP y 8866 el OCR: por eso 8010. |
| `MONGO_APP_USER` / `MONGO_APP_PASSWORD` | (de `maisa/.env`) | Credenciales con las que se construye la URI. **Es el punto que se rompe** (§7.3). |
| `MONGO_HOST` / `MONGO_PORT` | `mongo` / `27017` | DNS interno de la red compartida. |
| `MONGO_REPLICA_SET` / `MONGO_DIRECT_CONNECTION` | `rs0` / `true` | Replica set de un nodo, sin reconectar contra `127.0.0.1`. |
| `MONGO_DB` | `albertitos` | Base de datos, y también `authSource`. |
| `MONGO_AUTH_SOURCE` | `albertitos` | Base donde se autentica el usuario; si no se define, se usa `MONGO_DB`. |
| `API_HOST` | `0.0.0.0` | Solo lo usa `run_local.sh` (en Docker el bind lo fija uvicorn). |
| `MONGO_URI` | *(vacío)* | Sustituto de las piezas anteriores. Vacío a propósito (ver §3.5). |
| `MONGO_TIMEOUT_MS` | `1500` | Corto a propósito: si Mongo no está, la API se degrada en décimas, no se cuelga. |
| `OCR_URL` | `http://ocr-api:8866` | DNS interno del OCR. |
| `ERP_URL` | `http://host.docker.internal:8009` | Solo informativo: la API **no** consulta el ERP (lo hace el motor). |
| `OUTPUTS_DIR` / `FACTURAS_DIR` / `UI_DIR` | `/datos/outputs`, `/datos/facturas`, `/datos/ui` | Volúmenes de solo lectura. |
| `CORS_ORIGINS` | `http://localhost:8010`, `http://127.0.0.1:8010`, `:5173` | Solo hace falta si el visor se sirve desde otro origen; en el mismo origen no hay CORS. |
| `API_KEY` | *(vacío)* | Si está vacía, la API arranca en modo abierto. |
| `CRITICAL_DEPS` | `mongo,ocr` | Qué dependencias hacen que `/health/ready` devuelva `503`. |
| `HEALTH_TIMEOUT_S` | `2.0` | Margen por dependencia en `/health`. |
| `MAX_UPLOAD_MB`, `DEFAULT_LIMIT`, `MAX_LIMIT`, `MAX_QUERY_LEN` | `50`, `50`, `500`, `64` | Límites de subida, paginación y longitud de `q`. |
| `SUBIDAS_HABILITADAS` | `1` | Interruptor de `POST /api/facturas`. A `0`, el endpoint devuelve `403 subidas_deshabilitadas`. |
| `LOG_LEVEL` | `INFO` | Nivel de log. |

Las de los otros dos servicios, en sus propias plantillas: `maisa/.env.example`
(`MONGO_ROOT_USER`, `MONGO_ROOT_PASSWORD`, `MONGO_APP_USER`, `MONGO_APP_PASSWORD`,
`MONGO_PORT`, `MONGO_DB`, `MONGO_URI`) y `maisa/ocr_service/.env.example` (motor local y
nube: `OCR_ENGINE`, `OCR_CLOUD_*`, `OCR_DET_*`, `OCR_REC_*`, `OCR_MAX_UPLOAD_MB`,
`OCR_MAX_PAGES`, `LOG_LEVEL`).

---

## 6. Advertencia: el 27017 no se toca

> ## ⚠️ El puerto de MongoDB se queda en `127.0.0.1:27017`
>
> **Nunca** cambiar `127.0.0.1:${MONGO_PORT:-27017}:27017` por `0.0.0.0:...` en
> `maisa/docker-compose.yml`. Lo prohíben **D-12** (`diseño_conceptual.md`: «Puerto solo en
> `127.0.0.1`» — «el ERP y el OCR son locales; no hay motivo para exponer la BD»),
> **§13.1** (`diseño_logico.md`: «Solo accesible desde la propia máquina. No cambiar a
> `0.0.0.0`») y el **RNF-10** (mínimo privilegio, puerto no expuesto).
>
> Si el frontend o el equipo necesitan datos del ERP, se piden a la **API** (`/api/asientos`,
> `/api/snapshots`), que es la única que habla con Mongo. La API lee el catálogo del ERP y las
> decisiones, y solo **escribe** lo suyo: `POST /api/facturas` guarda expedientes y eventos
> (§`maisa/api/README.md` §3.8).
> El motivo completo y las alternativas descartadas están en
> `maisa/docs/ADR-0001-middleware-bff.md`.

---

## 7. Problemas conocidos y su causa real

### 7.1 Desde dentro de una red Docker, usar el DNS interno — nunca la IP del anfitrión

Dentro de `albertitos_net`, `http://ocr-api:8866/health` y `mongo:27017` funcionan;
`http://10.0.0.75:8866/health` (la IP privada del anfitrión) falla con
`curl: (7) ... Host is unreachable`.

**Causa**: la regla `iptables -t nat` que publica el puerto excluye el tráfico que entra por
la propia interfaz puente (`-A DOCKER ! -i br-...`). El paquete no se redirige al contenedor,
acaba tratándose como entrega local y la cadena `INPUT` lo rechaza. Desde fuera de Docker
(otra máquina) esa exclusión no aplica y el DNAT sí funciona. Documentado en
`maisa/ocr_service/README.md` («Dos avisos importantes»).

Consecuencia práctica: los valores por defecto de `maisa/api/docker-compose.yml` son los
correctos **porque** usan el DNS interno (`MONGO_HOST=mongo`, `OCR_URL=http://ocr-api:8866`).
Si se ejecuta la API en el anfitrión, `run_local.sh` los reescribe a `127.0.0.1`.

### 7.2 El host tiene la cadena `INPUT` endurecida

Solo acepta `lo` y el puerto 22, y rechaza el resto. **No** afecta a los puertos publicados
por Docker (se atienden por DNAT/`FORWARD`), pero sí a cualquier servicio que escuche en el
host sin publicarse: el ERP en `127.0.0.1:8009` no es alcanzable desde fuera ni cambiando su
bind, porque la cadena lo corta.

### 7.3 Credenciales de Mongo: manda el `.env` con el que se inicializó el volumen

Los usuarios de Mongo se crean **una sola vez**, cuando el volumen `albertitos_mongo_data`
está vacío. A partir de ahí, cambiar `maisa/.env` no cambia la contraseña de la base: solo
cambia la que el cliente envía. Pasó en el montaje del 2026-09-19 (se copió la plantilla sin
rellenar y el volumen ya existía de un arranque anterior) y se resolvió rellenando el `.env`
con las credenciales del volumen y recreando la API. Merece la pena conocer el síntoma,
porque **no rompe el arranque**:

| Dónde | Síntoma |
|---|---|
| `GET /health` | `"estado": "degradado"`, `dependencias.mongo.ok: false` |
| `GET /health/ready` | `503` |
| `GET /api/asientos`, `/api/snapshots` | `503` con `{"codigo": "mongo_no_disponible"}` |
| `GET /api/estadisticas` | `200` (lee la traza del disco), pero `asientos_vigentes: null` |
| Log de la API | `Authentication failed.` (código 18) y `No se pudieron comprobar los indices al arrancar` |

El error de autenticación puede llegar disfrazado: junto a `Authentication failed` el driver
reporta `client is configured to connect to a replica set named 'rs0' but this node belongs to
a set named 'None'`. Es el mismo fallo visto desde otro sitio: sin completar la
autenticación, el driver no llega a leer la configuración del replica set y no ve el
`setName`. Que Mongo está bien se comprueba con sus propias credenciales (§4: `rs.status().ok`
= `1`).

**Arreglo, en orden de preferencia:**

1. **Poner en `maisa/.env` las credenciales con las que se creó el volumen** (las que
   funcionan: las mismas que usa `docker exec albertitos-mongo`). Es lo correcto: no toca los
   datos y basta con recrear la API para que recoja el `.env` nuevo.
2. Si esas credenciales se han perdido: rellenar `maisa/.env` con las nuevas y **reinicializar
   el volumen** (`docker compose -f maisa/docker-compose.yml down -v`, y volver a §3.2). Es
   destructivo: se pierden los asientos y hay que reimportarlos.

Ojo al revés también: recrear la API **no** basta si el `.env` no se ha corregido antes, y
corregir el `.env` **no** basta si la API no se recrea (las variables entran al crear el
contenedor, no al arrancarlo).

El mismo síntoma aparece ejecutando la API **en el anfitrión** con `run_local.sh`: el script
carga `maisa/.env` pero, si `MONGO_URI` no está definida, fija una URI a `127.0.0.1` **sin
credenciales** y avisa por stderr. Se arregla definiendo `MONGO_URI` con usuario y contraseña
antes de lanzarlo.

### 7.4 El `&` de la URI rompe `source .env`

`MONGO_URI` lleva `&` y sin comillas bash corta la línea en el ampersand: la variable queda
sin definir y el resto se interpreta como comandos. Por eso `run_local.sh` **parsea** el
fichero línea a línea en vez de hacer `source`, y por eso la plantilla
`maisa/api/.env.example` pone el valor **entre comillas**.

### 7.5 Los `env_file` y la interpolación de Compose: sin `.env` no hay credenciales

Tres formas distintas de que falte un fichero, y tres efectos distintos:

| Fichero | Cómo lo usa compose | Si falta |
|---|---|---|
| `maisa/ocr_service/.env` | `env_file: .env` (obligatorio) | Aborta: `env file .../.env not found: ... no such file or directory`. |
| `maisa/.env` | **Interpolación** de `${MONGO_ROOT_USER}`, `${MONGO_APP_USER}`… (compose lo carga solo del directorio del proyecto) | No aborta: las variables se expanden a **vacío** y Mongo arranca sin credenciales. |
| `maisa/api/.env` | Sustituto opcional de los valores por defecto | No pasa nada: el compose de la API no lo declara. Sus credenciales las toma de `maisa/.env` vía `env_file: ../.env` con `required: false`, así que si `maisa/.env` no existe la API **arranca degradada**, no caída. |

Los ficheros `.env` están en `.gitignore`: en un clon nuevo hay que crearlos desde sus
`.env.example`.

### 7.6 El entrypoint de Mongo se monta vía `/bin/sh`

`docker-compose.yml` invoca `entrypoint: ["/bin/sh", "/usr/local/bin/mongo-keyfile-entrypoint.sh"]`
en lugar de ejecutar el script directamente. El script se monta desde el repo y así no
depende de que el **bit de ejecución** sobreviva a la copia (zip, Windows, filesystems sin
`exec`). Tiene shebang, pero esto lo blinda.

### 7.7 `albertitos_net` es externa: debe existir antes de cualquier `up`

Los tres composes la declaran `external: true` con `name: albertitos_net`. Nadie la crea: la
crea el operador una vez. La ventaja buscada es que su nombre no dependa del directorio ni del
project name, y que el `down` de un proyecto no se la lleve por delante mientras otro
contenedor siga conectado.

### 7.8 Primer arranque en frío: el `up` puede abortar y hay que repetirlo

Con el volumen vacío, la inicialización de Mongo (crear el usuario root, ejecutar los scripts,
escribir el oplog) tarda más que el margen del healthcheck, y `docker compose up -d` aborta
con `dependency failed to start: container albertitos-mongo is unhealthy`. **No es un fallo**:
el contenedor queda sano por su cuenta. Basta repetir `docker compose up -d`. En arranques
posteriores no ocurre.

### 7.9 Estado observado de `ocr-api` el 2026-09-19

El contenedor `ocr-api` en marcha lleva etiquetas del proyecto compose **`paddleocr`**
(`com.docker.compose.project=paddleocr`, config file `/home/ubuntu/projects/PaddleOCR/docker-compose.yml`),
un fichero que ya **no existe** en disco, y está unido a `albertitos_net` además de a
`paddleocr_default`. Levantarlo desde `maisa/ocr_service` recreará el contenedor con el mismo
nombre `ocr-api` (proyecto `ocr_service`): es lo esperado y no rompe nada, pero conviene
saberlo antes de dar por hecho qué compose gobierna ese contenedor.

### 7.10 `Up (healthy)` en la API no significa que Mongo esté accesible

El `healthcheck` del contenedor pega a `/health`, que **siempre** devuelve `200` mientras el
proceso viva (a propósito: el contenedor debe reiniciarse si el proceso no responde, no porque
una dependencia esté caída un momento). Por eso `docker ps` puede mostrar `albertitos-api
Up (healthy)` con Mongo en rojo y `/health/ready` en `503`.

Para saber si el sistema está entero hay que mirar el **cuerpo** de `/health`
(`"estado": "ok"` y `mongo.ok`/`ocr.ok` a `true`) o el código de `/health/ready` (`200`).
`escritura.ok` es informativa y **no** crítica: si Mongo no acepta escrituras, la API sigue
sirviendo lectura y solo falla `POST /api/facturas`.

### 7.11 El visor se comprueba en cada petición (ya no hace falta reiniciar)

`maisa/api/app/main.py` (`_montar_ui()`) decide **en cada petición** qué sirve en `GET /`: si
existe `UI_DIR/index.html` devuelve el fichero y, si no, el mensaje informativo en JSON. El
`StaticFiles` montado en `/` queda solo como red de seguridad para el resto de ficheros del visor
(`app.js`, `style.css`…).

Antes la decisión se tomaba **al arrancar**, lo que producía dos comportamientos confusos (crear o
borrar el `dist/` no se notaba hasta recrear el contenedor, y `GET /` podía quedarse en `404` con el
`index.html` ya borrado). Eso está corregido; ahora:

| Estado de `UI_DIR` | `GET /` |
|---|---|
| tiene `index.html` (hay build) | `200 text/html` con el panel. |
| sin `dist/`, o sin `index.html` dentro | `200 application/json` con el mensaje informativo. |

`GET /api/meta` → `configuracion.ui.disponible` refleja exactamente esa misma condición
(`(ui_dir / "index.html").is_file()`), así que ya no puede decir `true` mientras `/` devuelve el
mensaje o un `404`.

> **Recordatorio:** el código va **dentro de la imagen** (`COPY app ./app`), así que un cambio en
> `maisa/api/app/` no se aplica reiniciando el contenedor: hay que reconstruir
> (`docker compose -f maisa/api/docker-compose.yml up -d --build`). Los cambios de *contenido* en
> `maisa/ui/` sí son en caliente, porque es un bind mount.

### 7.12 El smoke test de la API acepta las dos respuestas de `/`

`maisa/api/smoke_lan.sh` recorre las 10 comprobaciones del despliegue (salud por la IP pública y
por `127.0.0.1`, estadísticas, facturas con motivos y hechos, el PDF con su `sha256`
comparado, asientos, snapshots, el visor y un `POST /api/ocr` real). El nombre del fichero es
histórico: la LAN **no** se prueba, porque no es una vía de consumo (ver `maisa/api/README.md`
§2.1). Las bases se pasan con `--publico` (necesita `PUBLIC_IP`) o con `--base <URL>` (repetible), y
`--subir` añade el ciclo de escritura (`201`, `200 duplicado`, expediente y PDF desde GridFS) y
deja **14** comprobaciones. La comprobación de `/` da por
bueno **cualquiera de los dos** casos de §7.11: `text/html` (hay build) o `application/json` (no lo
hay). Antes exigía `text/html` y fallaba legítimamente sin build. El resto de la salida
es la mejor comprobación de una pasada que hay en el repo.

### 7.13 El proxy salía `(unhealthy)` aunque el HTTPS funcionaba

`albertitos-proxy` aparecía como `Up (unhealthy)` con `https://82.70.78.22.sslip.io/health`
devolviendo `200`. La causa estaba en el `healthcheck`, que sondeaba
`http://127.0.0.1:80/`: Caddy responde **siempre** `308` hacia https en el 80, y al seguir el
redirect el cliente intenta TLS contra `127.0.0.1`. Como el certificado es del **nombre** y no
de la IP, Caddy rechaza el saludo (SNI desconocido) y `wget` muere con `SSL alert number 80` /
`Connection reset by peer`. El proxy estaba perfecto; el que se equivocaba era el sondeo.

Sondear el 443 tampoco vale: ataría la salud del proxy a la de la API (un reinicio de la API
marcaría el proxy como enfermo). Caddy no tiene endpoint de salud propio, así que el
`healthcheck` pregunta al **admin API** (`admin 127.0.0.1:2019` en el `Caddyfile`, escuchando
solo en loopback **dentro** del contenedor y sin publicarse):

```console
$ docker exec albertitos-proxy wget -q --spider http://127.0.0.1:2019/config/ && echo OK
OK
```

Eso comprueba lo que de verdad importa —Caddy vivo **con su configuración cargada**— sin
depender del certificado (que tarda unos segundos en emitirse) ni de la API.

---

## 8. Qué NO está implementado todavía

| Pendiente | Estado real |
|---|---|
| ~~**Frontend/visor completo** (`maisa/ui/`)~~ | **Resuelto.** El panel está en el repo y lo sirve la API en `/` (`UI_DIR=/datos/ui/dist`, montado como `/datos/ui:ro`). No hay proceso que arrancar: el único paso es construirlo (`npm ci && npm run build` en `maisa/ui`, §3.8) y el contenedor lo sirve en la petición siguiente, sin reiniciar (§7.11). Se ve en `http://127.0.0.1:8010/` y en `https://82.70.78.22.sslip.io/`, con las rutas del panel (`/facturas`, `/trazabilidad`, `/escalabilidad`). Sin build, `/` sigue devolviendo el mensaje informativo y `ui.disponible` es `false`. |
| **Persistencia en Mongo de `expedientes` y `eventos`** | **Parcial.** `POST /api/facturas` ya escribe expedientes y eventos (subidas por la API). Lo que sigue sin escribir es el **motor**: las decisiones viven solo en `outputs/outcomes_traza.jsonl`, y `ejecuciones` y `excel_filas` están **vacías** (`TRASPASO.md` §1: «Persistencia Mongo (`expedientes`…) — a hacer»). Cuando el motor escriba ahí, `GET /api/facturas` debería preferir Mongo. |
| **Autenticación real** | Hoy `API_KEY` es una **clave compartida**, no usuarios ni roles. El usuario que usa la API (`albertitos_app`) tiene `readWrite` sobre `albertitos` (lo necesita para `POST /api/facturas`). |
| ~~TLS~~ | **Resuelto.** La API se reparte por `https://82.70.78.22.sslip.io` con certificado de Let's Encrypt (`maisa/proxy/`, §3.6). El `8010` en claro sigue publicado a propósito, para diagnóstico. Lo único que **falta** de este frente es cerrar el `8010` al público cuando ya nadie lo necesite. (El otro punto que quedaba —fijar el origen del visor en `CORS_ORIGINS`— quedó sin objeto: el panel lo sirve la API en el mismo origen, §3.8.) |
| **Cierre del `8866` del OCR** | Sigue publicado en `0.0.0.0:8866` por su propio compose, ahora que el frontend entra por `/api/ocr`. Decisión pendiente (ADR-0001 §5). |
| **`GET /api/asientos/{asiento_id}` no filtra por `vigente`** | Con un solo snapshot es equivalente; con varios habrá que decidir cuál devolver. |
| **Caché/ETag en el listado** | Con 500 facturas la traza cabe en memoria; si el volumen crece, tocará paginar desde Mongo y cachear. |

---

## 9. Estado verificado

**2026-09-19, 17:20–17:45 UTC** (arranque completo) y **2026-09-20, 00:40–01:00 UTC** (proxy TLS).
Todo en verde. En aquel momento el panel todavía no se había construido, así que `/` devolvía el
JSON informativo: es una respuesta **correcta** y el smoke la da por buena (§7.11 y §7.12). El panel
ya construido y servido por el contenedor se verificó aparte, el 2026-09-20 (§9.1):

| Pieza | Verificación | Resultado |
|---|---|---|
| `albertitos-mongo` | `docker ps` | `Up (healthy)`, `127.0.0.1:27017->27017/tcp` |
| Replica set | `rs.status()` con el usuario root | `ok=1`, `set=rs0`, miembro `PRIMARY` |
| `asientos` / `erp_snapshots` | `countDocuments` con el usuario de app | `asientos=516`, `vigentes=516`, `snapshots=1` |
| `albertitos-mongo-init` | `docker ps -a` | `Exited (0)` (correcto) |
| `ocr-api` | `docker ps`, `curl /health` | `Up (healthy)`, `0.0.0.0:8866->8866`, `status: ok`; motores local y nube disponibles |
| Redes | `docker network inspect albertitos_net` | `albertitos-mongo` (172.20.0.2), `ocr-api` (172.20.0.3) y `albertitos-api`; alias `ocr-api` y `ocr` |
| ERP simulado | `ss -ltnp` | `python3 ... alberto_erp.py --puerto 8009` en `127.0.0.1:8009` |
| Traza y entrega | `wc -l` y recuento | `outcomes.jsonl` y `outcomes_traza.jsonl`: 500 líneas; `PAGAR 448 / NO_PAGAR 9 / ESCALAR 43` |
| Autenticación de Mongo | lectura anónima desde el propio contenedor | rechazada: `Command aggregate requires authentication` |
| Tests de la API | `.venv-api/bin/python -m pytest maisa/api` | `92 passed` (no necesitan Mongo ni OCR). El `-q` ya viene en `maisa/api/pytest.ini`; añadir otro `-q` a mano lo deja en `-qq` y **se come el resumen** |
| `albertitos-api` | `docker ps`, `docker port` | `Up (healthy)`, `0.0.0.0:8010->8000/tcp`, imagen `albertitos-api:latest`, usuario `apiuser` |
| `albertitos-proxy` | `docker ps` | `Up (healthy)`, `0.0.0.0:80->80/tcp`, `0.0.0.0:443->443/tcp`, imagen `caddy:2.8-alpine` |
| Certificado TLS | `openssl s_client -connect 82.70.78.22.sslip.io:443` | `CN=82.70.78.22.sslip.io`, emitido por `Let's Encrypt (YE2)`, válido del 2026-09-19 al 2026-12-18; Caddy lo renueva solo |
| API desde Internet (HTTPS) | `GET https://82.70.78.22.sslip.io/health` | `200` con `ssl_verify_result=0` (certificado **verificado**, no `--insecure`) |
| HTTP → HTTPS | `GET http://82.70.78.22.sslip.io/api/meta` | `308` a `https://82.70.78.22.sslip.io/api/meta` |
| IP real en el log de la API | `docker logs albertitos-api` tras una petición por HTTPS | `82.70.78.22:0 - "GET /health"`: `FORWARDED_ALLOW_IPS` está surtiendo efecto (sin él saldría la IP del contenedor de Caddy) |
| Smoke test por HTTPS | `PUBLIC_IP=82.70.78.22 ./maisa/api/smoke_lan.sh --base https://82.70.78.22.sslip.io --engine local` | **10 de 10 OK**, todo el recorrido pasando por Caddy |
| API, dependencias | `GET /health` | `"estado": "ok"`, `mongo.ok: true`, `ocr.ok: true`, `escritura.ok: true` |
| API lista | `GET /health/ready` | `200` |
| API desde Internet (claro, 8010) | `GET http://82.70.78.22:8010/health`, `/`, `/api/facturas?limit=1` | `200` en los tres |
| Puertos no publicados | `curl --max-time 5 http://82.70.78.22:{8866,27017,8009}/health` | `000` en los tres (OCI descarta el paquete; el reparto es correcto) |
| API contra Mongo | `GET /api/estadisticas` | `total: 500`, `PAGAR 448 / NO_PAGAR 9 / ESCALAR 43`, `asientos_vigentes: 516`, `mongo.ok: true`, `lineas_invalidas: 0`, `coincide_con_traza: true` |
| Asientos y snapshots | `GET /api/asientos?limit=1`, `GET /api/snapshots` | `total: 516`; 1 snapshot (`snap-2026-09-19T08-25-58Z`, 516 asientos, 26 páginas, `vigente: true`) |
| Proxy del OCR | `POST /api/ocr` con `2026-01-08_P001.pdf` | `200` en ~4,7 s; `motor: cloud`, `paginas: 1`, `stats.regions: 12` (el OCR tiene `OCR_ENGINE=auto`) |
| Smoke test completo | `PUBLIC_IP=82.70.78.22 ./maisa/api/smoke_lan.sh --publico --engine local --subir` | **14 de 14 OK**: salud (con `escritura`), estadísticas, facturas, PDF con `sha256`, asientos, snapshots, visor (`/` devuelve el JSON informativo, que es una respuesta válida: §7.11 y §7.12), `POST /api/ocr` con motor local y el ciclo de subida completo (`201`, `200 duplicado`, expediente y PDF desde GridFS) |

El log de arranque de la API no avisa de índices que falten
(`No se pudieron comprobar los indices` / `indices_faltantes`): con credenciales válidas la
comprobación pasa.

**Lo que sigue sin verificar**: que el motor de nube del OCR esté *siempre* disponible — aquí
respondió por la nube, pero si su cuota o su token fallan el OCR debe caer al motor local, y
ese camino no se ha forzado a propósito. Tampoco se ha probado la API con `API_KEY` definida
(hoy corre en modo abierto, ver §8). Y del panel, en aquella prueba solo se comprobó que `/`
respondía con el mensaje informativo en JSON, porque todavía no había build (§9.1).

Del lado del HTTPS, lo comprobado es la **tubería** (certificado verificado, `308` en el 80,
smoke de 10 comprobaciones por Caddy), no la integración con el visor: falta abrir el visor en
Vercel contra `https://82.70.78.22.sslip.io` y, en esa prueba, fijar su origen exacto en
`CORS_ORIGINS` en lugar del `*` que hoy tiene `maisa/api/.env`. Con `*`, un fallo de CORS no
saldría aquí: aparecería solo en la consola del navegador.

Esa última reserva quedó sin objeto cuando el panel pasó a servirse **desde la propia API**
(§9.1): en el mismo origen no hay CORS que fijar, ni visor en Vercel que probar. Lo que sigue en
pie es la parte del OCR y la de `API_KEY`.

### 9.1 El panel, servido por el contenedor (2026-09-20, 07:15 UTC)

Esta es la verificación que cierra el §8 para el panel. No se arrancó ningún servidor de
desarrollo: el panel lo sirve `albertitos-api` desde `/datos/ui/dist`.

| Comprobación | Cómo | Resultado |
|---|---|---|
| Build | `cd maisa/ui && npm run typecheck && npm run build` | `tsc` limpio; `✓ built`, `dist/assets/index-CTpyT83I.js` (1.029.498 B; el log de Vite dice 1.029,10 kB / 312,73 kB gzip) |
| Paridad del corpus | `node tools/verificar_paridad.ts` | `Todo cuadra.` |
| Panel por el contenedor | `GET http://127.0.0.1:8010/trazabilidad` | `200`, sirviendo el bundle recién construido |
| Panel por HTTPS | `GET https://82.70.78.22.sslip.io/trazabilidad` | `200` (por Caddy, sin tocar la API) |
| La API sabe que hay panel | `GET /api/meta` | `configuracion.ui.disponible: true` |
| El panel **funciona**, no solo responde | render *headless* de `/trazabilidad` contra `127.0.0.1:8010` | **0 errores de JavaScript**; «API viva» y el selector con las 540 facturas |
| Sin reiniciar | el contenedor arrancó a las 06:49 UTC y sirvió un `dist` escrito a las 07:07 (18 min después) | servido en caliente, como dice §7.11 |
| Varias formas de expediente | 6 renders: `P006`, catering, `P001`, `FA-5590`, `scan_002`, `F26-7728` | 0 errores en los seis; los tres casos de prosa (instrucción marcada, solo anomalía de regla, sin señal) salen como deben |

Dos consecuencias que conviene tener presentes:

- **`dist` no está versionado ni viaja en la imagen.** Un clon recién bajado no tiene panel: hay
  que construirlo (§3.8). Mientras no lo haya, `/` responde el JSON informativo y `ui.disponible`
  es `false` — degradación, no fallo.
- **No hay proceso que arrancar ni que parar.** El `npm run preview` del puerto 4173 es solo una
  lupa local para mirar `dist/` sin contenedor; no forma parte del despliegue y, si se usa, hay
  que acordarse de pararlo porque ocupa el puerto.
