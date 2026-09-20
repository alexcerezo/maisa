"""Cola de revision: la anotacion de la segunda lectura enganchada a la API.

El sidecar (`outcomes_cola.jsonl`) es **opcional**: sin el, el comportamiento es
el de siempre. Estas pruebas fijan las tres cosas que importan: que la anotacion
llegue al listado y al detalle, que un sidecar ilegible no tumbe la API y que la
firma del listado (ETag) cambie cuando cambia el sidecar, porque el listado lo
expone.

La anotacion se sirve como `segunda_lectura` y **no** como `revision`: ese
nombre ya lo ocupa el estado de revision humana que guarda Mongo. La ultima
prueba del fichero fija justamente que los dos conviven sin pisarse.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.traza import carga_cola, segunda_lectura_resumen

from .conftest import construir_settings

ESCALADA = "2026-02-01_P002.pdf"
PAGADA = "2026-01-08_P001.pdf"

EVIDENCIA = {
    "confirmable": True,
    "desvio": False,
    "campos": {"pedido": ["PO-2026-0200"], "nif": ["B46102332"]},
    "motivos": ["la segunda lectura aporta pedido=PO-2026-0200 y NIF=B46102332"],
}


def escribe_cola(directorio: Path, filas: list[dict]) -> Path:
    ruta = directorio / "outcomes_cola.jsonl"
    with ruta.open("w", encoding="utf-8") as fichero:
        for fila in filas:
            fichero.write(json.dumps(fila, ensure_ascii=False) + "\n")
    return ruta


COLA_COMPLETA = [
    {"file_id": ESCALADA, "result": "ESCALAR", "segunda_lectura": EVIDENCIA},
    {
        "file_id": PAGADA,
        "result": "PAGAR",
        "segunda_lectura": {
            "confirmable": False,
            "desvio": True,
            "campos": {},
            "motivos": ["IBAN ajeno"],
        },
    },
]


@pytest.fixture
def con_cola(outputs_dir: Path) -> Path:
    """Sidecar con una escalada confirmable y un desvio de pago."""
    return escribe_cola(outputs_dir, COLA_COMPLETA)


def _cola_completa(directorio: Path) -> Path:
    return escribe_cola(directorio, COLA_COMPLETA)


def _cola_minima(directorio: Path) -> Path:
    return escribe_cola(directorio, [{"file_id": ESCALADA, "segunda_lectura": EVIDENCIA}])


def _cliente(settings) -> TestClient:
    return TestClient(create_app(settings))


# --------------------------------------------------------------------------- #
# Lectura del sidecar
# --------------------------------------------------------------------------- #
def test_carga_cola_indexa_por_file_id(tmp_path: Path):
    ruta = escribe_cola(tmp_path, [{"file_id": "a.pdf", "segunda_lectura": EVIDENCIA}])
    cola = carga_cola(ruta)
    assert cola == {"a.pdf": EVIDENCIA}


def test_carga_cola_sin_fichero_es_vacio(tmp_path: Path):
    assert carga_cola(tmp_path / "no-existe.jsonl") == {}


def test_carga_cola_ignora_lineas_rotas_y_sin_file_id(tmp_path: Path):
    ruta = tmp_path / "outcomes_cola.jsonl"
    ruta.write_text(
        "\n".join(
            [
                "{ esto no es json",
                json.dumps({"segunda_lectura": EVIDENCIA}),
                json.dumps({"file_id": "b.pdf", "segunda_lectura": "no-es-dict"}),
                json.dumps({"file_id": "c.pdf", "segunda_lectura": EVIDENCIA}),
            ]
        ),
        encoding="utf-8",
    )
    assert carga_cola(ruta) == {"c.pdf": EVIDENCIA}


def test_segunda_lectura_resumen_recorta_a_lo_que_usa_el_listado():
    assert segunda_lectura_resumen(None) is None
    assert segunda_lectura_resumen({}) is None
    assert segunda_lectura_resumen(EVIDENCIA) == {"confirmable": True, "desvio": False}


# --------------------------------------------------------------------------- #
# Sin sidecar: nada cambia
# --------------------------------------------------------------------------- #
def test_sin_sidecar_no_hay_anotacion(settings):
    with _cliente(settings) as cliente:
        listado = cliente.get("/api/facturas").json()
        assert all(fila["segunda_lectura"] is None for fila in listado["items"])
        detalle = cliente.get(f"/api/facturas/{ESCALADA}").json()
        assert detalle["segunda_lectura"] is None


def test_sin_sidecar_la_firma_es_la_de_la_traza(settings):
    with _cliente(settings) as cliente:
        firma_sin = cliente.get("/api/facturas").headers["etag"]
    # Con el fichero de cola ausente, la firma no cambia al recargar.
    with _cliente(settings) as cliente:
        assert cliente.get("/api/facturas").headers["etag"] == firma_sin


# --------------------------------------------------------------------------- #
# Con sidecar: la anotacion llega al listado y al detalle
# --------------------------------------------------------------------------- #
def test_listado_expone_la_anotacion(settings, con_cola):
    with _cliente(settings) as cliente:
        filas = {fila["file_id"]: fila for fila in cliente.get("/api/facturas").json()["items"]}
    assert filas[ESCALADA]["segunda_lectura"] == {"confirmable": True, "desvio": False}
    assert filas[PAGADA]["segunda_lectura"] == {"confirmable": False, "desvio": True}


def test_detalle_lleva_la_evidencia_completa(settings, con_cola):
    with _cliente(settings) as cliente:
        detalle = cliente.get(f"/api/facturas/{ESCALADA}").json()
    assert detalle["segunda_lectura"] == EVIDENCIA
    assert detalle["resultado"] == "ESCALAR"
    assert detalle["segunda_lectura"]["campos"]["pedido"] == ["PO-2026-0200"]


def test_la_cola_no_cambia_la_decision(settings, con_cola):
    """El sidecar anota; no toca `resultado` ni la entrega oficial."""
    with _cliente(settings) as cliente:
        filas = {fila["file_id"]: fila for fila in cliente.get("/api/facturas").json()["items"]}
    assert filas[ESCALADA]["resultado"] == "ESCALAR"
    assert filas[PAGADA]["resultado"] == "PAGAR"


def test_estadisticas_cuentan_la_cola(settings, con_cola):
    with _cliente(settings) as cliente:
        datos = cliente.get("/api/estadisticas").json()
    assert datos["cola_segunda_lectura"] == {
        "anotadas": 2,
        "confirmables": 1,
        "desvios": 1,
        "con_evidencia": 0,
    }
    assert datos["por_resultado"]["ESCALAR"] == 1


def test_estadisticas_sin_cola_van_a_cero(settings):
    with _cliente(settings) as cliente:
        datos = cliente.get("/api/estadisticas").json()
    assert datos["cola_segunda_lectura"] == {
        "anotadas": 0,
        "confirmables": 0,
        "desvios": 0,
        "con_evidencia": 0,
    }


def test_meta_dice_si_hay_cola(settings, con_cola):
    with _cliente(settings) as cliente:
        assert cliente.get("/api/meta").json()["configuracion"]["datos"]["cola_existe"] is True


def test_meta_sin_cola(settings):
    with _cliente(settings) as cliente:
        assert cliente.get("/api/meta").json()["configuracion"]["datos"]["cola_existe"] is False


# --------------------------------------------------------------------------- #
# La firma del listado incluye el sidecar
# --------------------------------------------------------------------------- #
def test_la_firma_cambia_al_cambiar_el_sidecar(outputs_dir: Path, facturas_dir, ui_dir):
    settings = construir_settings(outputs_dir, facturas_dir, ui_dir)
    with _cliente(settings) as cliente:
        etag_sin_cola = cliente.get("/api/facturas").headers["etag"]

    _cola_completa(outputs_dir)
    with _cliente(settings) as cliente:
        etag_con_cola = cliente.get("/api/facturas").headers["etag"]

    assert etag_con_cola != etag_sin_cola


def test_el_sidecar_invalida_la_cache_del_listado(outputs_dir: Path, facturas_dir, ui_dir):
    """Reescribir la cola con el mismo cliente devuelve un ETag nuevo."""
    _cola_completa(outputs_dir)
    settings = construir_settings(outputs_dir, facturas_dir, ui_dir)
    with _cliente(settings) as cliente:
        primero = cliente.get("/api/facturas")
        assert primero.status_code == 200

        _cola_minima(outputs_dir)
        segundo = cliente.get(
            "/api/facturas", headers={"If-None-Match": primero.headers["etag"]}
        )

    assert segundo.status_code == 200
    assert segundo.headers["etag"] != primero.headers["etag"]


# --------------------------------------------------------------------------- #
# La anotacion de la maquina y la revision humana no se pisan
# --------------------------------------------------------------------------- #
def test_segunda_lectura_y_revision_humana_conviven(con_cola, cliente_con_fakes):
    """`segunda_lectura` (maquina) y `revision` (persona) viajan juntos."""
    file_id = ESCALADA
    marcada = cliente_con_fakes.put(
        f"/api/facturas/{file_id}/revision",
        json={"estado": "PENDIENTE", "revisor": "ana"},
    )
    assert marcada.status_code == 200

    detalle = cliente_con_fakes.get(f"/api/facturas/{file_id}").json()
    assert detalle["segunda_lectura"]["confirmable"] is True
    assert detalle["revision"]["estado"] == "PENDIENTE"
    assert detalle["revision"]["revisor"] == "ana"

    fila = next(
        fila
        for fila in cliente_con_fakes.get("/api/facturas").json()["items"]
        if fila["file_id"] == file_id
    )
    assert fila["segunda_lectura"] == {"confirmable": True, "desvio": False}
