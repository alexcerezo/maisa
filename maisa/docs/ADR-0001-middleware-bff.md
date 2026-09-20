# ADR-0001 — Middleware/BFF como única superficie publicada

| Campo | Valor |
|---|---|
| **Fecha** | 2026-09-19 |
| **Estado** | Aceptada |
| **Ámbito** | `maisa/api/` (nuevo), `maisa/docker-compose.yml`, `maisa/ocr_service/docker-compose.yml`, `maisa/ui/` |
| **Decisiones previas relacionadas** | D-11 y D-12 (`diseño_conceptual.md`), §13 (`diseño_logico.md`), RNF-10 |

---

## 1. Contexto

El sistema son tres piezas que ya existían por separado: el **motor de decisión**
(`motor/`, Python), el **servicio de OCR** (`ocr_service/`, contenedor `ocr-api`) y
**MongoDB 7** (`albertitos-mongo`, replica set `rs0` de un nodo, autenticación obligatoria
por `keyFile`). Las tres viven en **esta máquina**.

La documentación previa tomaba dos decisiones explícitas y **no prevía ninguna capa
intermedia**:

- **D-12** — «Puerto solo en `127.0.0.1`» frente a `0.0.0.0`, con el motivo «el ERP y el OCR
  son locales; no hay motivo para exponer la BD» (`diseño_conceptual.md` §10).
- **§13** — el compose publica `127.0.0.1:${MONGO_PORT:-27017}:27017` con el comentario
  «Solo accesible desde la propia máquina. No cambiar a `0.0.0.0`» (`diseño_logico.md`
  §13.1), y el RNF-10 exige «mínimo privilegio, puerto no expuesto» (auth + usuario de app
  + bind a `127.0.0.1`).
- El cliente del ERP/Mongo era **el propio motor**: `maisa/.env.example` documenta
  `MONGO_URI` como «la cadena de conexión del usuario de aplicación, lista para consumir
  (la lee el motor)». Y la UI se describía como «visor HTML estático de trazas (bonus)»,
  sin backend.

Eso era coherente mientras **todo** viviera en la misma máquina. Dejó de serlo cuando los
consumidores —el frontend y el resto del equipo— **no están en esta máquina**: la instancia
está en Oracle Cloud y se llega a ella por Internet. Aparece una
necesidad nueva: publicar *algo* fuera del anfitrión, y ninguna pieza existente lo hacía sin
contradecir D-12. Ese es el hueco que este ADR cierra.

---

## 2. Decisión

Se introduce un **middleware/BFF** (`maisa/api/`, FastAPI + uvicorn) que es la **única
superficie nueva publicada**: `0.0.0.0:${API_PORT:-8010}` → `8000` del contenedor,
`albertitos-api`, en la red `albertitos_net`, corriendo como usuario **no root** (`apiuser`,
uid 10001).

Reglas que impone la decisión:

1. **La API es el único cliente de Mongo.** La BD sigue escuchando **solo** en
   `127.0.0.1:27017` del anfitrión y en el DNS interno `mongo:27017` dentro de
   `albertitos_net`. Las credenciales no salen del servidor: el navegador nunca ve una
   cadena de conexión.
2. **La API es de solo lectura sobre Mongo** (`find`, `count_documents`, `aggregate`) y
   sobre los datos del motor (`outputs/*.jsonl`, `data/facturas/`, `ui/`, montados `:ro`).
3. **La API no decide nada.** Las decisiones y sus motivos siguen viviendo en el motor
   (`motor/src/maisa/`) y se leen desde `outputs/outcomes_traza.jsonl`. La API es una
   *vista*, no una segunda fuente de verdad.
4. **La API sirve el visor en el mismo origen** (`UI_DIR` montado en `/`), de modo que el
   navegador habla con la API sin CORS y sin credenciales.
5. **La API hace de proxy del OCR**: el frontend sube la factura a `/api/ocr` y no necesita
   conocer `ocr-api`.
6. **Las credenciales de Mongo viven en un solo sitio**: `maisa/.env` (no versionado), que el
   compose inyecta en la API como `env_file: ../.env`. La API **no** recibe una URI completa:
   la compone con esas piezas (`MONGO_APP_USER`/`MONGO_APP_PASSWORD` + `MONGO_HOST`…) y las
   anula en los logs (`sanitize_uri()`); lo único que sale hacia el navegador es
   `/api/meta` con la URI saneada. El compose tampoco le pasa las credenciales de *root*, que
   no necesita.

