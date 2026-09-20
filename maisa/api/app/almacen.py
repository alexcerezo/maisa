"""Escritura de expedientes: PDF en GridFS, agregado en `expedientes`, traza en `eventos`.

`mongo_repo.py` sigue siendo **solo lectura** a proposito: lo que alli se
consulta es el catalogo del ERP y los snapshots, que gobierna el motor. Aqui
vive la unica parte de la API que muta estado, y esta concentrada en tres
colecciones del esquema (`docker/mongosh/02-schema-init.js`):

  * `pdfs` (GridFS) -> el PDF original, troceado por el driver. Nunca se carga
    entero en memoria ni se escribe en disco.
  * `expedientes`   -> un documento por factura, `_id` = `file_id`. Se respeta
    el validador **estricto**, incluida la invariante INV-2/INV-3: `decision`
    solo existe si `estado_proceso` es `COMPLETADA`. Una subida entra como
    `PENDIENTE` (o `OCR` si se pidio OCR) y `decision: null`.
  * `eventos`       -> traza time-series de lo que hizo la API.

Por que GridFS y no `FACTURAS_DIR`: el contenedor monta sus volumenes en **solo
lectura** (`docker-compose.yml`), asi que la API no necesita permisos de
escritura en disco, el PDF subido sobrevive a un redespliegue y el backup sigue
siendo un unico `mongodump` (decision D-9 de `diseno_logico.md`).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from bson.int64 import Int64
from gridfs import GridFSBucket
from gridfs.errors import NoFile
from pymongo.errors import DuplicateKeyError, PyMongoError, WriteError

from .mongo_repo import MongoNoDisponible, MongoRepo, a_json, sanear

logger = logging.getLogger("albertitos-api")

# ---------------------------------------------------------------------- #
# Contrato con el esquema de Mongo
# ---------------------------------------------------------------------- #
BUCKET_PDFS = "pdfs"
COL_EXPEDIENTES = "expedientes"
COL_EVENTOS = "eventos"
COL_PDFS_FILES = f"{BUCKET_PDFS}.files"

ESQUEMA_VERSION = 1
# `expedientes.lote_id` es "lote1"/"lote2"; la traza del motor usa el entero
# `lote: 1`. La traduccion se hace aqui y en un solo sitio.
LOTES = ("lote1", "lote2")
ESTADO_INICIAL = "PENDIENTE"
ESTADO_TRAS_OCR = "OCR"
# Estados que el validador admite SIN `decision` (los mismos que su `oneOf`).
ESTADOS_SIN_DECISION = ("PENDIENTE", "OCR", "PARSEADA", "CONCILIADA", "DECIDIDA", "ERROR")

# `expedientes.ocr.motor` es un enum cerrado del esquema. El servicio de OCR
# habla de `local`/`cloud`; aqui se traduce a los nombres de motor que el
# esquema conoce.
MOTOR_POR_ENGINE = {"local": "rapidocr", "cloud": "paddleocr_vl"}
MOTOR_POR_DEFECTO = "ninguno"
MOTORES = ("rapidocr", "pytesseract", "pdfplumber", "paddleocr_vl", "ninguno")

TIPOS_EVENTO = ("EXPEDIENTE_ESTADO", "OCR_OK", "OCR_FAIL")
MAX_LINEAS_OCR = 2000
MAX_PAGINAS_GEO = 100

# Un `file_id` legitimo es "<fecha>_<proveedor>.pdf". El patron corta cualquier
# intento de salir de un directorio (barras, "..", rutas absolutas) y es el
# mismo que valida el parametro de ruta en `GET /api/facturas/{file_id}`.
#
# Se usa `\w` y no `[A-Za-z0-9]` porque el corpus real trae nombres acentuados
# ("FA-2116_mensajeria.pdf" con i acentuada, "F26-5240_ofimatica.pdf"): con la
# clase ASCII, 65 de las 500 facturas de la traza devolvian 422 tanto en el
# detalle como en el PDF. `\w` es Unicode por defecto en Python y tambien en el
# motor de Pydantic, y sigue excluyendo lo peligroso (espacios, barras,
# comillas, control). Se evitan escapes `\uXXXX`: Pydantic valida con la crate
# `regex` de Rust, que solo entiende `\u{XXXX}`.
PATRON_FILE_ID = re.compile(r"^[^\W_][\w.\-]{0,180}$")


class NombreInvalido(ValueError):
    """El nombre del fichero no sirve como `file_id`."""


class FacturaDuplicada(RuntimeError):
    """Ya hay un expediente con ese `file_id` y otro contenido."""


class DocumentoRechazado(RuntimeError):
    """Mongo rechazo el documento (validador estricto del esquema)."""


@dataclass(frozen=True)
class Subida:
    """Resultado de guardar una factura nueva."""

    file_id: str
    sha256: str
    tamano_bytes: int
    lote_id: str
    estado_proceso: str
    creado_en: datetime
    duplicado: bool
    expediente: dict
    eventos: tuple[str, ...] = ()


# ---------------------------------------------------------------------- #
# Validacion del nombre y traduccion del OCR
# ---------------------------------------------------------------------- #
def normalizar_file_id(nombre: str) -> str:
    """`file_id` a partir del nombre del fichero subido.

    Se queda con el basename (un navegador puede mandar una ruta), exige el
    patron y exige extension `.pdf`: la API no acepta otro formato porque todo
    el sistema (GridFS, traza, motor) habla de facturas en PDF.

    Se normaliza a NFC porque `\\w` no cubre las marcas combinantes: un nombre
    en NFD (lo tipico de macOS) como "informa" + acento combinante no casaria
    con el patron ni, despues, con el `file_id` que guarda la traza.
    """
    limpio = unicodedata.normalize(
        "NFC", (nombre or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    )
    if limpio in {"", ".", ".."} or not PATRON_FILE_ID.match(limpio):
        raise NombreInvalido(
            "El nombre del fichero no es valido: usa letras (acentos incluidos), digitos, "
            "punto, guion o guion bajo (maximo 181 caracteres) y sin rutas."
        )
    if not limpio.lower().endswith(".pdf"):
        raise NombreInvalido("Solo se aceptan ficheros PDF.")
    return limpio


def lote_id_desde_numero(lote: int) -> str:
    """`1` -> `"lote1"`, `2` -> `"lote2"`."""
    if lote not in (1, 2):
        raise NombreInvalido(f"El lote debe ser 1 o 2, no {lote}.")
    return f"lote{lote}"


def _bbox_desde_box(box: Any) -> list[float] | None:
    """Poligono del OCR -> `[x0, y0, x1, y1]`, que es lo que admite el esquema.

    El OCR devuelve 4 puntos (`[[x,y], ...]`); `expedientes.ocr.lineas[].bbox`
    es un array plano de 4 doubles, asi que se toma la caja envolvente.
    """
    if not isinstance(box, list) or not box:
        return None
    if len(box) == 4 and all(isinstance(v, (int, float)) for v in box):
        return [float(v) for v in box]
    xs: list[float] = []
    ys: list[float] = []
    for punto in box:
        if not isinstance(punto, (list, tuple)) or len(punto) < 2:
            return None
        try:
            xs.append(float(punto[0]))
            ys.append(float(punto[1]))
        except (TypeError, ValueError):
            return None
    if len(xs) != 4:
        return None
    return [min(xs), min(ys), max(xs), max(ys)]


def _linea_ocr(linea: Any, pagina: int) -> dict | None:
    if not isinstance(linea, dict):
        return None
    texto = linea.get("text")
    if not isinstance(texto, str) or not texto.strip():
        return None
    fila: dict[str, Any] = {"pagina": int(pagina), "texto": texto}
    caja = _bbox_desde_box(linea.get("box"))
    if caja is not None:
        fila["bbox"] = caja
    score = linea.get("score")
    if isinstance(score, (int, float)) and 0.0 <= float(score) <= 1.0:
        fila["score"] = float(score)
    return fila


def lineas_desde_ocr(bruto: dict[str, Any]) -> list[dict]:
    """Lineas del payload del OCR en el formato de `expedientes.ocr.lineas`.

    El OCR devuelve las lineas por pagina en `results[].lines` y, ademas, una
    copia plana en `lines` sin pagina. Se usa la version por pagina para poder
    rellenar `pagina`; el indice del array es la pagina 0-based.
    """
    salida: list[dict] = []
    paginas = bruto.get("results")
    if isinstance(paginas, list) and paginas:
        for indice, pagina in enumerate(paginas):
            if not isinstance(pagina, dict):
                continue
            for linea in pagina.get("lines") or []:
                fila = _linea_ocr(linea, indice)
                if fila is not None:
                    salida.append(fila)
    else:
        for linea in bruto.get("lines") or []:
            fila = _linea_ocr(linea, 0)
            if fila is not None:
                salida.append(fila)
    return salida[:MAX_LINEAS_OCR]


def paginas_geo_desde_ocr(bruto: dict[str, Any]) -> list[dict]:
    """Geometria por pagina del payload del OCR, en el formato de la cache del motor.

    `lineas_desde_ocr` guarda la caja pero **pierde la escala de la pagina**, y
    sin escala la caja no se puede pintar sobre el PDF: el OCR mide en pixeles
    del bitmap renderizado (hasta 288 dpi) y el visor pinta en puntos. Esta
    funcion conserva escala y tamano del render junto a las lineas, con la misma
    forma que `motor/.cache/ocr/<sha256>.json` (`pagina`/`escala`/`ancho`/`alto`
    y `lineas[].texto|score|caja`), para que el visor resuelva igual las
    escaneadas del lote (que lee de la cache) y las que entran por la API.

    Devuelve `[]` si el payload no trae geometria utilizable: la factura se lee
    igual, solo que sin resaltado.
    """
    resultados = bruto.get("results")
    if not isinstance(resultados, list):
        return []
    paginas: list[dict] = []
    for indice, pagina in enumerate(resultados[:MAX_PAGINAS_GEO]):
        if not isinstance(pagina, dict):
            continue
        escala = pagina.get("scale")
        if not isinstance(escala, (int, float)) or float(escala) <= 0:
            # Una caja sin su escala es una caja inutil.
            continue
        tamano = pagina.get("size") if isinstance(pagina.get("size"), dict) else {}
        ancho, alto = tamano.get("width"), tamano.get("height")
        tiene_tamano = (
            isinstance(ancho, (int, float))
            and isinstance(alto, (int, float))
            and float(ancho) > 0
            and float(alto) > 0
        )
        lineas: list[dict] = []
        for linea in pagina.get("lines") or []:
            if not isinstance(linea, dict):
                continue
            texto = linea.get("text")
            caja = _bbox_desde_box(linea.get("box"))
            if not isinstance(texto, str) or not texto.strip() or caja is None:
                continue
            fila: dict[str, Any] = {"texto": texto, "caja": caja}
            score = linea.get("score")
            if isinstance(score, (int, float)) and 0.0 <= float(score) <= 1.0:
                fila["score"] = round(float(score), 6)
            lineas.append(fila)
        if not lineas:
            continue
        # El validador de Mongo acota `lineas` a 2000 por pagina: se corta aqui
        # para no escribir un documento que el esquema rechace.
        entrada: dict[str, Any] = {
            "pagina": indice,
            "escala": float(escala),
            "lineas": lineas[:MAX_LINEAS_OCR],
        }
        if tiene_tamano:
            entrada["ancho"] = float(ancho)
            entrada["alto"] = float(alto)
        paginas.append(entrada)
    return paginas


def motor_desde_engine(engine: Any, hay_lineas: bool) -> str:
    """Motor del OCR del servicio -> valor del enum del esquema."""
    clave = str(engine or "").strip().lower()
    if clave in MOTOR_POR_ENGINE:
        return MOTOR_POR_ENGINE[clave]
    return "rapidocr" if hay_lineas else MOTOR_POR_DEFECTO


# ---------------------------------------------------------------------- #
# Almacen
# ---------------------------------------------------------------------- #
class AlmacenFacturas:
    """Unico punto de escritura de la API.

    Comparte el `MongoClient` de `MongoRepo` (`MongoRepo.base()`): abrir otro
    cliente por proceso duplicaria el pool y los handshakes sin ganar nada.
    """

    def __init__(self, mongo: MongoRepo) -> None:
        self._mongo = mongo

    @property
    def uri(self) -> str:
        return self._mongo.uri

    @property
    def db_nombre(self) -> str:
        return self._mongo.db_nombre

    def _db(self):
        try:
            return self._mongo.base()
        except MongoNoDisponible:
            raise
        except Exception as exc:
            raise MongoNoDisponible(sanear(str(exc), self._mongo.uri)) from None

    def _envolver(self, exc: Exception) -> Exception:
        """Traduce un fallo de pymongo a algo que el router sepa mapear."""
        if isinstance(exc, (MongoNoDisponible, FacturaDuplicada, DocumentoRechazado, NombreInvalido)):
            return exc
        if isinstance(exc, DuplicateKeyError):
            return FacturaDuplicada("Ya existe un expediente con ese file_id.")
        if isinstance(exc, WriteError):
            # code 121 = Document failed validation (validador estricto).
            if exc.code == 121:
                return DocumentoRechazado(sanear(str(exc.details or exc), self._mongo.uri))
            return MongoNoDisponible(sanear(str(exc), self._mongo.uri))
        if isinstance(exc, PyMongoError):
            return MongoNoDisponible(sanear(str(exc), self._mongo.uri))
        return exc

    # ------------------------------------------------------------------ #
    # Escritura
    # ------------------------------------------------------------------ #
    def _emitir_evento(
        self,
        db,
        *,
        tipo: str,
        run_id: str,
        file_id: str | None,
        detalle: dict[str, Any],
        duracion_ms: int | None = None,
        ahora: datetime | None = None,
    ) -> None:
        """Un documento de `eventos`. La traza no puede tumbar la subida.

        `eventos` es time-series y Mongo 7 no admite validador en ellas: el
        contrato de campos lo sostiene esta funcion. Si el insert falla se
        registra en el log y se sigue; perder la traza es mejor que perder el
        PDF que el usuario acaba de subir.
        """
        documento = {
            "ts": ahora or datetime.now(timezone.utc),
            "run_id": run_id,
            "tipo": tipo,
            "file_id": file_id,
            "detalle": detalle,
        }
        if duracion_ms is not None:
            documento["duracion_ms"] = int(duracion_ms)
        try:
            db[COL_EVENTOS].insert_one(documento)
        except Exception as exc:
            logger.warning("No se pudo escribir la traza %s: %s", tipo, sanear(str(exc), self._mongo.uri))

    def _guardar(
        self,
        *,
        nombre_original: str,
        contenido: bytes,
        content_type: str | None,
        lote_id: str,
        ocr: dict[str, Any] | None,
        run_id: str,
    ) -> Subida:
        file_id = normalizar_file_id(nombre_original)
        if lote_id not in LOTES:
            raise NombreInvalido(f"lote_id debe ser uno de {LOTES}.")
        sha256 = hashlib.sha256(contenido).hexdigest()
        db = self._db()
        expedientes = db[COL_EXPEDIENTES]

        previo = expedientes.find_one({"_id": file_id})
        if previo is not None:
            if (previo.get("documento") or {}).get("sha256") == sha256:
                # Idempotente: mismo nombre y mismo contenido. Se devuelve lo que
                # ya hay en vez de duplicar el PDF en GridFS.
                return Subida(
                    file_id=file_id,
                    sha256=sha256,
                    tamano_bytes=len(contenido),
                    lote_id=previo.get("lote_id", lote_id),
                    estado_proceso=previo.get("estado_proceso", ESTADO_INICIAL),
                    creado_en=previo.get("creado_en") or datetime.now(timezone.utc),
                    duplicado=True,
                    expediente=a_json(previo),
                )
            raise FacturaDuplicada(
                f"Ya existe una factura '{file_id}' con otro contenido. "
                "Renombra el fichero o borra el expediente anterior."
            )

        eventos: list[str] = []
        bucket = GridFSBucket(db, bucket_name=BUCKET_PDFS)
        ahora = datetime.now(timezone.utc)
        gridfs_id = bucket.upload_from_stream(
            file_id,
            contenido,
            metadata={
                "file_id": file_id,
                "lote_id": lote_id,
                "sha256": sha256,
                "content_type": content_type or "application/pdf",
            },
        )

        lineas = lineas_desde_ocr((ocr or {}).get("bruto") or {})
        motor = motor_desde_engine((ocr or {}).get("motor"), bool(lineas))
        estado = ESTADO_TRAS_OCR if ocr is not None else ESTADO_INICIAL
        paginas = (ocr or {}).get("paginas")
        geo = paginas_geo_desde_ocr((ocr or {}).get("bruto") or {})

        documento: dict[str, Any] = {
            "_id": file_id,
            "lote_id": lote_id,
            "estado_proceso": estado,
            "esquema_version": ESQUEMA_VERSION,
            "creado_en": ahora,
            "actualizado_en": ahora,
            "documento": {
                "nombre_original": nombre_original,
                # `tamano_bytes` es `long` en el esquema: un int de Python
                # pequeno se guardaria como int32 y el validador lo rechazaria.
                "tamano_bytes": Int64(len(contenido)),
                "sha256": sha256,
                "gridfs_id": gridfs_id,
            },
            "ocr": {
                "motor": motor,
                "lineas": lineas,
                "disponible": ocr is not None,
            },
            # INV-2/INV-3: la decision solo existe si esta COMPLETADA.
            "decision": None,
            "origen": {
                "fuente": "API",
                "run_id": run_id,
                "content_type": content_type or "application/pdf",
            },
        }
        if isinstance(paginas, int) and paginas >= 0:
            documento["documento"]["paginas"] = paginas
            documento["ocr"]["paginas"] = paginas
        if geo:
            documento["ocr"]["paginas_geo"] = geo
        if ocr is not None and isinstance(ocr.get("segundos_proxy"), (int, float)):
            documento["ocr"]["duracion_ms"] = int(float(ocr["segundos_proxy"]) * 1000)
        if ocr is not None and isinstance(ocr.get("texto"), str) and ocr["texto"].strip():
            # El esquema no tiene un campo de texto plano y `lineas` puede venir
            # vacio (el motor cloud no devuelve cajas). Se guarda aparte para no
            # perder la lectura.
            documento["ocr"]["texto"] = ocr["texto"]

        try:
            expedientes.insert_one(documento)
        except Exception as exc:
            # Compensacion: si el expediente no entra, el PDF tampoco se queda.
            try:
                bucket.delete(gridfs_id)
            except Exception:
                logger.warning("No se pudo deshacer el PDF %s de GridFS", file_id)
            raise self._envolver(exc) from None

        self._emitir_evento(
            db,
            tipo="EXPEDIENTE_ESTADO",
            run_id=run_id,
            file_id=file_id,
            detalle={
                "estado_proceso": estado,
                "lote_id": lote_id,
                "origen": "API",
                "sha256": sha256,
                "tamano_bytes": len(contenido),
            },
            ahora=ahora,
        )
        eventos.append("EXPEDIENTE_ESTADO")

        if ocr is not None:
            tipo = "OCR_OK" if lineas or ocr.get("texto") else "OCR_FAIL"
            self._emitir_evento(
                db,
                tipo=tipo,
                run_id=run_id,
                file_id=file_id,
                detalle={
                    "motor": motor,
                    "engine": ocr.get("motor"),
                    "paginas": paginas,
                    "lineas": len(lineas),
                    "score": ocr.get("score"),
                },
                duracion_ms=documento["ocr"].get("duracion_ms"),
                ahora=ahora,
            )
            eventos.append(tipo)

        logger.info(
            "Expediente guardado: file_id=%s lote_id=%s estado=%s bytes=%d eventos=%s",
            file_id, lote_id, estado, len(contenido), ",".join(eventos),
        )
        return Subida(
            file_id=file_id,
            sha256=sha256,
            tamano_bytes=len(contenido),
            lote_id=lote_id,
            estado_proceso=estado,
            creado_en=ahora,
            duplicado=False,
            expediente=a_json(documento),
            eventos=tuple(eventos),
        )

    async def guardar(
        self,
        *,
        nombre_original: str,
        contenido: bytes,
        content_type: str | None = None,
        lote_id: str = "lote1",
        ocr: dict[str, Any] | None = None,
        run_id: str = "api",
    ) -> Subida:
        """Guarda el PDF en GridFS y crea su expediente. Bloqueante en un hilo."""
        try:
            return await asyncio.to_thread(
                self._guardar,
                nombre_original=nombre_original,
                contenido=contenido,
                content_type=content_type,
                lote_id=lote_id,
                ocr=ocr,
                run_id=run_id,
            )
        except (MongoNoDisponible, FacturaDuplicada, DocumentoRechazado, NombreInvalido):
            raise
        except Exception as exc:
            raise self._envolver(exc) from None

    def _emitir_evento_suelto(self, **kwargs: Any) -> None:
        self._emitir_evento(self._db(), **kwargs)

    async def evento(self, **kwargs: Any) -> None:
        """Traza suelta (por ejemplo, un OCR que fallo antes de guardar nada)."""
        try:
            await asyncio.to_thread(self._emitir_evento_suelto, **kwargs)
        except MongoNoDisponible:
            raise
        except Exception as exc:
            logger.warning("No se pudo escribir la traza: %s", sanear(str(exc), self._mongo.uri))

    # ------------------------------------------------------------------ #
    # Lectura de lo subido
    # ------------------------------------------------------------------ #
    def _obtener(self, file_id: str) -> dict | None:
        documento = self._db()[COL_EXPEDIENTES].find_one({"_id": file_id})
        return a_json(documento) if documento is not None else None

    async def obtener(self, file_id: str) -> dict | None:
        try:
            return await asyncio.to_thread(self._obtener, file_id)
        except MongoNoDisponible:
            raise
        except Exception as exc:
            raise self._envolver(exc) from None

    def _listar(
        self,
        *,
        lote_id: str | None,
        estado: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict], int]:
        coleccion = self._db()[COL_EXPEDIENTES]
        filtro: dict[str, Any] = {}
        if lote_id is not None:
            filtro["lote_id"] = lote_id
        if estado is not None:
            filtro["estado_proceso"] = estado
        total = coleccion.count_documents(filtro)
        cursor = (
            coleccion.find(filtro)
            .sort([("creado_en", -1), ("_id", 1)])
            .skip(offset)
            .limit(limit)
        )
        return [a_json(doc) for doc in cursor], total

    async def listar(
        self,
        *,
        lote_id: str | None = None,
        estado: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        try:
            return await asyncio.to_thread(
                self._listar, lote_id=lote_id, estado=estado, limit=limit, offset=offset
            )
        except MongoNoDisponible:
            raise
        except Exception as exc:
            raise self._envolver(exc) from None

    def _abrir_pdf(self, file_id: str) -> bytes | None:
        """Descarga el PDF de GridFS. None si no hay ningun fichero con ese nombre."""
        bucket = GridFSBucket(self._db(), bucket_name=BUCKET_PDFS)
        try:
            flujo = bucket.open_download_stream_by_name(file_id)
        except NoFile:
            return None
        try:
            return flujo.read()
        finally:
            flujo.close()

    async def abrir_pdf(self, file_id: str) -> bytes | None:
        try:
            return await asyncio.to_thread(self._abrir_pdf, file_id)
        except MongoNoDisponible:
            raise
        except Exception as exc:
            raise self._envolver(exc) from None

    # ------------------------------------------------------------------ #
    # Diagnostico
    # ------------------------------------------------------------------ #
    def _estado(self) -> dict[str, Any]:
        db = self._db()
        estado: dict[str, Any] = {
            "bucket": BUCKET_PDFS,
            "expedientes": db[COL_EXPEDIENTES].count_documents({}),
            "pdfs": db[COL_PDFS_FILES].count_documents({}),
        }
        try:
            estado["eventos"] = db[COL_EVENTOS].count_documents({})
        except PyMongoError as exc:
            # `eventos` es time-series: contarlas obliga a leer
            # `system.buckets.eventos`, y el usuario de la app tiene `readWrite`
            # sobre la base pero no sobre las colecciones internas. Escribir en
            # la traza si funciona: solo falla el recuento.
            estado["eventos"] = None
            estado["eventos_error"] = sanear(str(exc), self._mongo.uri)
        return estado

    async def estado(self) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(self._estado)
        except MongoNoDisponible:
            raise
        except Exception as exc:
            raise self._envolver(exc) from None
