"""Asientos y snapshots contra un doble de Mongo (sin base de datos real)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.deps import get_mongo, get_ocr
from app.main import create_app
from app.mongo_repo import sanear

from .conftest import FakeMongo


def test_listado_de_asientos(cliente_con_fakes):
    respuesta = cliente_con_fakes.get("/api/asientos")
    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert datos["total"] == 3
    assert [fila["asiento_id"] for fila in datos["items"]] == ["AS-00096", "AS-00096", "AS-00476"]
    assert datos["items"][0]["pedido"] == "PO-2026-0096"
    assert datos["items"][0]["importe"] == 3012.89


def test_filtro_vigente_y_busqueda(cliente_con_fakes):
    vigentes = cliente_con_fakes.get("/api/asientos", params={"vigente": True}).json()
    assert vigentes["total"] == 2
    assert all(fila["vigente"] is True for fila in vigentes["items"])

    assert cliente_con_fakes.get("/api/asientos", params={"vigente": False}).json()["total"] == 1
    assert cliente_con_fakes.get("/api/asientos", params={"q": "AS-00476"}).json()["total"] == 1
    assert cliente_con_fakes.get("/api/asientos", params={"q": "b46102333"}).json()["total"] == 1
    assert cliente_con_fakes.get("/api/asientos", params={"q": "PO-2026-0096"}).json()["total"] == 2
    assert cliente_con_fakes.get("/api/asientos", params={"q": "nada"}).json()["total"] == 0


def test_paginacion_de_asientos(cliente_con_fakes):
    datos = cliente_con_fakes.get("/api/asientos", params={"limit": 1, "offset": 1}).json()
    assert datos["total"] == 3
    assert datos["devueltas"] == 1
    assert datos["items"][0]["_id"] == "snap-2025-12-01#AS-00096"


def test_detalle_de_asiento(cliente_con_fakes):
    respuesta = cliente_con_fakes.get("/api/asientos/AS-00096")
    assert respuesta.status_code == 200
    assert respuesta.json()["nif"] == "B46102331"


def test_detalle_de_asiento_inexistente(cliente_con_fakes):
    respuesta = cliente_con_fakes.get("/api/asientos/AS-99999")
    assert respuesta.status_code == 404
    assert respuesta.json()["error"]["codigo"] == "asiento_no_encontrado"


def test_asiento_con_nombre_invalido(cliente_con_fakes):
    assert cliente_con_fakes.get("/api/asientos/AS 00096").status_code == 422
    assert cliente_con_fakes.get("/api/asientos/" + "A" * 40).status_code == 422


def test_snapshots_ordenados_por_fecha(cliente_con_fakes):
    datos = cliente_con_fakes.get("/api/snapshots").json()
    assert datos["total"] == 2
    assert [fila["_id"] for fila in datos["items"]] == ["snap-2026-01-01", "snap-2025-12-01"]
    assert datos["items"][0]["vigente"] is True
    assert datos["items"][0]["estado"] == "COMPLETO"


def test_mongo_caido_devuelve_503_y_no_500(settings, fake_ocr):
    app = create_app(settings)
    app.dependency_overrides[get_mongo] = lambda: FakeMongo(error="no hay conexion")
    app.dependency_overrides[get_ocr] = lambda: fake_ocr
    with TestClient(app) as cliente:
        for ruta in ("/api/asientos", "/api/asientos/AS-00096", "/api/snapshots"):
            respuesta = cliente.get(ruta)
            assert respuesta.status_code == 503, ruta
            assert respuesta.json()["error"]["codigo"] == "mongo_no_disponible"

        salud = cliente.get("/health")
        assert salud.status_code == 200
        assert salud.json()["dependencias"]["mongo"]["ok"] is False
    app.dependency_overrides.clear()


def test_sanear_quita_las_credenciales_del_mensaje():
    uri = "mongodb://albertitos_app:secreto123@mongo:27017/albertitos?replicaSet=rs0"
    mensaje = f"failed to connect to {uri}: timeout"
    saneado = sanear(mensaje, uri)
    assert "secreto123" not in saneado
    assert "albertitos_app" not in saneado
    assert "***@mongo:27017" in saneado
