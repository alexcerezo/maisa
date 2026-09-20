"""Acceso a MongoDB, con la escritura reducida a un unico caso.

Reglas que se cumplen aqui y conviene no romper:

  * Para todo lo que decide el motor (asientos, snapshots, expedientes): solo
    `find`, `count_documents` y `aggregate`. Nunca `insert`/`update`/`delete`/
    `$out`/`$merge`/`$where`: esa escritura la gobierna el motor, no la API.
  * **Las dos unicas escrituras de la API estan acotadas y justificadas:**
      1. La subida de facturas vive aparte, en `almacen.py`, y reutiliza
         **este mismo cliente** via `MongoRepo.base()` (coleccion de subidas).
      2. Las colecciones `revisiones` y `correcciones`: marcar una factura
         `ESCALAR` como revisada, y anotar los datos que un operador completa a
         mano, son acciones humanas sobre el panel, no decisiones del pipeline.
         Viven en sus propias colecciones sin tocar `asientos` ni
         `expedientes`. Son los unicos lugares de este modulo que hacen
         `update_one(..., upsert=True)`.
    Lo que **nunca** ocurre aqui: reescribir `resultado`. Que una persona
    corrija el NIF que el motor no supo leer **no cambia la decision**; la
    correccion es una anotacion al lado, y la decision sigue siendo la del
    motor. Cambiarla seria falsear la traza de auditoria.
  * El usuario de app (`albertitos_app`) ya es `readWrite`, asi que el limite
    de las demas colecciones lo pone esta capa, no los permisos de Mongo.
  * Un unico `MongoClient` reutilizado por proceso (crear un cliente por
    peticion agota el pool y multiplica los handshakes).
  * Timeouts cortos (`MONGO_TIMEOUT_MS`): si Mongo no esta, la API debe
    degradarse en decimas, no colgarse.
  * Las busquedas se construyen con `re.escape` y longitud acotada, nunca con
    `$where` ni con el texto del usuario concatenado.
  * Los errores que se propagan al cliente no llevan la cadena de conexion.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timezone
from typing import Any

from bson import ObjectId
from bson.decimal128 import Decimal128
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import PyMongoError

from .config import sanitize_uri

logger = logging.getLogger("albertitos-api")

# Colecciones del esquema (docker/mongosh/02-schema-init.js) que la API consulta.
COLECCION_ASIENTOS = "asientos"
COLECCION_SNAPSHOTS = "erp_snapshots"
COLECCION_REVISIONES = "revisiones"
COLECCION_CORRECCIONES = "correcciones"

# Indices que el esquema deberia tener para las consultas de esta API.
# La API NO los crea: si falta alguno, se avisa en /health y en el README.
INDICES_ESPERADOS: dict[str, tuple[str, ...]] = {
    COLECCION_ASIENTOS: (
        "ix_pedido_vigente",
        "ix_nif_vigente",
        "ix_nif_importe_vigente",
        "ix_snapshot",
        "ix_vigente_parcial",
    ),
    COLECCION_SNAPSHOTS: ("ix_descargado", "ix_vigente_parcial"),
    COLECCION_REVISIONES: ("ix_estado",),
    COLECCION_CORRECCIONES: ("ix_actualizado",),
}

ESTADOS_REVISION = ("PENDIENTE", "RESUELTA")

# Campos que un operador puede completar o corregir a mano. Es la misma lista
# que usa el resaltado (`anclajes.CAMPOS`): los datos que viven *dentro* del
# documento. Lo que viene del ERP (asiento, importe_erp) no se toca: no esta en
# el PDF y el operador no tiene forma de verificarlo mirandolo.
CAMPOS_CORREGIBLES = ("pedido", "nif", "iban", "fecha", "base", "iva", "total")

# Un valor corregido a mano es texto libre, pero acotado: por encima de esto no
# es un dato de factura, es un pegado accidental.
MAX_VALOR_CORREGIDO = 200
MAX_NOTA_CORRECCION = 500
MAX_CORRECCIONES = len(CAMPOS_CORREGIBLES)

PROYECCION_REVISION = {
    "_id": 1,
    "estado": 1,
    "revisor": 1,
    "comentario": 1,
    "actualizado_en": 1,
}

PROYECCION_CORRECCION = {
    "_id": 1,
    "campos": 1,
    "actualizado_en": 1,
}

# Proyeccion de lectura: se excluye lo pesado si algun dia el esquema crece.
PROYECCION_ASIENTO = {
    "_id": 1,
    "asiento_id": 1,
    "snapshot_id": 1,
    "fecha": 1,
    "proveedor": 1,
    "nif": 1,
    "pedido": 1,
    "importe": 1,
    "estado": 1,
    "vigente": 1,
    "esquema_version": 1,
}

PROYECCION_SNAPSHOT = {
    "_id": 1,
    "descargado_en": 1,
    "total_asientos": 1,
    "paginas": 1,
    "vigente": 1,
    "estado": 1,
    "reintentos": 1,
    "duracion_ms": 1,
    "esquema_version": 1,
}


class MongoNoDisponible(RuntimeError):
    """Mongo no responde. El mensaje ya viene saneado (sin credenciales)."""


def sanear(texto: str, uri: str) -> str:
    """Quita credenciales de un mensaje de error antes de loguearlo o servirlo."""
    saneado = texto
    saneada_uri = sanitize_uri(uri)
    if saneada_uri != uri:
        # Los drivers citan a veces la URI completa en el error.
        saneado = saneado.replace(uri, saneada_uri)
        if "@" in uri:
            credenciales = uri.split("://", 1)[-1].split("@", 1)[0]
            saneado = saneado.replace(credenciales, "***")
    return saneado


def a_json(valor: Any) -> Any:
    """Convierte BSON a algo serializable en JSON sin perder informacion util."""
    if isinstance(valor, ObjectId):
        return str(valor)
    if isinstance(valor, Decimal128):
        return float(valor.to_decimal())
    if isinstance(valor, datetime):
        return valor.isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    if isinstance(valor, dict):
        return {clave: a_json(dato) for clave, dato in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [a_json(dato) for dato in valor]
    if isinstance(valor, (str, int, float, bool)) or valor is None:
        return valor
    return str(valor)


def _patron(texto: str, max_len: int) -> re.Pattern[str]:
    return re.compile(re.escape(texto.strip()[:max_len]), re.IGNORECASE)


class MongoRepo:
    """Repositorio de lectura sobre la base `albertitos`."""

    def __init__(
        self,
        uri: str,
        db: str,
        timeout_ms: int = 1500,
        max_query_len: int = 64,
    ) -> None:
        self.uri = uri
        self.db_nombre = db
        self.timeout_ms = timeout_ms
        self.max_query_len = max_query_len
        self._cliente: MongoClient | None = None
        self._error_arranque: str | None = None
        try:
            # Lazy: MongoClient no conecta hasta la primera operacion, asi que
            # la API arranca aunque Mongo este caido.
            self._cliente = MongoClient(
                uri,
                serverSelectionTimeoutMS=timeout_ms,
                connectTimeoutMS=timeout_ms,
                socketTimeoutMS=max(timeout_ms * 2, 2000),
                appname="albertitos-api",
                tz_aware=False,
            )
        except Exception as exc:  # URI invalida, opciones incompatibles...
            self._error_arranque = sanear(str(exc), uri)
            logger.error("Mongo: configuracion invalida (%s)", self._error_arranque)

    # ------------------------------------------------------------------ #
    # Infraestructura
    # ------------------------------------------------------------------ #
    @property
    def configurado(self) -> bool:
        return self._cliente is not None

    def _db(self):
        if self._cliente is None:
            raise MongoNoDisponible(self._error_arranque or "Cliente de Mongo no configurado.")
        return self._cliente[self.db_nombre]

    def base(self):
        """`Database` de la API, para que la capa de escritura reuse el cliente.

        Existe para `almacen.py`: un `MongoClient` por proceso es la regla, y
        abrir un segundo cliente solo para escribir duplicaria el pool y los
        handshakes. Las consultas de esta clase siguen siendo de solo lectura.
        """
        return self._db()

    def cerrar(self) -> None:
        if self._cliente is not None:
            self._cliente.close()

    def ping(self) -> None:
        """Comprueba la conexion. Lanza MongoNoDisponible si no responde."""
        try:
            self._db().command("ping")
        except PyMongoError as exc:
            raise MongoNoDisponible(sanear(str(exc), self.uri)) from None
        except Exception as exc:  # pymongo puede lanzar errores de otro tipo
            raise MongoNoDisponible(sanear(str(exc), self.uri)) from None

    async def ping_async(self) -> None:
        await asyncio.to_thread(self.ping)

    def indices_faltantes(self) -> dict[str, list[str]]:
        """Indices del esquema que faltan. No crea nada: solo informa."""
        faltantes: dict[str, list[str]] = {}
        try:
            db = self._db()
            for coleccion, esperados in INDICES_ESPERADOS.items():
                presentes = set(db[coleccion].index_information().keys())
                ausentes = [nombre for nombre in esperados if nombre not in presentes]
                if ausentes:
                    faltantes[coleccion] = ausentes
        except Exception as exc:
            logger.warning("No se pudieron comprobar los indices: %s", sanear(str(exc), self.uri))
            raise MongoNoDisponible(sanear(str(exc), self.uri)) from None
        return faltantes

    # ------------------------------------------------------------------ #
    # Asientos
    # ------------------------------------------------------------------ #
    def _filtro_asientos(self, vigente: bool | None, q: str | None) -> dict:
        filtro: dict[str, Any] = {}
        if vigente is not None:
            filtro["vigente"] = vigente
        if q:
            patron = _patron(q, self.max_query_len)
            filtro["$or"] = [
                {"asiento_id": {"$regex": patron.pattern, "$options": "i"}},
                {"nif": {"$regex": patron.pattern, "$options": "i"}},
                {"pedido": {"$regex": patron.pattern, "$options": "i"}},
            ]
        return filtro

    def _listar_asientos(self, vigente: bool | None, q: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
        coleccion = self._db()[COLECCION_ASIENTOS]
        filtro = self._filtro_asientos(vigente, q)
        total = coleccion.count_documents(filtro)
        cursor = (
            coleccion.find(filtro, PROYECCION_ASIENTO)
            .sort([("asiento_id", ASCENDING), ("snapshot_id", DESCENDING)])
            .skip(offset)
            .limit(limit)
        )
        return [a_json(doc) for doc in cursor], total

    async def listar_asientos(
        self, *, vigente: bool | None = None, q: str | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[list[dict], int]:
        try:
            return await asyncio.to_thread(self._listar_asientos, vigente, q, limit, offset)
        except MongoNoDisponible:
            raise
        except Exception as exc:
            raise MongoNoDisponible(sanear(str(exc), self.uri)) from None

    def _obtener_asiento(self, asiento_id: str) -> dict | None:
        coleccion = self._db()[COLECCION_ASIENTOS]
        # Se prefiere el asiento vigente; si hay varios snapshots, el mas
        # reciente (snapshot_id descendente).
        cursor = (
            coleccion.find({"asiento_id": asiento_id}, PROYECCION_ASIENTO)
            .sort([("vigente", DESCENDING), ("snapshot_id", DESCENDING)])
            .limit(1)
        )
        for doc in cursor:
            return a_json(doc)
        return None

    async def obtener_asiento(self, asiento_id: str) -> dict | None:
        try:
            return await asyncio.to_thread(self._obtener_asiento, asiento_id)
        except MongoNoDisponible:
            raise
        except Exception as exc:
            raise MongoNoDisponible(sanear(str(exc), self.uri)) from None

    def _contar_asientos_vigentes(self) -> int:
        return self._db()[COLECCION_ASIENTOS].count_documents({"vigente": True})

    async def contar_asientos_vigentes(self) -> int:
        try:
            return await asyncio.to_thread(self._contar_asientos_vigentes)
        except MongoNoDisponible:
            raise
        except Exception as exc:
            raise MongoNoDisponible(sanear(str(exc), self.uri)) from None

    # ------------------------------------------------------------------ #
    # Snapshots del ERP
    # ------------------------------------------------------------------ #
    def _listar_snapshots(self, limit: int, offset: int) -> tuple[list[dict], int]:
        coleccion = self._db()[COLECCION_SNAPSHOTS]
        total = coleccion.count_documents({})
        cursor = (
            coleccion.find({}, PROYECCION_SNAPSHOT)
            .sort([("descargado_en", DESCENDING), ("_id", DESCENDING)])
            .skip(offset)
            .limit(limit)
        )
        return [a_json(doc) for doc in cursor], total

    async def listar_snapshots(self, *, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
        try:
            return await asyncio.to_thread(self._listar_snapshots, limit, offset)
        except MongoNoDisponible:
            raise
        except Exception as exc:
            raise MongoNoDisponible(sanear(str(exc), self.uri)) from None

    def _obtener_snapshot_vigente(self) -> dict | None:
        coleccion = self._db()[COLECCION_SNAPSHOTS]
        cursor = (
            coleccion.find({"vigente": True}, PROYECCION_SNAPSHOT)
            .sort([("descargado_en", DESCENDING)])
            .limit(1)
        )
        for doc in cursor:
            return a_json(doc)
        return None

    async def obtener_snapshot_vigente(self) -> dict | None:
        try:
            return await asyncio.to_thread(self._obtener_snapshot_vigente)
        except MongoNoDisponible:
            raise
        except Exception as exc:
            raise MongoNoDisponible(sanear(str(exc), self.uri)) from None

    # ------------------------------------------------------------------ #
    # Revision humana de facturas ESCALAR (unica escritura de este modulo)
    # ------------------------------------------------------------------ #
    def _listar_revisiones(self, file_ids: list[str]) -> dict[str, dict]:
        if not file_ids:
            return {}
        coleccion = self._db()[COLECCION_REVISIONES]
        cursor = coleccion.find({"_id": {"$in": list(file_ids)}}, PROYECCION_REVISION)
        return {doc["_id"]: a_json(doc) for doc in cursor}

    async def listar_revisiones(self, file_ids: list[str]) -> dict[str, dict]:
        """Revisiones existentes, indexadas por `file_id`. Ausente == pendiente."""
        try:
            return await asyncio.to_thread(self._listar_revisiones, file_ids)
        except MongoNoDisponible:
            raise
        except Exception as exc:
            raise MongoNoDisponible(sanear(str(exc), self.uri)) from None

    async def obtener_revision(self, file_id: str) -> dict | None:
        revisiones = await self.listar_revisiones([file_id])
        return revisiones.get(file_id)

    def _marcar_revision(
        self, file_id: str, *, estado: str, revisor: str | None, comentario: str | None
    ) -> dict:
        coleccion = self._db()[COLECCION_REVISIONES]
        coleccion.update_one(
            {"_id": file_id},
            {
                "$set": {
                    "estado": estado,
                    "revisor": revisor,
                    "comentario": comentario,
                    "actualizado_en": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )
        return a_json(coleccion.find_one({"_id": file_id}, PROYECCION_REVISION))

    async def marcar_revision(
        self, file_id: str, *, estado: str, revisor: str | None = None, comentario: str | None = None
    ) -> dict:
        """Registra que un operador marco `file_id` como PENDIENTE o RESUELTA.

        Es intencionadamente un `upsert`: la primera vez que se revisa una
        factura no existe fila previa, y volver a marcarla actualiza la misma.
        """
        try:
            return await asyncio.to_thread(
                self._marcar_revision, file_id, estado=estado, revisor=revisor, comentario=comentario
            )
        except MongoNoDisponible:
            raise
        except Exception as exc:
            raise MongoNoDisponible(sanear(str(exc), self.uri)) from None

    # ------------------------------------------------------------------ #
    # Datos que un operador completa o corrige a mano
    # ------------------------------------------------------------------ #
    def _listar_correcciones(self, file_ids: list[str]) -> dict[str, dict]:
        if not file_ids:
            return {}
        coleccion = self._db()[COLECCION_CORRECCIONES]
        cursor = coleccion.find({"_id": {"$in": list(file_ids)}}, PROYECCION_CORRECCION)
        return {doc["_id"]: a_json(doc) for doc in cursor}

    async def listar_correcciones(self, file_ids: list[str]) -> dict[str, dict]:
        """Correcciones existentes, indexadas por `file_id`. Ausente == sin corregir."""
        try:
            return await asyncio.to_thread(self._listar_correcciones, file_ids)
        except MongoNoDisponible:
            raise
        except Exception as exc:
            raise MongoNoDisponible(sanear(str(exc), self.uri)) from None

    async def obtener_correcciones(self, file_id: str) -> dict | None:
        correcciones = await self.listar_correcciones([file_id])
        return correcciones.get(file_id)

    def _guardar_correcciones(self, file_id: str, campos: dict, autor: str | None) -> dict:
        """Fusiona `campos` sobre lo que ya hubiera: corregir el NIF no borra el IBAN.

        Un `$set` con rutas punteadas (`campos.nif.valor`) permite que la
        operacion sea **parcial**: el panel manda solo el campo que el operador
        acaba de completar y lo demas se conserva. Es un `upsert` porque la
        primera correccion de una factura no tiene fila previa.
        """
        coleccion = self._db()[COLECCION_CORRECCIONES]
        ahora = datetime.now(timezone.utc)
        actualizar: dict[str, Any] = {"actualizado_en": ahora}
        for campo, dato in campos.items():
            actualizar[f"campos.{campo}.valor"] = dato["valor"]
            actualizar[f"campos.{campo}.nota"] = dato.get("nota")
            actualizar[f"campos.{campo}.autor"] = autor
            actualizar[f"campos.{campo}.actualizado_en"] = ahora
        coleccion.update_one(
            {"_id": file_id},
            {"$set": actualizar, "$setOnInsert": {"creado_en": ahora}},
            upsert=True,
        )
        return a_json(coleccion.find_one({"_id": file_id}, PROYECCION_CORRECCION))

    async def guardar_correcciones(self, file_id: str, campos: dict, autor: str | None = None) -> dict:
        """Anota los datos que un operador ha completado a mano.

        Escribe **solo** en `correcciones`: no toca la traza, no toca
        `expedientes` y por supuesto no cambia `resultado`. La decision sigue
        siendo la del motor.
        """
        try:
            return await asyncio.to_thread(self._guardar_correcciones, file_id, campos, autor)
        except MongoNoDisponible:
            raise
        except Exception as exc:
            raise MongoNoDisponible(sanear(str(exc), self.uri)) from None

    def _borrar_correcciones(self, file_id: str, campo: str | None) -> dict | None:
        """Deshace una correccion (un campo) o todas (el documento entero).

        Si al quitar un campo la factura se queda sin ninguno corregido, se
        borra el documento: dejar un `campos: {}` huerfano haria que el panel
        creyera que hay algo que mostrar.

        **Por que no se hace con un `$unset` y una limpieza despues.** El
        validador de `correcciones` exige `minProperties: 1` en `campos` y esta
        en `validationAction: "error"`, asi que Mongo valida el documento
        **resultante de cada escritura**, no solo el final. Quitar el ultimo
        campo con `$unset` dejaria `campos: {}` un instante y Mongo rechaza esa
        escritura con un error de validacion: la limpieza posterior no llega a
        ejecutarse, y deshacer la **ultima** correccion de una factura fallaba
        siempre. Hay que decidir antes de escribir.
        """
        coleccion = self._db()[COLECCION_CORRECCIONES]
        if campo is None:
            coleccion.delete_one({"_id": file_id})
            return None
        actual = coleccion.find_one({"_id": file_id}, {"campos": 1})
        if actual is None:
            return None
        if set(actual.get("campos", {})) <= {campo}:
            # O era el unico campo, o ese campo no estaba: en los dos casos lo
            # que queda por enseñar es nada, y el documento entero sobra.
            coleccion.delete_one({"_id": file_id})
            return None
        coleccion.update_one({"_id": file_id}, {"$unset": {f"campos.{campo}": ""}})
        return a_json(coleccion.find_one({"_id": file_id}, PROYECCION_CORRECCION))

    async def borrar_correcciones(self, file_id: str, campo: str | None = None) -> dict | None:
        """Quita las correcciones de una factura, o solo las de un campo."""
        try:
            return await asyncio.to_thread(self._borrar_correcciones, file_id, campo)
        except MongoNoDisponible:
            raise
        except Exception as exc:
            raise MongoNoDisponible(sanear(str(exc), self.uri)) from None