### Topología

```
   Equipo del resto                     MÁQUINA SERVIDORA (esta)
   (frontend, motor,                    ┌────────────────────────────────────────────┐
    herramientas)                       │                                            │
        │                               │   albertitos-api                           │
        │   HTTP  ─────────────────────▶│   0.0.0.0:8010 -> 8000                     │
        │   (única puerta nueva)        │   FastAPI/uvicorn, usuario no root         │
        │                               │        │                                   │
        │                               │        ├── albertitos_net ─▶ mongo:27017    │
        │                               │        │      (en el host: 127.0.0.1:27017)│
        │                               │        ├── albertitos_net ─▶ ocr-api:8866   │
        │                               │        └── disco (solo lectura):            │
        │                               │             outputs/*.jsonl, data/facturas, │
        │                               │             ui/                             │
        │                               └────────────────────────────────────────────┘
        │
   El motor (proceso local) sigue hablando DIRECTO con Mongo (127.0.0.1:27017)
   y con el ERP (127.0.0.1:8009). La API no lo sustituye: es la puerta de los
   consumidores que no están en esta máquina.
```

### Qué está expuesto y qué no

```
   Puerto    Servicio              Interfaz publicada     Abierto en el NSG (Internet)
   ------    -------------------   -------------------    ----------------------------
   8010      albertitos-api       0.0.0.0:8010           SÍ   <- única superficie publicada
   8866      ocr-api              0.0.0.0:8866           NO   <- solo red Docker
   27017     albertitos-mongo     127.0.0.1:27017        NO   <- cerrado a propósito (D-12)
   8009      ERP simulado         127.0.0.1:8009         NO   <- solo loopback del host
```

> «Publicado» es lo que expone Docker en el host; la columna del NSG es lo que el cortafuegos
> de la VCN deja entrar desde fuera. El `ocr-api` **sí** sigue publicado en `0.0.0.0:8866` por
> su propio `ocr_service/docker-compose.yml` (`ports: "8866:8866"`), que es anterior a este ADR,
> pero el NSG **no** abre el 8866: no es alcanzable desde Internet (ni desde la LAN). Lo que
> este ADR fija es que **la API no lo publica** y que el frontend no necesita esa puerta: entra
> por `/api/ocr`. Cerrar el 8866 en el host es una decisión pendiente, no un efecto de este ADR
> (ver §5).

---

## 3. Alternativas consideradas

### (a) Publicar MongoDB en `0.0.0.0:27017` — descartada

Es la solución de un solo carácter y la que menos código añade. Se descarta por cinco
motivos concretos:

| Motivo | Detalle |
|---|---|
| Rompe D-12 y el RNF-10 | La regla está escrita dos veces (`diseño_conceptual.md` D-12, `diseño_logico.md` §13) y el RNF-10 pide «puerto no expuesto». Sería revocar una decisión documentada, no ampliarla. |
| Credenciales de BD en el navegador | Para consultar, el visor necesitaría la cadena de conexión. Hoy no la tiene ni la necesita. |
| Acopla el frontend al esquema | El visor pasaría a conocer colecciones, validadores e índices; un cambio en `02-schema-init.js` rompe el cliente. |
| Acceso total a la base para cualquier cliente | El usuario de app tiene `readWrite` sobre `albertitos`: quien tenga la clave puede **escribir**, no solo leer. |
| Sin punto único de CORS, auth ni observabilidad | Cada cliente resolvería por su cuenta los orígenes, la autenticación y el diagnóstico. |

### (b) Cada cliente (frontend, motor, herramientas) habla directo con Mongo y con el OCR — descartada

Es la topología que la documentación asumía de facto. Se descarta porque **multiplica los
puntos donde viven las credenciales** (tantos como clientes, cada uno con su copia de
`MONGO_URI`) y porque, para que funcione desde fuera de la máquina, **obliga a abrir Mongo**
— es decir, arrastra el problema de (a) en vez de evitarlo. Además cada cliente tendría que
implementar paginación, validación de entrada y saneado de consultas por separado.

### (c) Túnel, VPN o `ssh -L` — descartada

