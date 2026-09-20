"""Facturas: listado, detalle, PDF y subida.

Dos fuentes de datos conviven aqui a proposito:

  * La **traza del motor** en disco (`OUTPUTS_DIR/outcomes_traza.jsonl`) es la
    vista de lo que ya se decidio: listado, detalle y estadisticas.
  * El **PDF original** se busca primero en `FACTURAS_DIR` (los que dejo el
    motor) y, si no esta, en GridFS (los que ha subido `POST /api/facturas`).

`POST /api/facturas` es el unico endpoint de escritura de la API: guarda el PDF
en GridFS, crea el expediente en Mongo y deja traza en `eventos`. Ver
`almacen.py`.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Header, Path as PathParam, Query, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..almacen import (
    PATRON_FILE_ID,
    AlmacenFacturas,
    DocumentoRechazado,
    FacturaDuplicada,
    NombreInvalido,
    lote_id_desde_numero,
    normalizar_file_id,
)
from ..config import Settings
from ..deps import (
    Paginacion,
    get_almacen,
    get_mongo,
    get_ocr,
    get_settings,
    get_traza,
    paginacion,
    texto_busqueda,
)
from ..errors import ApiError
from ..mongo_repo import ESTADOS_REVISION, MongoNoDisponible, MongoRepo
from ..ocr_client import OcrClient, leer_con_tope
from ..traza import RESULTADOS, TrazaStore, detallar
from .ocr import MOTORES as MOTORES_OCR

router = APIRouter(prefix="/facturas", tags=["facturas"])

# La cabecera de un PDF puede ir precedida de basura (la especificacion tolera
# hasta 1024 bytes), asi que se busca la firma en ese margen y no solo al
# principio del fichero.
FIRMA_PDF = b"%PDF-"
MARGEN_FIRMA = 1024

# Cache del listado: `private` porque va detras de la API key y `no-cache` (no
# `no-store`) para que el cliente pueda guardar el cuerpo pero tenga que
# revalidarlo con `If-None-Match` en cada peticion.
CABECERAS_CACHE = {"Cache-Control": "private, no-cache", "Vary": "Accept-Encoding"}


def etag_listado(
    firma: str,
    *,
    resultado: str | None,
    lote: int | None,
    proveedor: str | None,
    q: str | None,
    limit: int,
    offset: int,
) -> str:
    """ETag fuerte y estable del listado.

    Depende de la firma de la traza y de **todos** los parametros que cambian la
    respuesta. El sha256 se serializa con `json` (sin ambiguedad al concatenar) y
    se trunca: 128 bits sobran para detectar cambios y la cabecera queda corta.
    """
    partes = [firma, resultado, lote, proveedor, q, limit, offset]
    digest = hashlib.sha256(json.dumps(partes, ensure_ascii=False).encode("utf-8")).hexdigest()
    return f'"{digest[:32]}"'


def etag_coincide(if_none_match: str | None, etag: str) -> bool:
    """Comparacion de `If-None-Match` segun RFC 9110.

    La cabecera es una lista separada por comas; `*` casa con cualquier
    representacion y el prefijo `W/` de un ETag debil se ignora, porque la
    comparacion de cache es siempre debil (aunque el ETag que emitimos sea
    fuerte).
    """
    if not if_none_match:
        return False
    for candidato in if_none_match.split(","):
        valor = candidato.strip()
        if valor == "*":
            return True
        if valor.startswith("W/"):
            valor = valor[2:].strip()
        if valor and valor == etag:
            return True
    return False


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
    response: Response,
    resultado: str | None = Query(None, description="PAGAR, NO_PAGAR o ESCALAR"),
    lote: int | None = Query(None, ge=0, description="Numero de lote del motor"),
    proveedor: str | None = Query(None, max_length=64, description="Filtro por proveedor (contiene)"),
    q: str | None = Depends(texto_busqueda),
    pagina: Paginacion = Depends(paginacion),
    if_none_match: str | None = Header(
        None, alias="If-None-Match", description="ETag del listado que el cliente ya tiene."
    ),
    traza: TrazaStore = Depends(get_traza),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Listado paginado leido de la traza del motor, con cache por ETag.

    La traza solo cambia cuando el motor vuelve a ejecutarse, asi que el listado
    es cacheable. Cada 200 lleva un `ETag` fuerte que depende de la firma
    (sha256) de la traza y de los parametros que cambian la respuesta
    (`resultado`, `lote`, `proveedor`, `q`, `limit`, `offset`), mas
    `Cache-Control: private, no-cache` y `Vary: Accept-Encoding`: el cliente
    puede guardar el cuerpo, pero debe revalidarlo antes de usarlo.

    Si la peticion trae `If-None-Match` y casa con ese ETag (lista separada por
    comas, `*` y ETag debil `W/` incluidos) la respuesta es **304 Not Modified**,
    con el mismo `ETag` y sin cuerpo: ni se filtra ni se pagina otra vez. El
    contrato del 200 (`total`, `limit`, `offset`, `devueltas`, `items`) no
    cambia.
    """
    _traza_o_503(traza, settings)
    if resultado is not None and resultado not in RESULTADOS:
        raise ApiError(
            400,
            "resultado_invalido",
            f"`resultado` debe ser uno de: {', '.join(RESULTADOS)}.",
        )
    etag = etag_listado(
        traza.firma(),
        resultado=resultado,
        lote=lote,
        proveedor=proveedor,
        q=q,
        limit=pagina.limit,
        offset=pagina.offset,
    )
    cabeceras = {"ETag": etag, **CABECERAS_CACHE}
    if etag_coincide(if_none_match, etag):
        return Response(status_code=304, headers=cabeceras)
    response.headers.update(cabeceras)
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
    file_id: str = PathParam(..., pattern=PATRON_FILE_ID.pattern),
    traza: TrazaStore = Depends(get_traza),
    settings: Settings = Depends(get_settings),
    mongo: MongoRepo = Depends(get_mongo),
) -> dict:
    _traza_o_503(traza, settings)
    registro = traza.obtener(file_id)
    if registro is None:
        raise ApiError(404, "factura_no_encontrada", f"No hay ninguna factura con file_id '{file_id}'.")
    datos = detallar(registro)
    try:
        datos["revision"] = await mongo.obtener_revision(file_id)
    except MongoNoDisponible:
        datos["revision"] = None
    return datos


