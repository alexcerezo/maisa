"""Estadisticas del panel: recuentos de decision y estado del ERP.

Los recuentos por resultado y por lote salen de la traza (memoria, sin Mongo).
El numero de asientos vigentes sale de Mongo y, si Mongo no responde, el campo
viene a `null` con el motivo: el panel se dibuja igual.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import get_entrega, get_mongo, get_traza
from ..errors import ApiError
from ..mongo_repo import MongoNoDisponible, MongoRepo
from ..traza import EntregaStore, TrazaStore

router = APIRouter(tags=["estadisticas"])


@router.get("/estadisticas", summary="Recuento por resultado, lote y estado del ERP")
async def estadisticas(
    traza: TrazaStore = Depends(get_traza),
    entrega: EntregaStore = Depends(get_entrega),
    mongo: MongoRepo = Depends(get_mongo),
) -> dict:
    if not traza.disponible:
        raise ApiError(
            503,
            "traza_no_disponible",
            "La traza del motor no esta disponible o esta vacia.",
        )

    datos = traza.estadisticas()

    asientos_vigentes: int | None = None
    mongo_error: str | None = None
    try:
        asientos_vigentes = await mongo.contar_asientos_vigentes()
    except MongoNoDisponible as exc:
        mongo_error = str(exc)

    resultados_entrega = entrega.resultados()
    cobertura = traza.cobertura_entrega(set(resultados_entrega))

    pendientes_revision: int | None = None
    if mongo_error is None:
        escalados = traza.file_ids_por_resultado("ESCALAR")
        try:
            revisiones = await mongo.listar_revisiones(escalados)
            pendientes_revision = sum(
                1 for file_id in escalados if revisiones.get(file_id, {}).get("estado") != "RESUELTA"
            )
        except MongoNoDisponible as exc:
            mongo_error = str(exc)

    return {
        **datos,
        "asientos_vigentes": asientos_vigentes,
        "pendientes_revision": pendientes_revision,
        "mongo": {"ok": mongo_error is None, "error": mongo_error},
        "entrega": {
            "total": entrega.total(),
            "lineas_invalidas": entrega.lineas_invalidas(),
            "coincide_con_traza": cobertura["coincide"],
            "lotes": cobertura["lotes"],
            "faltan_en_traza": cobertura["faltan_en_traza"],
        },
    }
