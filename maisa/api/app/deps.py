"""Dependencias de FastAPI: estado de la app, autenticacion y paginacion.

Todo lo que necesitan los routers se resuelve por inyeccion para que los tests
puedan sustituirlo (`app.dependency_overrides`) sin Mongo ni OCR reales.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from fastapi import Depends, Header, Query, Request

from .almacen import AlmacenFacturas
from .config import Settings
from .errors import ApiError
from .mongo_repo import MongoRepo
from .ocr_client import OcrClient
from .traza import EntregaStore, TrazaStore


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_traza(request: Request) -> TrazaStore:
    return request.app.state.traza


def get_entrega(request: Request) -> EntregaStore:
    return request.app.state.entrega


def get_mongo(request: Request) -> MongoRepo:
    return request.app.state.mongo


def get_almacen(request: Request) -> AlmacenFacturas:
    return request.app.state.almacen


def get_ocr(request: Request) -> OcrClient:
    return request.app.state.ocr


def require_api_key(
    x_api_key: str | None = Header(
        default=None,
        alias="X-API-Key",
        description="Obligatoria solo si la API se arranca con API_KEY definida.",
    ),
    settings: Settings = Depends(get_settings),
) -> None:
    """Exige `X-API-Key` cuando `API_KEY` esta definida.

    Sin `API_KEY` la API queda en modo abierto (se avisa al arrancar y en
    /api/meta). La comparacion es en tiempo constante para no filtrar la clave
    caracter a caracter.
    """
    if settings.api_key is None:
        return
    if not x_api_key or not secrets.compare_digest(x_api_key, settings.api_key):
        raise ApiError(
            401,
            "no_autorizado",
            "Falta la cabecera X-API-Key o no es valida.",
        )


@dataclass(frozen=True)
class Paginacion:
    limit: int
    offset: int


def paginacion(
    limit: int = Query(50, ge=1, description="Tamano de pagina (se recorta a MAX_LIMIT)."),
    offset: int = Query(0, ge=0, description="Desplazamiento desde el inicio."),
    settings: Settings = Depends(get_settings),
) -> Paginacion:
    """Normaliza la paginacion y aplica el tope duro de `MAX_LIMIT`.

    Un `limit` mayor que el tope no es un error del cliente: se recorta y se
    devuelve el valor realmente aplicado en la respuesta.
    """
    return Paginacion(limit=min(limit, settings.max_limit), offset=offset)


def texto_busqueda(
    q: str | None = Query(None, max_length=64, description="Busqueda de texto libre."),
    settings: Settings = Depends(get_settings),
) -> str | None:
    """Valida la longitud de la busqueda antes de compilar el patron."""
    if q is None:
        return None
    limpio = q.strip()
    if not limpio:
        return None
    if len(limpio) > settings.max_query_len:
        raise ApiError(
            400,
            "busqueda_demasiado_larga",
            f"El texto de busqueda supera los {settings.max_query_len} caracteres.",
        )
    return limpio