class RevisionEntrada(BaseModel):
    estado: str = Field(..., description="PENDIENTE o RESUELTA")
    revisor: str | None = Field(None, max_length=120)
    comentario: str | None = Field(None, max_length=2000)


@router.put("/{file_id}/revision", summary="Marca una factura como pendiente o resuelta de revision humana")
async def marcar_revision(
    entrada: RevisionEntrada,
    file_id: str = PathParam(..., pattern=PATRON_FILE_ID),
    traza: TrazaStore = Depends(get_traza),
    settings: Settings = Depends(get_settings),
    mongo: MongoRepo = Depends(get_mongo),
) -> dict:
    _traza_o_503(traza, settings)
    if not traza.contiene(file_id):
        raise ApiError(404, "factura_no_encontrada", f"No hay ninguna factura con file_id '{file_id}'.")
    if entrada.estado not in ESTADOS_REVISION:
        raise ApiError(
            400,
            "estado_invalido",
            f"`estado` debe ser uno de: {', '.join(ESTADOS_REVISION)}.",
        )
    try:
        return await mongo.marcar_revision(
            file_id, estado=entrada.estado, revisor=entrada.revisor, comentario=entrada.comentario
        )
    except MongoNoDisponible as exc:
        raise ApiError(
            503,
            "mongo_no_disponible",
            "No se pudo guardar la revision: Mongo no responde.",
            {"detalle": str(exc)},
        ) from None


@router.get("/{file_id}/pdf", summary="PDF original, en modo inline")
async def pdf(
    file_id: str = PathParam(..., pattern=PATRON_FILE_ID.pattern),
    settings: Settings = Depends(get_settings),
    almacen: AlmacenFacturas = Depends(get_almacen),
) -> Response:
    """El PDF del motor (disco) o, si no esta, el que se subio por la API (GridFS)."""
    ruta = resolver_pdf(settings.facturas_dir, file_id)
    if ruta is not None:
        media_type = mimetypes.guess_type(ruta.name)[0] or "application/pdf"
        return FileResponse(
            path=ruta,
            media_type=media_type,
            filename=ruta.name,
            content_disposition_type="inline",
            headers={"Cache-Control": "private, max-age=300"},
        )

    try:
        contenido = await almacen.abrir_pdf(file_id)
    except MongoNoDisponible as exc:
        raise ApiError(
            503,
            "mongo_no_disponible",
            "El PDF no esta en disco y Mongo no esta disponible para buscarlo en GridFS.",
            {"detalle": str(exc)},
        ) from None
    if contenido is None:
        raise ApiError(
            404,
            "pdf_no_encontrado",
            f"No se encontro el PDF '{file_id}'.",
            {"facturas_dir": str(settings.facturas_dir), "gridfs": "pdfs"},
        )
    return Response(
        content=contenido,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{file_id}"',
            "Cache-Control": "private, max-age=300",
        },
    )


def _parece_pdf(contenido: bytes) -> bool:
    return FIRMA_PDF in contenido[:MARGEN_FIRMA]


