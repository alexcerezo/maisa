"""Borrado de correcciones contra un doble que **valida como Mongo**.

Este fichero existe por un fallo que los tests de endpoints no podian ver. El
validador de la coleccion `correcciones` (ver
`docker/mongosh/02-schema-init.js`) exige `minProperties: 1` en `campos` y esta
en `validationAction: "error"`. Mongo valida el documento **resultante de cada
escritura**, no solo el estado final.

`_borrar_correcciones` quitaba el campo con `$unset` y borraba el documento
despues si se habia quedado vacio. Al quitar el **ultimo** campo, el `$unset`
dejaba `campos: {}` un instante, Mongo rechazaba esa escritura con un error de
validacion y la limpieza posterior no llegaba a ejecutarse: deshacer la ultima
correccion de una factura fallaba siempre, con un 503 que parecia "Mongo no
responde".

El doble `FakeMongo` de `conftest.py` no puede detectarlo: solo ve el estado
final, que era correcto. Por eso aqui se usa un doble distinto que **rechaza
cualquier escritura que deje `campos` vacio**, igual que Mongo.
"""

from __future__ import annotations

import pytest

from app.mongo_repo import COLECCION_CORRECCIONES, MongoRepo

FACTURA = "2026-02-01_P002.pdf"


class DocumentoInvalido(Exception):
    """Lo que Mongo lanza: `Document failed validation`."""


class ColeccionConValidador:
    """Doble minimo de `Collection` que aplica el validador en cada escritura."""

    def __init__(self, documentos: dict[str, dict] | None = None) -> None:
        self.documentos = documentos if documentos is not None else {}
        self.escrituras: list[str] = []

    def _valida(self, doc: dict) -> None:
        campos = doc.get("campos")
        if not isinstance(campos, dict) or not campos:
            raise DocumentoInvalido(
                "Document failed validation: 'campos' must have at least 1 property"
            )

    def _aplica(self, filtro: dict, doc: dict) -> None:
        self._valida(doc)
        self.documentos[filtro["_id"]] = doc

    def find_one(self, filtro: dict, proyeccion: dict | None = None):
        doc = self.documentos.get(filtro["_id"])
        if doc is None:
            return None
        if not proyeccion:
            return dict(doc)
        devuelto = {"_id": doc["_id"]}
        for clave, incluir in proyeccion.items():
            if incluir and clave in doc:
                devuelto[clave] = doc[clave]
        return devuelto

    def update_one(self, filtro: dict, operacion: dict):
        self.escrituras.append("update")
        doc = dict(self.documentos.get(filtro["_id"], {"_id": filtro["_id"], "campos": {}}))
        for ruta, valor in operacion.get("$set", {}).items():
            self._pon(doc, ruta, valor)
        for ruta in operacion.get("$unset", {}):
            self._quita(doc, ruta)
        # La validacion va al final del `update_one`, como en Mongo: el
        # documento intermedio nunca llega a existir si no pasa el validador.
        self._aplica(filtro, doc)

    def delete_one(self, filtro: dict):
        self.escrituras.append("delete")
        self.documentos.pop(filtro["_id"], None)

    def count_documents(self, filtro: dict) -> int:
        """Solo se usa en las comprobaciones posteriores al borrado.

        Existe para que, si alguien vuelve a la version que limpiaba despues del
        `$unset`, el fallo del test sea `DocumentoInvalido` (el error real de
        Mongo) y no un `AttributeError` por un metodo que falta.
        """
        doc = self.documentos.get(filtro["_id"])
        if doc is None:
            return 0
        if filtro.get("campos") == {"$ne": {}}:
            return 1 if doc.get("campos") else 0
        return 1

    @staticmethod
    def _pon(doc: dict, ruta: str, valor) -> None:
        partes = ruta.split(".")
        actual = doc
        for parte in partes[:-1]:
            actual = actual.setdefault(parte, {})
        actual[partes[-1]] = valor

    @staticmethod
    def _quita(doc: dict, ruta: str) -> None:
        partes = ruta.split(".")
        actual = doc
        for parte in partes[:-1]:
            actual = actual.get(parte, {})
        actual.pop(partes[-1], None)


