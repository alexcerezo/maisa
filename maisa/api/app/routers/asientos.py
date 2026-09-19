"""Asientos y snapshots del ERP: lectura directa de Mongo.

Mongo sigue escuchando SOLO en 127.0.0.1 (o en el DNS `mongo` de la red
compartida); el frontend nunca lo toca, pasa por aqui. Si Mongo no responde, los
endpoints devuelven 503 con un mensaje claro en lugar de un 500 opaco.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path as PathParam, Query

from ..deps import Paginacion, get_mongo, paginacion, texto_busqueda
from ..errors import ApiError
from ..mongo_repo import MongoNoDisponible, MongoRepo

router = APIRouter(tags=["erp"])

PATRON_ASIENTO = r"^[A-Za-z0-9_-]{1,32}$"


def _503(exc: MongoNoDisponible) -> ApiError:
    return ApiError(
        503,
        "mongo_no_disponible",
        "La base de datos no esta disponible en este momento.",
        {"detalle": str(exc)},
    )


@router.get("/asientos", summary="Listado paginado de asientos del ERP")
async def listar_asientos(
    vigente: bool | None = Query(None, description="true = solo el snapshot vigente"),
    q: str | None = Depends(texto_busqueda),
    pagina: Paginacion = Depends(paginacion),
    mongo: MongoRepo = Depends(get_mongo),
) -> dict:
    try:
        items, total = await mongo.listar_asientos(
            vigente=vigente, q=q, limit=pagina.limit, offset=pagina.offset
        )
    except MongoNoDisponible as exc:
        raise _503(exc) from None
    return {
        "total": total,
        "limit": pagina.limit,
        "offset": pagina.offset,
        "devueltas": len(items),
        "items": items,
    }


@router.get("/asientos/{asiento_id}", summary="Detalle de un asiento")
async def detalle_asiento(
    asiento_id: str = PathParam(..., pattern=PATRON_ASIENTO),
    mongo: MongoRepo = Depends(get_mongo),
) -> dict:
    try:
        asiento = await mongo.obtener_asiento(asiento_id)
    except MongoNoDisponible as exc:
        raise _503(exc) from None
    if asiento is None:
        raise ApiError(404, "asiento_no_encontrado", f"No hay ningun asiento '{asiento_id}'.")
    return asiento


@router.get("/snapshots", summary="Descargas del ERP registradas, mas recientes primero")
async def listar_snapshots(
    pagina: Paginacion = Depends(paginacion),
    mongo: MongoRepo = Depends(get_mongo),
) -> dict:
    try:
        items, total = await mongo.listar_snapshots(limit=pagina.limit, offset=pagina.offset)
    except MongoNoDisponible as exc:
        raise _503(exc) from None
    return {
        "total": total,
        "limit": pagina.limit,
        "offset": pagina.offset,
        "devueltas": len(items),
        "items": items,
    }
