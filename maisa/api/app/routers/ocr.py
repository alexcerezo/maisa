"""Proxy del OCR.

`POST /api/ocr?engine=auto|cloud|local&detalle=true|false` con el fichero en
multipart (`file`). El visor sube aqui la factura y recibe el texto ya extraido,
sin saber que detras hay un RapidOCR ni en que contenedor vive.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, UploadFile

from ..config import Settings
from ..deps import get_ocr, get_settings
from ..errors import ApiError
from ..ocr_client import OcrClient, leer_con_tope

router = APIRouter(tags=["ocr"])

MOTORES = ("auto", "cloud", "local")


@router.post("/ocr", summary="Ejecuta el OCR sobre un PDF o una imagen")
async def ejecutar_ocr(
    file: UploadFile = File(..., description="PDF o imagen (png/jpg/webp)"),
    engine: str | None = Query(None, description="auto | cloud | local"),
    detalle: bool = Query(
        False,
        description="false (por defecto) = solo texto (/ocr/text); true = payload completo con cajas (/ocr).",
    ),
    settings: Settings = Depends(get_settings),
    ocr: OcrClient = Depends(get_ocr),
) -> dict:
    if engine is not None and engine not in MOTORES:
        raise ApiError(400, "engine_invalido", f"`engine` debe ser uno de: {', '.join(MOTORES)}.")

    max_bytes = int(settings.max_upload_mb * 1024 * 1024)
    contenido = await leer_con_tope(file, max_bytes)

    resultado = await ocr.procesar(
        nombre=file.filename or "documento",
        contenido=contenido,
        content_type=file.content_type,
        engine=engine,
        detalle=detalle,
    )
    if not detalle:
        # En modo resumen no tiene sentido devolver el bruto entero.
        resultado.pop("bruto", None)
    return resultado