@router.post("", status_code=201, summary="Sube una factura nueva (PDF)")
async def subir(
    response: Response,
    file: UploadFile = File(..., description="PDF de la factura. Va en el campo `file`."),
    lote: int = Query(1, ge=1, le=2, description="Numero de lote: 1 o 2 (se guarda como lote1/lote2)."),
    ocr: bool = Query(False, description="Ejecutar el OCR y guardar la lectura extraida."),
    engine: str = Query("auto", description="Motor del OCR cuando `ocr=true`."),
    settings: Settings = Depends(get_settings),
    almacen: AlmacenFacturas = Depends(get_almacen),
    ocr_client: OcrClient = Depends(get_ocr),
) -> dict:
    """Guarda el PDF en GridFS, crea el expediente y deja traza en `eventos`.

    Es **idempotente** por contenido: volver a subir el mismo fichero con el
    mismo nombre devuelve `200` con `duplicado: true` y no reescribe nada. Si el
    nombre ya existe con **otro** contenido, devuelve `409`: renombrar es cosa
    del cliente, no de la API.

    El OCR es opcional y **best-effort**: si falla, la factura queda guardada
    igualmente en estado `PENDIENTE`, se deja un evento `OCR_FAIL` y el error
    viaja en `ocr.aviso`. Perder la lectura es mejor que perder el PDF.
    """
    if not settings.subidas_habilitadas:
        raise ApiError(
            403,
            "subidas_deshabilitadas",
            "La subida de facturas esta desactivada (SUBIDAS_HABILITADAS=0).",
        )
    if engine not in MOTORES_OCR:
        raise ApiError(400, "engine_invalido", f"`engine` debe ser uno de: {', '.join(MOTORES_OCR)}.")

    contenido = await leer_con_tope(file, settings.max_upload_bytes)
    nombre = file.filename or ""
    try:
        file_id = normalizar_file_id(nombre)
    except NombreInvalido as exc:
        raise ApiError(400, "nombre_invalido", str(exc)) from None
    if not _parece_pdf(contenido):
        raise ApiError(
            415,
            "formato_no_soportado",
            "El fichero no parece un PDF (falta la firma %PDF-).",
            {"content_type": file.content_type, "bytes": len(contenido)},
        )

    lote_id = lote_id_desde_numero(lote)
    run_id = f"api-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%f}"

    resultado_ocr: dict | None = None
    aviso_ocr: dict | None = None
    if ocr:
        try:
            resultado_ocr = await ocr_client.procesar(
                nombre=file_id,
                contenido=contenido,
                content_type=file.content_type,
                engine=None if engine == "auto" else engine,
                detalle=True,
            )
        except ApiError as exc:
            aviso_ocr = {"codigo": exc.codigo, "mensaje": exc.mensaje, "detalle": exc.detalle}

    try:
        subida = await almacen.guardar(
            nombre_original=nombre,
            contenido=contenido,
            content_type=file.content_type,
            lote_id=lote_id,
            ocr=resultado_ocr,
            run_id=run_id,
        )
    except FacturaDuplicada as exc:
        raise ApiError(
            409,
            "factura_ya_existe",
            str(exc),
            {"file_id": file_id, "nota": "GET /api/expedientes/{file_id} devuelve el expediente actual."},
        ) from None
    except DocumentoRechazado as exc:
        raise ApiError(
            500,
            "esquema_incompatible",
            "Mongo rechazo el expediente: el documento no cumple el esquema de `expedientes`.",
            {"detalle": str(exc)},
        ) from None
    except MongoNoDisponible as exc:
        raise ApiError(
            503,
            "mongo_no_disponible",
            "Mongo no esta disponible: no se pudo guardar la factura.",
            {"detalle": str(exc)},
        ) from None

    eventos = list(subida.eventos)
    if aviso_ocr is not None:
        # La traza del fallo se escribe aparte porque `guardar` no ha visto OCR.
        await almacen.evento(
            tipo="OCR_FAIL",
            run_id=run_id,
            file_id=subida.file_id,
            detalle={"solicitado": True, **aviso_ocr},
        )
        eventos.append("OCR_FAIL")

    response.status_code = 200 if subida.duplicado else 201
    ocr_expediente = subida.expediente.get("ocr") or {}
    return {
        "file_id": subida.file_id,
        "duplicado": subida.duplicado,
        "lote_id": subida.lote_id,
        "estado_proceso": subida.estado_proceso,
        "sha256": subida.sha256,
        "tamano_bytes": subida.tamano_bytes,
        "creado_en": subida.creado_en.isoformat(),
        "eventos": eventos,
        "ocr": {
            "solicitado": ocr,
            "ejecutado": resultado_ocr is not None,
            "motor": ocr_expediente.get("motor"),
            "engine": (resultado_ocr or {}).get("motor"),
            "paginas": ocr_expediente.get("paginas"),
            "lineas": len(ocr_expediente.get("lineas") or []),
            "segundos": (resultado_ocr or {}).get("segundos_ocr"),
            "aviso": aviso_ocr,
        },
        "expediente": subida.expediente,
        "urls": {
            "expediente": f"/api/expedientes/{subida.file_id}",
            "pdf": f"/api/facturas/{subida.file_id}/pdf",
        },
    }
