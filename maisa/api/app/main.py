"""Punto de entrada de la API/BFF de Albertitos.

Mapa de la aplicacion:

    /health, /health/ready   -> diagnostico (sin API key, siempre JSON)
    /api/*                   -> datos (con API key si API_KEY esta definida)
    /docs, /openapi.json     -> documentacion interactiva
    /                        -> visor estatico si `UI_DIR/index.html` existe

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

from .almacen import AlmacenFacturas
from .config import API_VERSION, Settings
from .deps import require_api_key
from .errors import instalar_manejadores
from .mongo_repo import MongoRepo
from .ocr_client import OcrClient
from .routers import asientos, estadisticas, expedientes, facturas, health, meta, ocr
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
        app.state.traza = TrazaStore(settings.traza_path)
        app.state.entrega = EntregaStore(settings.outcomes_path)
        app.state.mongo = MongoRepo(
            settings.mongo_uri,
            settings.mongo_db,
            timeout_ms=settings.mongo_timeout_ms,
            max_query_len=settings.max_query_len,
        )
        app.state.ocr = OcrClient(settings.ocr_url)
        app.state.almacen = AlmacenFacturas(app.state.mongo)

        app.state.traza.cargar()
        if not app.state.traza.disponible:
            logger.warning(
                "Traza no disponible en %s: los endpoints de facturas devolveran 503.",
                settings.traza_path,
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
            settings.facturas_dir,
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
            allow_methods=["GET", "POST", "OPTIONS"],
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


def _montar_ui(app: FastAPI, settings: Settings) -> None:
    """Sirve el visor en `/` para que no haga falta CORS.

    La decision se toma **en cada peticion**, no al arrancar: asi el frontend se
    puede dejar en `UI_DIR` con el contenedor ya levantado. Si no hay
    `index.html` se devuelve un mensaje informativo en lugar de un 404.
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
                    "No hay visor estatico en UI_DIR: coloca el frontend en esa carpeta y se "
                    "servira automaticamente en /."
                ),
                "ui_dir": str(ui_dir),
                "docs": "/docs",
                "health": "/health",
                "api": "/api/meta",
            }
        )

    if ui_dir.is_dir():
        app.mount("/", StaticFiles(directory=ui_dir, html=True), name="ui")
        logger.info("Visor estatico servido desde %s (sin reinicio)", ui_dir)
    else:
        logger.warning("UI_DIR no existe (%s): / solo devolvera el mensaje informativo", ui_dir)


app = create_app()
