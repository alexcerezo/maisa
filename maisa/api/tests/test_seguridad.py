"""API key opcional y CORS cerrado por defecto."""

from __future__ import annotations

import dataclasses
import json

from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import API_KEY, construir_settings


def test_modo_abierto_sin_api_key(client):
    respuesta = client.get("/api/meta")
    assert respuesta.status_code == 200
    assert respuesta.json()["modo_abierto"] is True
    assert respuesta.json()["configuracion"]["api"]["api_key_requerida"] is False


def test_api_key_obligatoria_cuando_esta_configurada(client_con_api_key):
    sin_cabecera = client_con_api_key.get("/api/facturas")
    assert sin_cabecera.status_code == 401
    assert sin_cabecera.json()["error"]["codigo"] == "no_autorizado"

    erronea = client_con_api_key.get("/api/facturas", headers={"X-API-Key": "otra-clave"})
    assert erronea.status_code == 401

    correcta = client_con_api_key.get("/api/facturas", headers={"X-API-Key": API_KEY})
    assert correcta.status_code == 200
    assert correcta.json()["total"] == 4

    assert client_con_api_key.post("/api/ocr", headers={"X-API-Key": API_KEY}).status_code == 422


def test_api_key_no_se_exige_sin_configurar(client):
    assert client.get("/api/facturas", headers={"X-API-Key": "lo-que-sea"}).status_code == 200
    assert client.get("/api/facturas").status_code == 200


def test_meta_no_filtra_credenciales_de_mongo(outputs_dir, facturas_dir, ui_dir):
    settings = construir_settings(
        outputs_dir,
        facturas_dir,
        ui_dir,
        mongo_uri="mongodb://albertitos_app:super-secreta@mongo:27017/albertitos?replicaSet=rs0",
    )
    with TestClient(create_app(settings)) as cliente:
        respuesta = cliente.get("/api/meta")

    assert respuesta.status_code == 200
    cuerpo = json.dumps(respuesta.json())
    assert "super-secreta" not in cuerpo
    assert "albertitos_app:" not in cuerpo
    assert respuesta.json()["configuracion"]["mongo"]["uri_sanitizada"].startswith("mongodb://***@mongo:27017")


def test_meta_expone_version_y_versiones_del_motor(client):
    datos = client.get("/api/meta").json()
    assert datos["api_version"] == "1.0.0"
    assert datos["motor"]["facturas_en_traza"] == 4
    assert datos["motor"]["versiones_norma"] == {"norma_v3.0": 1, "norma_v3.1": 3}
    assert datos["configuracion"]["mongo"]["modo"] == "solo lectura"
    assert datos["configuracion"]["ocr"]["url"] == "http://127.0.0.1:1"
    assert datos["configuracion"]["api"]["cors_origins"] == [
        "http://localhost:8010",
        "http://127.0.0.1:8010",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


def test_cors_permite_origen_local_y_rechaza_otro(client):
    permitido = client.get("/api/facturas", headers={"Origin": "http://localhost:8010"})
    assert permitido.headers.get("access-control-allow-origin") == "http://localhost:8010"

    ajeno = client.get("/api/facturas", headers={"Origin": "http://malicioso.example"})
    assert "access-control-allow-origin" not in ajeno.headers


def test_cors_configurable_por_entorno(outputs_dir, facturas_dir, ui_dir):
    settings = construir_settings(outputs_dir, facturas_dir, ui_dir)
    settings = dataclasses.replace(settings, cors_origins=("http://10.0.0.5:3000",))
    with TestClient(create_app(settings)) as cliente:
        respuesta = cliente.get("/api/facturas", headers={"Origin": "http://10.0.0.5:3000"})
    assert respuesta.headers.get("access-control-allow-origin") == "http://10.0.0.5:3000"
