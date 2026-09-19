"""Salud del servicio y de sus dependencias.

Dos endpoints con propositos distintos:

  * `GET /health`  -> diagnostico. **Siempre 200** mientras el proceso viva, con
    el detalle de cada dependencia (ok/error + latencia). Es lo que consulta el
    healthcheck del contenedor y lo que se enseña en el visor.
  * `GET /health/ready` -> listo para servir. 503 si alguna dependencia critica
    (`CRITICAL_DEPS`, por defecto `mongo,ocr`) no responde.

El campo `estado` vale `ok` (todo responde), `degradado` (falta algo, pero queda
algo critico en pie) o `error` (no responde ninguna dependencia critica).

Ninguna comprobacion puede tumbar el endpoint: todo va con timeout corto y
captura de errores.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ..almacen import AlmacenFacturas
from ..config import Settings
from ..deps import get_almacen, get_mongo, get_ocr, get_settings, get_traza
from ..mongo_repo import MongoRepo
from ..ocr_client import OcrClient
from ..traza import TrazaStore

router = APIRouter(tags=["salud"])


async def _con_tiempo(nombre: str, comprobacion: Callable[[], Awaitable[dict[str, Any]]], timeout: float) -> dict:
    """Ejecuta una comprobacion midiendo latencia y sin dejar escapar errores."""
    inicio = time.perf_counter()
    try:
        extra = await asyncio.wait_for(comprobacion(), timeout=timeout)
        resultado: dict[str, Any] = {"ok": True}
    except asyncio.TimeoutError:
        extra = {"error": f"tiempo agotado ({timeout}s)"}
        resultado = {"ok": False}
    except Exception as exc:  # cualquier fallo de la dependencia es un dato, no una excepcion
        extra = {"error": str(exc) or exc.__class__.__name__}
        resultado = {"ok": False}
    resultado["nombre"] = nombre
    resultado["latencia_ms"] = round((time.perf_counter() - inicio) * 1000, 2)
    resultado.update(extra)
    return resultado


async def _comprobar_mongo(mongo: MongoRepo, incluir_indices: bool) -> dict[str, Any]:
    await mongo.ping_async()
    detalle: dict[str, Any] = {"db": mongo.db_nombre}
    if incluir_indices:
        try:
            faltantes = mongo.indices_faltantes()
        except Exception:
            faltantes = {}
        detalle["indices_faltantes"] = faltantes
    return detalle


async def _comprobar_ocr(ocr: OcrClient, timeout: float) -> dict[str, Any]:
    payload = await ocr.salud(timeout_s=timeout)
    return {
        "url": ocr.base_url,
        "estado_ocr": payload.get("status"),
        "motor": payload.get("engine"),
        "motores": payload.get("engines"),
    }


async def _comprobar_escritura(almacen: AlmacenFacturas) -> dict[str, Any]:
    return await almacen.estado()


async def _estado(
    settings: Settings,
    mongo: MongoRepo,
    ocr: OcrClient,
    traza: TrazaStore,
    almacen: AlmacenFacturas,
    incluir_indices: bool,
) -> dict:
    timeout = settings.health_timeout_s
    dependencias = {
        "mongo": await _con_tiempo("mongo", lambda: _comprobar_mongo(mongo, incluir_indices), timeout),
        "ocr": await _con_tiempo("ocr", lambda: _comprobar_ocr(ocr, timeout), timeout),
        "escritura": await _con_tiempo("escritura", lambda: _comprobar_escritura(almacen), timeout),
    }
    caidas = [nombre for nombre, dato in dependencias.items() if not dato["ok"]]
    criticas = [nombre for nombre in settings.critical_deps if nombre in dependencias]
    criticas_caidas = [nombre for nombre in caidas if nombre in criticas]

    if not caidas:
        estado = "ok"
    elif criticas and len(criticas_caidas) == len(criticas):
        # Ninguna dependencia critica responde: la API esta viva pero no puede
        # servir datos. `/health` sigue devolviendo 200 con el detalle; quien
        # decide el 503 es `/health/ready`.
        estado = "error"
    else:
        estado = "degradado"

    return {
        "estado": estado,
        "servicio": "albertitos-api",
        "dependencias": dependencias,
        "dependencias_criticas": criticas,
        "criticas_caidas": criticas_caidas,
        "datos": {
            "traza": {
                "ok": traza.disponible,
                "facturas": traza.total(),
                "lineas_invalidas": traza.lineas_invalidas(),
            }
        },
    }


@router.get("/health", summary="Estado del servicio y de sus dependencias")
async def health(
    settings: Settings = Depends(get_settings),
    mongo: MongoRepo = Depends(get_mongo),
    ocr: OcrClient = Depends(get_ocr),
    traza: TrazaStore = Depends(get_traza),
    almacen: AlmacenFacturas = Depends(get_almacen),
) -> dict:
    return await _estado(settings, mongo, ocr, traza, almacen, incluir_indices=True)


@router.get("/health/ready", summary="Listo para servir (503 si falla algo critico)")
async def ready(
    settings: Settings = Depends(get_settings),
    mongo: MongoRepo = Depends(get_mongo),
    ocr: OcrClient = Depends(get_ocr),
    traza: TrazaStore = Depends(get_traza),
    almacen: AlmacenFacturas = Depends(get_almacen),
) -> JSONResponse:
    estado = await _estado(settings, mongo, ocr, traza, almacen, incluir_indices=False)
    listo = not estado["criticas_caidas"]
    return JSONResponse(
        status_code=200 if listo else 503,
        content={
            "listo": listo,
            "estado": estado["estado"],
            "criticas_caidas": estado["criticas_caidas"],
            "dependencias": {
                nombre: {"ok": dato["ok"], "latencia_ms": dato["latencia_ms"], "error": dato.get("error")}
                for nombre, dato in estado["dependencias"].items()
            },
        },
    )
