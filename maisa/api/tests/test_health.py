"""Salud: nunca revienta, siempre detalla, y /health/ready decide el 503."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def test_health_con_dependencias_caidas_detalla_sin_romper(client):
    respuesta = client.get("/health")
    assert respuesta.status_code == 200
    datos = respuesta.json()

    assert datos["estado"] == "error"
    assert set(datos["dependencias"]) == {"mongo", "ocr", "escritura"}
    for nombre, dependencia in datos["dependencias"].items():
        assert dependencia["ok"] is False, nombre
        assert dependencia["error"], nombre
        assert dependencia["latencia_ms"] >= 0
    # `escritura` comparte el cliente de Mongo, pero NO es critica: un fallo
    # suyo no impide servir datos.
    assert sorted(datos["criticas_caidas"]) == ["mongo", "ocr"]
    # La traza es local: se lee aunque las dependencias externas esten caidas.
    assert datos["datos"]["traza"] == {"ok": True, "facturas": 4, "lineas_invalidas": 0}


def test_health_ready_devuelve_503_si_falla_algo_critico(client):
    respuesta = client.get("/health/ready")
    assert respuesta.status_code == 503
    datos = respuesta.json()
    assert datos["listo"] is False
    assert sorted(datos["criticas_caidas"]) == ["mongo", "ocr"]
    assert datos["dependencias"]["mongo"]["ok"] is False


def test_health_ok_con_dependencias_sanas(cliente_con_fakes):
    respuesta = cliente_con_fakes.get("/health")
    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert datos["estado"] == "ok"
    assert datos["criticas_caidas"] == []
    assert datos["dependencias"]["mongo"]["ok"] is True
    assert datos["dependencias"]["ocr"]["ok"] is True
    assert datos["dependencias"]["escritura"]["ok"] is True
    assert datos["dependencias"]["escritura"]["expedientes"] == 0

    listo = cliente_con_fakes.get("/health/ready")
    assert listo.status_code == 200
    assert listo.json()["listo"] is True


def test_health_sigue_respondiendo_sin_fichero_de_traza(settings):
    settings.traza_path.unlink()
    with TestClient(create_app(settings)) as cliente:
        respuesta = cliente.get("/health")
    assert respuesta.status_code == 200
    assert respuesta.json()["datos"]["traza"]["ok"] is False


def test_health_y_docs_no_exigen_api_key(client_con_api_key):
    assert client_con_api_key.get("/health").status_code == 200
    assert client_con_api_key.get("/health/ready").status_code in (200, 503)
    assert client_con_api_key.get("/docs").status_code == 200
    assert client_con_api_key.get("/openapi.json").status_code == 200


def test_raiz_informativa_cuando_el_visor_esta_vacio(client):
    respuesta = client.get("/")
    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert "UI_DIR" in datos["mensaje"] or "visor" in datos["mensaje"]
    assert datos["api"] == "/api/meta"


def test_raiz_sirve_el_visor_sin_reiniciar(cliente_con_fakes, ui_dir):
    """Dejar el `index.html` en `UI_DIR` activa el visor sin recrear la app.

    La decision se toma en cada peticion: antes se tomaba al arrancar y hacia
    falta reiniciar el contenedor (y al reves: borrarlo dejaba un 404).
    """
    assert cliente_con_fakes.get("/").headers["content-type"].startswith("application/json")

    indice = ui_dir / "index.html"
    indice.write_text("<!doctype html><html><body>visor</body></html>")

    respuesta = cliente_con_fakes.get("/")
    assert respuesta.status_code == 200
    assert respuesta.headers["content-type"].startswith("text/html")
    assert "visor" in respuesta.text

    # Y al reves: quitarlo vuelve al mensaje informativo, no a un 404.
    indice.unlink()
    vuelta = cliente_con_fakes.get("/")
    assert vuelta.status_code == 200
    assert vuelta.headers["content-type"].startswith("application/json")


def test_meta_refleja_si_hay_visor(cliente_con_fakes, ui_dir):
    """`ui.disponible` tiene que coincidir con lo que sirve `GET /`."""
    ui = cliente_con_fakes.get("/api/meta").json()["configuracion"]["ui"]
    assert ui["disponible"] is False

    (ui_dir / "index.html").write_text("<!doctype html><html></html>")

    ui = cliente_con_fakes.get("/api/meta").json()["configuracion"]["ui"]
    assert ui["disponible"] is True
    assert ui["index_html"].endswith("index.html")
