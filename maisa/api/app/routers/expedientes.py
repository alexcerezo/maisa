"""Expedientes persistidos: lo que la API ha guardado en Mongo.

Dos vistas de la misma factura, con fuentes de verdad distintas:

  * `/api/facturas`  -> la **traza del motor** en disco (`OUTPUTS_DIR`). Es la
    vista de lo que ya se decidio, y existe desde antes de que hubiera nada en
    Mongo.
  * `/api/expedientes` -> la coleccion **`expedientes`** de Mongo: el agregado
    que crea `POST /api/facturas` al subir un PDF y que el motor ira
    completando (estado, evidencia, decision).

Aqui solo se lee. Escribir es cosa de `POST /api/facturas` (`almacen.py`).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path as PathParam, Query

from ..almacen import ESTADOS_SIN_DECISION, LOTES, PATRON_FILE_ID, AlmacenFacturas
from ..deps import Paginacion, get_almacen, paginacion
from ..errors import ApiError
from ..mongo_repo import MongoNoDisponible

router = APIRouter(prefix="/expedientes", tags=["expedientes"])


def _503(exc: Exception) -> ApiError:
    return ApiError(
        503,
        "mongo_no_disponible",
        "Mongo no esta disponible: no se pueden leer los expedientes.",
        {"detalle": str(exc)},
    )


@router.get("", summary="Listado paginado de expedientes guardados en Mongo")
async def listar(
    lote_id: str | None = Query(None, description="lote1 o lote2"),
    estado: str | None = Query(None, description="Estado del proceso (PENDIENTE, OCR, COMPLETADA...)"),
    pagina: Paginacion = Depends(paginacion),
    almacen: AlmacenFacturas = Depends(get_almacen),
) -> dict:
    if lote_id is not None and lote_id not in LOTES:
        raise ApiError(400, "lote_invalido", f"`lote_id` debe ser uno de: {', '.join(LOTES)}.")
    if estado is not None and estado not in ESTADOS_SIN_DECISION + ("COMPLETADA",):
        raise ApiError(
            400,
            "estado_invalido",
            f"`estado` debe ser uno de: {', '.join(ESTADOS_SIN_DECISION + ('COMPLETADA',))}.",
        )
    try:
        items, total = await almacen.listar(
            lote_id=lote_id, estado=estado, limit=pagina.limit, offset=pagina.offset
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


@router.get("/{file_id}", summary="Expediente completo guardado en Mongo")
async def detalle(
    file_id: str = PathParam(..., pattern=PATRON_FILE_ID.pattern),
    almacen: AlmacenFacturas = Depends(get_almacen),
) -> dict:
    try:
        expediente = await almacen.obtener(file_id)
    except MongoNoDisponible as exc:
        raise _503(exc) from None
    if expediente is None:
        raise ApiError(
            404,
            "expediente_no_encontrado",
            f"No hay ningun expediente guardado con file_id '{file_id}'.",
            {"nota": "Los expedientes los crea POST /api/facturas; la traza del motor se lee en /api/facturas."},
        )
    return expediente