class BaseConValidador:
    def __init__(self, coleccion: ColeccionConValidador) -> None:
        self.coleccion = coleccion

    def __getitem__(self, nombre: str) -> ColeccionConValidador:
        assert nombre == COLECCION_CORRECCIONES
        return self.coleccion


def _repo_con(coleccion: ColeccionConValidador) -> MongoRepo:
    repo = MongoRepo(uri="mongodb://127.0.0.1:1/albertitos", db="albertitos")
    repo._db = lambda: BaseConValidador(coleccion)  # type: ignore[method-assign]
    return repo


def _correccion(valor: str) -> dict:
    return {
        "valor": valor,
        "nota": None,
        "autor": "albertito",
        "actualizado_en": "2026-01-01T00:00:00+00:00",
    }


def _con_dos_campos() -> ColeccionConValidador:
    return ColeccionConValidador(
        {FACTURA: {"_id": FACTURA, "campos": {"nif": _correccion("B90233418"), "iban": _correccion("ES44")}}}
    )


# --------------------------------------------------------------------------- #
# Quitar el ultimo campo: el caso que fallaba
# --------------------------------------------------------------------------- #
def test_quitar_el_ultimo_campo_borra_el_documento():
    coleccion = ColeccionConValidador(
        {FACTURA: {"_id": FACTURA, "campos": {"nif": _correccion("B90233418")}}}
    )
    assert _repo_con(coleccion)._borrar_correcciones(FACTURA, "nif") is None
    assert FACTURA not in coleccion.documentos


def test_quitar_el_ultimo_campo_no_pasa_por_un_estado_invalido():
    """Lo que de verdad se comprueba: no se llega a escribir `campos: {}`.

    Si el repositorio vuelve a intentar el `$unset` y limpiar despues, el doble
    lanza `DocumentoInvalido` en mitad del `update_one` y este test falla.
    """
    coleccion = ColeccionConValidador(
        {FACTURA: {"_id": FACTURA, "campos": {"nif": _correccion("B90233418")}}}
    )
    _repo_con(coleccion)._borrar_correcciones(FACTURA, "nif")
    assert "update" not in coleccion.escrituras
    assert coleccion.escrituras == ["delete"]


# --------------------------------------------------------------------------- #
# Los demas casos del borrado
# --------------------------------------------------------------------------- #
def test_quitar_un_campo_conserva_los_demas():
    coleccion = _con_dos_campos()
    devuelto = _repo_con(coleccion)._borrar_correcciones(FACTURA, "nif")
    assert set(devuelto["campos"]) == {"iban"}


def test_quitar_todas_borra_el_documento():
    coleccion = _con_dos_campos()
    assert _repo_con(coleccion)._borrar_correcciones(FACTURA, None) is None
    assert FACTURA not in coleccion.documentos


def test_quitar_un_campo_que_no_esta_no_toca_nada():
    """Es el caso de un `DELETE` repetido: no puede reventar ni borrar de mas."""
    coleccion = _con_dos_campos()
    devuelto = _repo_con(coleccion)._borrar_correcciones(FACTURA, "pedido")
    assert set(devuelto["campos"]) == {"nif", "iban"}


def test_quitar_de_una_factura_sin_correcciones_devuelve_none():
    coleccion = ColeccionConValidador()
    assert _repo_con(coleccion)._borrar_correcciones(FACTURA, "nif") is None
    assert coleccion.escrituras == []


def test_quitar_un_campo_de_una_factura_que_solo_tenia_ese():
    """El caso real del panel: se corrige un campo, y luego se deshace."""
    coleccion = ColeccionConValidador(
        {FACTURA: {"_id": FACTURA, "campos": {"iban": _correccion("ES44")}}}
    )
    assert _repo_con(coleccion)._borrar_correcciones(FACTURA, "iban") is None
    assert FACTURA not in coleccion.documentos


@pytest.mark.parametrize("campo", ["nif", "iban"])
def test_quitar_cualquiera_de_los_dos_deja_el_otro(campo):
    coleccion = _con_dos_campos()
    devuelto = _repo_con(coleccion)._borrar_correcciones(FACTURA, campo)
    assert set(devuelto["campos"]) == ({"nif", "iban"} - {campo})