Técnicamente correcta y **suficiente para un equipo pequeño**: no añade código, no expone
puertos y reutiliza la autenticación de SSH. Se descarta porque **traslada la fricción a
cada cliente** (cada persona mantiene su túnel, y un túnel caído se ve como «la app no
funciona») y porque **no ofrece una superficie HTTP estable**: un visor servido por
`file://` o por un `python -m http.server` local no puede subir facturas al OCR ni resolver
rutas relativas contra una API. Para un frontend que se abre desde el navegador de otra
persona, hace falta una URL, no un túnel por usuario.

### (d) Middleware/BFF — elegida

Una API HTTP en el medio: **una URL, una lista blanca de orígenes, una clave, un `/health`**.
El OCR y Mongo quedan detrás, sin exponerse. El frontend se sirve desde el mismo origen que
la API, así que no hay CORS ni credenciales en el navegador. A cambio se asume un servicio
más que operar y un contrato que mantener (§4).

---

## 4. Consecuencias

### Positivas

| # | Consecuencia |
|---|---|
| 1 | **Un solo punto de CORS, autenticación y observabilidad.** `CORS_ORIGINS`, `API_KEY` y `/health` + `/health/ready` existen una vez, no una por cliente. |
| 2 | **Mongo sigue sin exponerse.** Se cumple D-12 y el RNF-10 sin excepciones. |
| 3 | **El frontend no conoce la topología interna.** Solo necesita una URL; si Mongo o el OCR cambian de host o de puerto, se toca una variable de entorno de la API y el visor no se entera. |
| 4 | **El OCR se consume sin publicarlo dos veces.** El visor sube la factura a `/api/ocr`; el proxy reenvía a `ocr-api:8866` por la red interna y propaga los errores con su código (422, 503…), nunca como un 500 opaco. |
| 5 | **Trazabilidad de la entrega intacta.** La API lee `outcomes.jsonl` y `outcomes_traza.jsonl` con recarga automática; el motor sigue siendo el único que escribe. |

### Negativas (asumidas)

| # | Consecuencia | Mitigación actual |
|---|---|---|
| 1 | **El frontend depende de la API**: el contrato (`/api/...`) pasa a ser un compromiso de compatibilidad. | Los endpoints están documentados en `maisa/api/README.md` y cubiertos por tests (`maisa/api/tests/`, 55 tests que no necesitan ni Mongo ni OCR). |
| 2 | **Un servicio más que operar** (build, healthcheck, logs, reinicio). | Contenedor con `restart: unless-stopped`, `HEALTHCHECK` contra `/health` (no `/health/ready`, que devuelve 503 cuando una dependencia cae) y usuario no root. |
| 3 | **Salto de red adicional** en cada consulta. | Los datos calientes (la traza, 500 líneas) se leen de disco en memoria; solo el catálogo del ERP va a Mongo. |
| 4 | **La API es un punto único de fallo para los consumidores externos.** | Si la API cae, el motor sigue funcionando: no depende de ella. La traza se regenera en disco y la API la relee. |

### Explícito: el BFF **no** es una segunda fuente de verdad

La API **no decide, no recalcula y no reescribe**: no hay reglas de negocio en `maisa/api/`.
Sirve lo que el motor ya decidió (`result`, `motivos`, `hechos`, `campos`) y lo que el ERP ya
cargó (`asientos`). Por eso también es **de solo lectura** en Mongo y monta `outputs/`,
`data/facturas/` y `ui/` como `:ro`. Si mañana hay que cambiar una regla, se cambia en
`motor/config/reglas.toml` y se reejecuta el motor; la API solo mostrará otro resultado.

---

## 5. Estado

**Aceptada el 2026-09-19.** La topología descrita está implementada y en pie (ver el runbook
`maisa/docs/arranque_servicios.md` para el arranque y el detalle de lo verificado).

> **Despliegue observado el 2026-09-19.** Los tres contenedores están en pie y la API publica
> `0.0.0.0:8010` sirviendo `/health`, `/api/*` y el visor; `27017` sigue en `127.0.0.1`.
> Durante el montaje hubo un desajuste de credenciales (la API no autenticaba contra Mongo
> porque `maisa/.env` llevaba los valores de la plantilla y el volumen de la base se había
> inicializado con otros): `/health/ready` devolvía `503` y `/api/asientos` respondía
> `mongo_no_disponible`, **con la topología intacta**. Se resolvió rellenando el `.env` y
> recreando el contenedor, sin tocar la decisión. Causa, síntoma y arreglo en
> `maisa/docs/arranque_servicios.md` §7.3; el estado verificado final, en su §9.

