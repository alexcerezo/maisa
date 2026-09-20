"""Punto de entrada de la API/BFF de Albertitos.

Mapa de la aplicacion:

    /health, /health/ready   -> diagnostico (sin API key, siempre JSON)
    /api/*                   -> datos (con API key si API_KEY esta definida)
    /docs, /openapi.json     -> documentacion interactiva
    / y rutas del panel      -> visor estatico si `UI_DIR/index.html` existe

La API es la **unica** superficie de datos del sistema y se consume de dos
maneras: por Internet (la IP publica de la instancia) o desde otro contenedor de
la red compartida (`http://albertitos-api:8000`). La LAN de la maquina
anfitriona no es una via de consumo.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Scope

from .almacen import AlmacenFacturas
from .config import API_VERSION, Settings
from .deps import require_api_key
from .errors import instalar_manejadores
from .mongo_repo import MongoRepo
from .ocr_client import OcrClient
from .routers import asientos, estadisticas, expedientes, facturas, health, meta, ocr
from .anclajes import CacheGeo
from .traza import EntregaStore, TrazaStore

logger = logging.getLogger("albertitos-api")


def configurar_logging(nivel: str) -> None:
    logging.basicConfig(
        level=nivel.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configurar_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.traza = TrazaStore(settings.traza_paths, settings.cola_path)
        app.state.entrega = EntregaStore(settings.outcomes_path)
        app.state.mongo = MongoRepo(
            settings.mongo_uri,
            settings.mongo_db,
            timeout_ms=settings.mongo_timeout_ms,
            max_query_len=settings.max_query_len,
        )
        app.state.ocr = OcrClient(settings.ocr_url)
        app.state.almacen = AlmacenFacturas(app.state.mongo)
        app.state.geo = CacheGeo(settings.ocr_cache_dir)

        app.state.traza.cargar()
        if not app.state.traza.disponible:
            logger.warning(
                "Traza no disponible en %s: los endpoints de facturas devolveran 503.",
                ", ".join(str(ruta) for ruta in settings.traza_paths),
            )
        if not app.state.geo.disponible:
            # No es un fallo de arranque: la API sirve igual, pero las 29
            # escaneadas se quedan sin resaltado. Se avisa aqui para que no se
            # confunda con un fallo del visor.
            logger.warning(
                "Cache de OCR no disponible en %s: las facturas escaneadas no podran "
                "resaltar sus datos (revisa el volumen OCR_CACHE_DIR).",
                settings.ocr_cache_dir,
            )
        if settings.api_key is None:
            logger.warning(
                "API_KEY no definida: la API arranca en MODO ABIERTO (sin autenticacion). "
                "Con el puerto publicado a Internet eso deja leer Y ESCRIBIR facturas a cualquiera: "
                "define API_KEY antes de exponerla."
            )
        if not settings.subidas_habilitadas:
            logger.warning("SUBIDAS_HABILITADAS=0: POST /api/facturas devolvera 403.")
        logger.info(
            "albertitos-api %s escuchando: mongo_db=%s ocr=%s facturas=%s subidas=%s",
            API_VERSION,
            settings.mongo_db,
            settings.ocr_url,
            ", ".join(str(cada) for cada in settings.facturas_dirs),
            "si" if settings.subidas_habilitadas else "no",
        )
        try:
            faltantes = app.state.mongo.indices_faltantes()
            if faltantes:
                logger.warning(
                    "Indices que faltan para las consultas de la API: %s. "
                    "El esquema lo gobierna docker/mongosh/02-schema-init.js; esta API no crea indices.",
                    faltantes,
                )
        except Exception as exc:
            logger.warning("No se pudieron comprobar los indices al arrancar: %s", exc)

        try:
            yield
        finally:
            await app.state.ocr.cerrar()
            app.state.mongo.cerrar()

    app = FastAPI(
        title="Albertitos API",
        description=(
            "Middleware/BFF del motor de decision de pago de facturas. "
            "Unica superficie expuesta: sirve decisiones (traza del motor), catalogo del ERP "
            "(Mongo en solo lectura), proxy del OCR y el visor estatico en el mismo origen."
        ),
        version=API_VERSION,
        lifespan=lifespan,
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=settings.cors_allow_credentials,
            # `PUT` y `DELETE` no son de adorno: son los metodos de
            # `/api/facturas/{file_id}/correcciones`, que es lo unico que escribe
            # del panel. Sin ellos aqui, el navegador manda el preflight, el
            # middleware lo rechaza con un 400 y el formulario de correcciones
            # falla **antes** de llegar al endpoint: el error que se ve es de
            # CORS, no de la API, y apunta a otro sitio. `curl` no lo detecta
            # porque no hace preflight.
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["X-API-Key", "Content-Type"],
            expose_headers=["X-Tiempo-ms"],
        )
        if settings.cors_abierto:
            logger.warning("CORS_ORIGINS='*': CORS abierto y credenciales desactivadas.")

    instalar_manejadores(app)

    # La salud NO lleva API key: la necesitan el healthcheck del contenedor y el
    # panel de diagnostico antes de tener credenciales.
    app.include_router(health.router)

    api = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])
    api.include_router(facturas.router)
    api.include_router(expedientes.router)
    api.include_router(asientos.router)
    api.include_router(estadisticas.router)
    api.include_router(ocr.router)
    api.include_router(meta.router)
    app.include_router(api)

    _montar_ui(app, settings)

    return app


# Prefijos que son de la API y no del visor. Si una ruta de aqui no existe, el
# 404 tiene que seguir siendo JSON: `/api/nope` no es una pagina del panel, y
# devolver `index.html` haria que un error de la API pareciera un exito (el
# panel pinta `ErrorPeticion` cuando el cuerpo es JSON, no cuando es HTML).
PREFIJOS_API = ("api", "docs", "health", "openapi.json", "redoc")


class VisorSPA(StaticFiles):
    """`StaticFiles` que cae a `index.html` cuando la ruta no es un fichero.

    El visor es una SPA: `/facturas/2026-01-08_P001.pdf` o `/escalabilidad` no
    existen en disco y los resuelve react-router **en el navegador**. Con un
    `StaticFiles` a secas, recargar (F5) o abrir un enlace directo a cualquiera
    de esas rutas devolvia el 404 JSON de Starlette (`{"detail":"Not Found"}`),
    aunque el visor cargase bien en `/` y sus enlaces internos (que son de
    cliente) navegaran sin problema. Es exactamente el `rewrite` que Vercel
    aplica en el despliegue estatico (ver `ui/README.md` §5), y por la misma
    razon los cargadores del panel comprueban el `content-type` con `esJson()`
    antes de parsear: una ruta desconocida responde `index.html`, no un 404.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            # Solo se intercepta el "no existe ese fichero" y solo fuera de la
            # superficie de la API. Un 405 (metodo no permitido) o un 401 se
            # propagan tal cual.
            if exc.status_code != 404 or not _es_ruta_del_visor(path):
                raise
            # Sin build no hay `index.html`: el `super()` vuelve a lanzar el 404
            # y el cliente recibe el mismo "no encontrado" de antes.
            return await super().get_response("index.html", scope)


