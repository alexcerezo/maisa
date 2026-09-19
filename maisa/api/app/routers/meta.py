"""Metadatos: version de la API, versiones del motor y configuracion publica.

Nada de credenciales: la URI de Mongo sale saneada (`***@host`), nunca completa.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..config import API_VERSION, Settings
from ..deps import get_settings, get_traza
from ..traza import TrazaStore

router = APIRouter(tags=["meta"])


@router.get("/meta", summary="Version, versiones del motor y configuracion no sensible")
async def meta(
    settings: Settings = Depends(get_settings),
    traza: TrazaStore = Depends(get_traza),
) -> dict:
    return {
        "api_version": API_VERSION,
        "servicio": "albertitos-api",
        "modo_abierto": settings.api_key is None,
        "motor": {
            "versiones_norma": traza.versiones_norma(),
            "facturas_en_traza": traza.total(),
            "lineas_invalidas": traza.lineas_invalidas(),
        },
        "configuracion": settings.publico(),
    }
