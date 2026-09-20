# maisa-ui — el panel de conciliación de Albertitos

Panel del proyecto Albertitos: enseña las facturas de la traza (listado, filtros, motivos,
expediente y PDF original) y es donde se marca la cola de revisión. **React 19 + Vite 8 +
TypeScript + Tailwind v4**, con `react-router-dom` v7 y los componentes de shadcn/ui.

No es un servicio: es un **build estático** (`dist/`) que se sirve desde **Vercel**. La API vive
en otro origen (`https://82.70.78.22.sslip.io`), así que aquí hay CORS de por medio.

- Cómo se consume la API y qué sorpresas tiene: `src/api/cliente.ts`.
- Qué es cada endpoint: `maisa/api/README.md` §3.
- Cómo se levanta el sistema entero (Mongo, API, OCR, TLS): `maisa/docs/arranque_servicios.md`.

---

## 1. En local

Hace falta Node `^20.19.0 || >=22.12.0` (lo que exige Vite 8) y, para ver datos en vivo, la API
arriba (`maisa/api/README.md` §5).

```bash
npm install
npm run dev          # http://localhost:5173
```

**El 5173 no es negociable.** `DEFAULT_CORS_ORIGINS` en `maisa/api/app/config.py` incluye
`http://localhost:5173` y `http://127.0.0.1:5173` (además de los del 8010, que es cuando el panel
lo sirve la propia API). En otro puerto el navegador bloquea las peticiones y el fallo parece de
la UI: la petición no sale y en la consola se ve un error de CORS, no un error del panel.
`vite.config.ts` lleva `strictPort: true` justo para eso:
si el 5173 está ocupado, Vite **falla** en vez de saltar al 5174 en silencio.

Contra qué API habla lo decide `public/config.json` (campo `api`) **en tiempo de ejecución**, no
el build: cambiar de backend no exige recompilar, solo recargar. Para apuntar a otra API sin tocar
ficheros, `?api=https://otra` en la barra de direcciones. El orden completo (consulta →
`localStorage` → `config.json`) está en `src/api/config.ts`.

## 2. Comprobar y construir

```bash
npm run typecheck    # tsc --noEmit
npm run build        # tsc --noEmit && vite build  ->  dist/
npm run preview      # sirve dist/ para mirarlo antes de desplegar
```

`npm run build` **ya** hace el typecheck, así que no hay que llamarlo antes por separado. Vercel
tampoco lo duplica: el `buildCommand` es `npm run build`.

## 3. Despliegue en Vercel

**Root Directory = `maisa/ui`.** Es el ajuste que más tiempo cuesta encontrar: `vercel.json` vive
dentro de `maisa/ui/`, y Vercel solo lee el `vercel.json` de la raíz del proyecto. Si se deja la
raíz del repo, este fichero **no se lee**: no hay rewrite de SPA y recargar en
`/facturas/<fileId>` devuelve el 404 de Vercel. La app se ve perfecta hasta que alguien recarga.

En Vercel: importar el repositorio, poner **Root Directory = `maisa/ui`** y desplegar. No hay
variables de entorno que definir: la URL de la API va en `public/config.json`, no en el bundle.

Cada línea de `vercel.json` está por un motivo:

