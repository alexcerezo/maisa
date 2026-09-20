"""El visor estatico: `/` y las rutas del panel que no son ficheros.

El panel es una SPA: react-router resuelve `/facturas` o `/escalabilidad` **en
el navegador**, asi que esas rutas no existen en el disco del servidor. Si la
API contesta con su 404 de fichero, abrir un enlace directo o recargar (F5)
sobre cualquiera de ellas se rompe, aunque el visor cargue bien en `/` y sus
enlaces internos naveguen. Aqui se fija ese contrato, y el contrario: la
superficie de la API sigue devolviendo JSON cuando no encuentra algo, para que
un error no se disfrace de pagina.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import construir_settings

INDICE = '<!doctype html><html><body><div id="root"></div></body></html>'


@pytest.fixture
def ui_dir_construido(tmp_path):
    """Un `dist/` minimo: el `index.html` que deja Vite y un fichero suelto."""
    directorio = tmp_path / "ui"
    (directorio / "data").mkdir(parents=True)
    (directorio / "index.html").write_text(INDICE, encoding="utf-8")
    (directorio / "data" / "escalabilidad.json").write_text('{"version": 1}', encoding="utf-8")
    return directorio


@pytest.fixture
def cliente_visor(outputs_dir, facturas_dir, ui_dir_construido):
    settings = construir_settings(outputs_dir, facturas_dir, ui_dir_construido)
    with TestClient(create_app(settings)) as cliente:
        yield cliente


def test_raiz_sirve_el_visor(cliente_visor):
    respuesta = cliente_visor.get("/")

    assert respuesta.status_code == 200
    assert respuesta.headers["content-type"].startswith("text/html")
    assert 'id="root"' in respuesta.text


def test_un_fichero_del_visor_se_sirve_tal_cual(cliente_visor):
    respuesta = cliente_visor.get("/data/escalabilidad.json")

    assert respuesta.status_code == 200
    assert respuesta.json() == {"version": 1}


@pytest.mark.parametrize(
    "ruta",
    [
        "/facturas",
        "/escalabilidad",
        "/trazabilidad",
        # El id del expediente lleva extension y aun asi es una ruta del panel:
        # por eso la caida NO se puede decidir mirando si la ruta tiene punto.
        "/facturas/2026-01-08_P001.pdf",
    ],
)
def test_una_ruta_del_panel_devuelve_el_indice(cliente_visor, ruta):
    respuesta = cliente_visor.get(ruta)

    assert respuesta.status_code == 200
    assert respuesta.headers["content-type"].startswith("text/html")
    assert 'id="root"' in respuesta.text


@pytest.mark.parametrize("ruta", ["/api/nope", "/api/facturas/nope/no-existe", "/health/nope"])
def test_la_api_conserva_su_404_json(cliente_visor, ruta):
    respuesta = cliente_visor.get(ruta)

    assert respuesta.status_code == 404
    assert respuesta.headers["content-type"].startswith("application/json")
    assert respuesta.json() == {"detail": "Not Found"}


def test_sin_build_una_ruta_del_panel_sigue_siendo_404(outputs_dir, facturas_dir, ui_dir):
    # `ui_dir` es el fixture del conftest: el directorio existe pero no hay
    # `index.html`, asi que no hay pagina que devolver y el 404 es lo honesto.
    settings = construir_settings(outputs_dir, facturas_dir, ui_dir)
    with TestClient(create_app(settings)) as cliente:
        assert cliente.get("/escalabilidad").status_code == 404
        # En `/` si hay algo que decir: el mensaje de "construye el panel".
        raiz = cliente.get("/")
        assert raiz.status_code == 200
        assert raiz.json()["servicio"] == "albertitos-api"