> **TLS añadido el 2026-09-19 (no altera la decisión).** El visor se sirve desde **Vercel**, es
> decir por HTTPS, y un navegador en una página HTTPS **no puede** llamar a `http://…:8010`: el
> bloqueo por *mixed content* ocurre antes de que la petición salga. Como no hay dominio, se puso un
> **Caddy** (`maisa/proxy/`) que termina TLS para `82.70.78.22.sslip.io` —un nombre de `sslip.io`,
> DNS público que resuelve a la IP escrita en el nombre, así que Let's Encrypt emite un certificado
> válido— y reenvía a `albertitos-api:8000` por `albertitos_net`. El NSG abre **80** y **443**.
>
> Esto **no** cambia la arquitectura del ADR: sigue habiendo un único servicio de aplicación
> publicado y Mongo sigue en loopback. El proxy es un terminador, no un BFF nuevo: no enruta, no
> traduce ni conoce el dominio. Lo único que toca de la API es una variable,
> `FORWARDED_ALLOW_IPS`, para que Uvicorn acepte las cabeceras `X-Forwarded-*` del proxy y los logs
> vean la IP real del cliente en vez de la del contenedor vecino.

Pendiente, y así queda registrado:

| Pendiente | Dónde está dicho |
|---|---|
| **Persistencia en Mongo de `expedientes` y `eventos`.** Parcial: `POST /api/facturas` ya escribe expedientes y eventos; lo que no escribe todavía el **motor** son sus decisiones, que viven solo en la traza de disco (`ejecuciones` y `excel_filas` siguen vacías). Cuando el motor escriba ahí, `GET /api/facturas` debería preferir Mongo y usar la traza como respaldo. | `TRASPASO.md` §1 («Persistencia Mongo (`expedientes`…) — **a hacer**»), `maisa/api/README.md` §7 y §3.8 |
| **Autenticación real.** `API_KEY` es una clave compartida. La API está publicada en Internet y **escribe** (`POST /api/facturas`), así que hoy cualquiera puede subir PDFs: hace falta clave y/o cerrar la regla de entrada. Falta usuario/rol — y el usuario que usa la API hoy (`albertitos_app`) tiene `readWrite`, que es lo que necesita para escribir expedientes. | `maisa/api/README.md` §2.4, §3.8 y §7 |
| **TLS** — ~~la API ya sale a Internet sin cifrar~~. **Resuelto** (2026-09-19, posterior al ADR): un Caddy delante termina TLS con certificado de Let's Encrypt y la vía pública es `https://82.70.78.22.sslip.io`. No cambia esta decisión (la API sigue siendo el único servicio publicado); solo añade el terminador. | `maisa/proxy/README.md`, `maisa/api/README.md` §2.5 |
| **Decidir si el `8866` del OCR se deja publicado** en el host, ahora que el frontend entra por `/api/ocr` (el NSG ya lo bloquea desde fuera). | §2 de este ADR |
| **El visor completo** (`maisa/ui/`): el montaje está hecho y probado, pero el frontend es un trabajo en curso. | `maisa/api/README.md` §7 |

---

## 6. Referencias

- `maisa/api/README.md` — endpoints, variables de entorno y cómo arrancarlo.
- `maisa/api/docker-compose.yml` — la superficie publicada, la red externa y de dónde salen
  las credenciales de Mongo (`env_file: ../.env`).
- `maisa/api/.env.example` — las variables de la API y su valor por defecto (sin secretos).
- `maisa/docs/arranque_servicios.md` — runbook de arranque y problemas conocidos.
- `maisa/proxy/README.md` — el terminador TLS (Caddy + Let's Encrypt) que se añadió después.
- `maisa/diseño_conceptual.md` D-11, D-12 y RNF-10.
- `maisa/diseño_logico.md` §13 (Docker Compose) y §13.10 (verificación del despliegue).
- `maisa/TRASPASO.md` §1 (reparto y estado de los módulos).