| Clave | Valor | Por qué |
|---|---|---|
| `framework` | `vite` | Fija el preset en el fichero y no en los ajustes del panel: el despliegue no depende de que alguien marque la casilla correcta. |
| `installCommand` | `npm ci` | Hay `package-lock.json` versionado, así que se instala exactamente lo que dice el lock (reproducible) en vez de resolver versiones nuevas en cada despliegue. |
| `buildCommand` | `npm run build` | Es el script del repo (`tsc --noEmit && vite build`). No se repite el `tsc` aquí: ya está dentro. |
| `outputDirectory` | `dist` | El sitio que deja Vite por defecto. |
| `rewrites` | `/(.*)` → `/index.html` | La app usa rutas reales (`/facturas`, `/facturas/:fileId`): sin esto, recargar o compartir un enlace da 404. Ver §5. |
| `headers` `/config.json` | `Cache-Control: no-store` | Es el fichero que dice **contra qué API** habla el panel y se cambia sin recompilar. Cacheado, un despliegue que apunta a otro sitio seguiría sirviendo el viejo y no habría forma de verlo. |
| `headers` `/data/(.*).json` | `application/json; charset=utf-8` + caché larga | Los JSON del congelado: se declara el tipo del que depende `esJson` (y el `charset` importa, que hay acentos) y se cachean como lo que son, ficheros que no cambian. |
| `headers` `/data/(.*)` | caché larga `immutable` | Todo el congelado (incluidos `pdfs/`) es una traza cerrada: lo que se copió, copiado se queda. |
| `headers` `/assets/(.*)` | caché larga `immutable` | Lo que emite Vite con hash en el nombre (`index-BhwL-kTE.js`, las fuentes): un fichero con hash nunca cambia de contenido, así que un año de caché es gratis. |

No se ponen `cleanUrls` ni `trailingSlash`: la app no tiene rutas `.html` ni directorios con
`index.html`, y `trailingSlash` solo añadiría un 308 en cada ruta de react-router sin ganar nada.
El `$schema` apunta a `https://openapi.vercel.sh/vercel.json`, que es lo que valida el fichero
(además de dar autocompletado en el editor).

**CORS.** Si el panel se sirve desde un dominio de Vercel, ese origen tiene que estar en
`CORS_ORIGINS` de la API: `maisa/api/README.md` §4. Con el `*` de hoy un fallo de CORS no aparece
en ningún log del servidor, solo en la consola del navegador.

## 4. Las dos fuentes de datos

Lo decide `src/api/fuente.ts` en tiempo de ejecución, y la pantalla dice de cuál está tirando:

1. **API viva** — la de `public/config.json` (o `?api=`). La comprobación es
   `GET /api/estadisticas` con 5 s de tiempo máximo. A propósito **no** se usa `/health/ready`:
   depende del OCR y devuelve 503 cuando está caído, pero el listado se lee de fichero y funciona
   igual.
2. **Congelado** — los ficheros de `public/data/`: `facturas.json` (500 facturas),
   `estadisticas.json`, `manifiesto.json`, `facturas/*.json` (500 expedientes) y `pdfs/`. Se
   fuerza con **`?fuente=congelado`** en la barra de direcciones, y es lo que se enseña cuando la
   API no contesta.

El congelado se **copia** de la API viva, no se escribe a mano:

```bash
python3 tools/descargar_fixtures.py --base https://82.70.78.22.sslip.io
node tools/verificar_paridad.ts    # comprueba que congelado y API enseñan lo mismo
```

## 5. La trampa del rewrite (leer antes de tocar `vercel.json`)

El `rewrites` manda a `/index.html` **cualquier** ruta que no sea un fichero estático, y lo hace
con **200**. Así que un fichero que falta no da 404: da 200 con `text/html`, y `res.json()`
revienta con un `Unexpected token '<'` que no explica nada. Por eso el cliente mira el
`content-type` **antes** de convertir (`esJson` en `src/api/config.ts`, y el mismo cuidado en
`src/api/datos.ts` para el congelado y en `src/api/cliente.ts` para la API).

Dos consecuencias que no se pueden romper:

- El rewrite **se queda** y va al final: en el orden de Vercel (`cleanUrls` → `routes` →
  `redirects` → `headers` → sistema de ficheros → `rewrites`) las cabeceras se aplican antes, así
  que también viajan con lo que acaba en `index.html`. Por eso el `Content-Type` de
  `/data/*.json` no sustituye a la comprobación del cliente: si un fichero falta, el cliente lo
  detecta al parsear (`no es JSON válido`) y no convirtiendo un HTML en datos.
- `/config.json` no se cachea nunca (`no-store`), y el cliente además lo pide con
  `cache: "no-store"`. Es la pieza que decide contra qué backend habla todo lo demás.