def _es_ruta_del_visor(path: str) -> bool:
    """`path` llega normalizado y relativo al montaje (`api/nope`, `facturas`)."""
    return path.split("/", 1)[0] not in PREFIJOS_API


def _montar_ui(app: FastAPI, settings: Settings) -> None:
    """Sirve el visor en `/` para que no haga falta CORS.

    `UI_DIR` apunta al **build** del panel (`maisa/ui/dist`), no a su codigo
    fuente: la plantilla `maisa/ui/index.html` tambien existe sin construir y
    carga `/src/main.tsx`, que un navegador no ejecuta. Sirviendo esa, el
    resultado seria una pagina en blanco en vez del aviso de que no hay visor.

    La decision se toma **en cada peticion**, no al arrancar: asi el build se
    puede reemplazar con el contenedor ya levantado. Si no hay `index.html` se
    devuelve un mensaje informativo en lugar de un 404.

    El montaje es `VisorSPA` y no `StaticFiles` a secas para que las rutas del
    panel que no son ficheros (`/facturas`, `/escalabilidad`, ...) tambien
    devuelvan el `index.html`: sin eso, un enlace directo o un F5 sobre
    cualquier ruta de react-router daba 404.
    """
    ui_dir = settings.ui_dir

    # Se registra antes del mount para que "/" gane al StaticFiles; el mount
    # queda como red de seguridad para el resto de ficheros del visor.
    @app.get("/", include_in_schema=False)
    async def raiz() -> Response:
        indice = ui_dir / "index.html"
        if indice.is_file():
            return FileResponse(indice)
        return JSONResponse(
            content={
                "servicio": "albertitos-api",
                "api_version": API_VERSION,
                "mensaje": (
                    "No hay visor estatico en UI_DIR: construye el panel con "
                    "`npm run build` en maisa/ui (deja el resultado en ui/dist, que es lo que "
                    "apunta UI_DIR) y se servira automaticamente en /."
                ),
                "ui_dir": str(ui_dir),
                "docs": "/docs",
                "health": "/health",
                "api": "/api/meta",
            }
        )

    if ui_dir.is_dir():
        app.mount("/", VisorSPA(directory=ui_dir, html=True), name="ui")
        logger.info("Visor estatico servido desde %s (sin reinicio)", ui_dir)
    else:
        logger.warning(
            "UI_DIR no existe (%s): / solo devolvera el mensaje informativo. "
            "Si falta, es que no se ha construido el panel: `npm run build` en maisa/ui.",
            ui_dir,
        )


app = create_app()
