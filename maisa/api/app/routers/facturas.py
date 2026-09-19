"""Facturas: listado, detalle y PDF.

Fuente de datos: la traza del motor en disco (`OUTPUTS_DIR/outcomes_traza.jsonl`)
y los PDFs originales (`FACTURAS_DIR`). Mongo todavia no guarda `expedientes`,
asi que la traza es la unica fuente de verdad de las decisiones.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, Path as PathParam, Query
from fastapi.responses import FileResponse

from ..config import Settings
from ..deps import Paginacion, get_settings, get_traza, paginacion, texto_busqueda
from ..errors import ApiError
from ..traza import RESULTADOS, TrazaStore, detallar

router = APIRouter(prefix="/facturas", tags=["facturas"])

# Un file_id legitimo es "<fecha>_<proveedor>.pdf". El patron corta cualquier
# intento de salir de FACTURAS_DIR (barras, "..", rutas absolutas).
PATRON_FILE_ID = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,180}$"


def _traza_o_503(traza: TrazaStore, settings: Settings) -> None:
    if not traza.disponible:
        raise ApiError(
            503,
            "traza_no_disponible",
            "La traza del motor no esta disponible o esta vacia.",
            {"fichero": str(settings.traza_path), "outputs_dir": str(settings.outputs_dir)},
        )


def resolver_pdf(facturas_dir: Path, file_id: str) -> Path | None:
    """Ruta absoluta del PDF, o None si el nombre no es seguro o no existe.

    Doble comprobacion: el nombre no puede contener separadores y la ruta
    resuelta tiene que seguir colgando de FACTURAS_DIR.
    """
    if not file_id or "/" in file_id or "\\" in file_id or file_id in {".", ".."}:
        return None
    base = facturas_dir.resolve()
    candidato = (base / file_id).resolve()
    if base != candidato.parent:
        return None
    return candidato if candidato.is_file() else None


@router.get("", summary="Listado paginado de facturas con su decision")
async def listar(
    resultado: str | None = Query(None, description="PAGAR, NO_PAGAR o ESCALAR"),
    lote: int | None = Query(None, ge=0, description="Numero de lote del motor"),
    proveedor: str | None = Query(None, max_length=64, description="Filtro por proveedor (contiene)"),
    q: str | None = Depends(texto_busqueda),
    pagina: Paginacion = Depends(paginacion),
    traza: TrazaStore = Depends(get_traza),
    settings: Settings = Depends(get_settings),
) -> dict:
    _traza_o_503(traza, settings)
    if resultado is not None and resultado not in RESULTADOS:
        raise ApiError(
            400,
            "resultado_invalido",
            f"`resultado` debe ser uno de: {', '.join(RESULTADOS)}.",
        )
    filas, total = traza.buscar(
        resultado=resultado,
        lote=lote,
        proveedor=proveedor,
        q=q,
        limit=pagina.limit,
        offset=pagina.offset,
        max_query_len=settings.max_query_len,
    )
    return {
        "total": total,
        "limit": pagina.limit,
        "offset": pagina.offset,
        "devueltas": len(filas),
        "items": filas,
    }


@router.get("/{file_id}", summary="Detalle completo de una factura")
async def detalle(
    file_id: str = PathParam(..., pattern=PATRON_FILE_ID),
    traza: TrazaStore = Depends(get_traza),
    settings: Settings = Depends(get_settings),
) -> dict:
    _traza_o_503(traza, settings)
    registro = traza.obtener(file_id)
    if registro is None:
        raise ApiError(404, "factura_no_encontrada", f"No hay ninguna factura con file_id '{file_id}'.")
    return detallar(registro)


@router.get("/{file_id}/pdf", summary="PDF original, en modo inline")
async def pdf(
    file_id: str = PathParam(..., pattern=PATRON_FILE_ID),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    ruta = resolver_pdf(settings.facturas_dir, file_id)
    if ruta is None:
        raise ApiError(
            404,
            "pdf_no_encontrado",
            f"No se encontro el PDF '{file_id}'.",
            {"facturas_dir": str(settings.facturas_dir)},
        )
    media_type = mimetypes.guess_type(ruta.name)[0] or "application/pdf"
    return FileResponse(
        path=ruta,
        media_type=media_type,
        filename=ruta.name,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=300"},
    )
