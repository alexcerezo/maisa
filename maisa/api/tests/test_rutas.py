"""Inventario de rutas: la API debe exponer todo lo pactado."""

from __future__ import annotations

RUTAS_ESPERADAS = {
    ("get", "/health"),
    ("get", "/health/ready"),
    ("get", "/api/facturas"),
    ("get", "/api/facturas/{file_id}"),
    ("get", "/api/facturas/{file_id}/pdf"),
    ("get", "/api/asientos"),
    ("get", "/api/asientos/{asiento_id}"),
    ("get", "/api/snapshots"),
    ("get", "/api/estadisticas"),
    ("get", "/api/meta"),
    ("post", "/api/ocr"),
}


def test_todas_las_rutas_estan_publicadas(client):
    esquema = client.get("/openapi.json").json()
    publicadas = {(metodo, ruta) for ruta, metodos in esquema["paths"].items() for metodo in metodos}
    faltan = RUTAS_ESPERADAS - publicadas
    assert not faltan, f"faltan rutas: {sorted(faltan)}"


def test_openapi_no_exige_api_key(client_con_api_key):
    assert client_con_api_key.get("/openapi.json").status_code == 200
