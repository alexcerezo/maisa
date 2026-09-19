"""Errores JSON homogeneos.

Contrato de error de la API (siempre el mismo sobre, sin trazas internas ni
cadenas de conexion):

    {"error": {"codigo": "no_encontrado", "mensaje": "...", "detalle": {...}}}
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger("albertitos-api")


class ApiError(Exception):
    """Error de negocio con codigo estable y mensaje en espanol."""

    def __init__(
        self,
        status: int,
        codigo: str,
        mensaje: str,
        detalle: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(mensaje)
        self.status = status
        self.codigo = codigo
        self.mensaje = mensaje
        self.detalle = detalle


def cuerpo_error(codigo: str, mensaje: str, detalle: dict[str, Any] | None = None) -> dict:
    error: dict[str, Any] = {"codigo": codigo, "mensaje": mensaje}
    if detalle:
        error["detalle"] = detalle
    return {"error": error}


def instalar_manejadores(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content=cuerpo_error(exc.codigo, exc.mensaje, exc.detalle),
        )

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
        detalle = exc.detail if isinstance(exc.detail, dict) else None
        mensaje = exc.detail if isinstance(exc.detail, str) else "Error en la peticion."
        return JSONResponse(
            status_code=exc.status_code,
            content=cuerpo_error(_codigo_por_status(exc.status_code), mensaje, detalle),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validacion(_: Request, exc: RequestValidationError) -> JSONResponse:
        campos = [
            {"campo": ".".join(str(p) for p in err.get("loc", [])), "problema": err.get("msg", "")}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=cuerpo_error("peticion_invalida", "Parametros de la peticion no validos.", {"campos": campos}),
        )

    @app.exception_handler(Exception)
    async def _inesperado(request: Request, exc: Exception) -> JSONResponse:
        # Se registra en el servidor con su traza; al cliente solo va el generico.
        logger.exception("Error no controlado en %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content=cuerpo_error("error_interno", "Error interno del servidor."),
        )


def _codigo_por_status(status: int) -> str:
    return {
        400: "peticion_invalida",
        401: "no_autorizado",
        403: "prohibido",
        404: "no_encontrado",
        413: "demasiado_grande",
        422: "peticion_invalida",
        429: "demasiadas_peticiones",
        502: "error_dependencia",
        503: "servicio_no_disponible",
        504: "tiempo_agotado",
    }.get(status, "error")
