"""Estadisticas del panel y lectura de la traza."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.deps import get_mongo, get_ocr
from app.main import create_app
from app.traza import TrazaStore, divisa_principal, motivo_principal

from .conftest import FakeMongo, FakeOcr


def test_estadisticas_con_mongo_disponible(cliente_con_fakes):
    datos = cliente_con_fakes.get("/api/estadisticas").json()
    assert datos["total"] == 4
    assert datos["por_resultado"] == {"PAGAR": 2, "NO_PAGAR": 1, "ESCALAR": 1}
    assert datos["por_lote"] == {"1": 2, "2": 2}
    assert datos["por_metodo_lectura"] == {"texto_determinista": 3, "vision_ocr": 1}
    assert datos["asientos_vigentes"] == 2
    assert datos["mongo"] == {"ok": True, "error": None}
    assert datos["entrega"] == {"total": 4, "lineas_invalidas": 0, "coincide_con_traza": True}
    # Una sola factura ESCALAR ("2026-02-01_P002.pdf") y ninguna revisada todavia.
    assert datos["pendientes_revision"] == 1


def test_pendientes_revision_baja_al_marcar_resuelta(cliente_con_fakes, fake_mongo):
    file_id = "2026-02-01_P002.pdf"
    assert cliente_con_fakes.get("/api/estadisticas").json()["pendientes_revision"] == 1

    respuesta = cliente_con_fakes.put(f"/api/facturas/{file_id}/revision", json={"estado": "RESUELTA"})
    assert respuesta.status_code == 200
    assert respuesta.json()["estado"] == "RESUELTA"

    assert cliente_con_fakes.get("/api/estadisticas").json()["pendientes_revision"] == 0


def test_pendientes_revision_null_si_mongo_cae(settings, fake_ocr):
    app = create_app(settings)
    app.dependency_overrides[get_mongo] = lambda: FakeMongo(error="no hay conexion")
    app.dependency_overrides[get_ocr] = lambda: fake_ocr
    with TestClient(app) as cliente:
        datos = cliente.get("/api/estadisticas").json()
    app.dependency_overrides.clear()

    assert datos["pendientes_revision"] is None


def test_estadisticas_sobreviven_a_mongo_caido(settings, fake_ocr):
    app = create_app(settings)
    app.dependency_overrides[get_mongo] = lambda: FakeMongo(error="no hay conexion")
    app.dependency_overrides[get_ocr] = lambda: fake_ocr
    with TestClient(app) as cliente:
        respuesta = cliente.get("/api/estadisticas")
    app.dependency_overrides.clear()

    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert datos["asientos_vigentes"] is None
    assert datos["mongo"]["ok"] is False
    assert datos["total"] == 4


def test_traza_ausente_da_503(settings):
    settings.traza_path.unlink()
    app = create_app(settings)
    app.dependency_overrides[get_ocr] = lambda: FakeOcr()
    with TestClient(app) as cliente:
        assert cliente.get("/api/facturas").status_code == 503
        assert cliente.get("/api/estadisticas").status_code == 503
        assert cliente.get("/api/facturas/2026-01-08_P001.pdf").status_code == 503
    app.dependency_overrides.clear()


def test_lineas_invalidas_se_ignoran(tmp_path):
    ruta = tmp_path / "traza.jsonl"
    ruta.write_text(
        json.dumps({"file_id": "ok.pdf", "result": "PAGAR"})
        + "\n{esto no es json}\n"
        + json.dumps({"result": "PAGAR"})
        + "\n\n",
        encoding="utf-8",
    )
    traza = TrazaStore(ruta)
    assert traza.total() == 1
    assert traza.lineas_invalidas() == 2
    assert traza.obtener("ok.pdf") is not None
    assert traza.obtener("otra.pdf") is None


def test_recarga_cuando_cambia_el_fichero(tmp_path):
    ruta = tmp_path / "traza.jsonl"
    ruta.write_text(json.dumps({"file_id": "a.pdf", "result": "PAGAR"}) + "\n", encoding="utf-8")
    traza = TrazaStore(ruta)
    assert traza.total() == 1

    ruta.write_text(
        json.dumps({"file_id": "a.pdf", "result": "PAGAR"})
        + "\n"
        + json.dumps({"file_id": "b.pdf", "result": "NO_PAGAR"})
        + "\n",
        encoding="utf-8",
    )
    assert traza.total() == 2
    assert traza.estadisticas()["por_resultado"]["NO_PAGAR"] == 1


def test_motivo_principal_prioriza_motivos_y_luego_hechos():
    assert motivo_principal({"motivos": ["motivo A"], "hechos": []}) == "motivo A"
    assert (
        motivo_principal(
            {
                "motivos": [],
                "hechos": [
                    {"ok": True, "motivo": "todo bien", "duro": True},
                    {"ok": False, "motivo": "fallo blando", "duro": False},
                    {"ok": False, "motivo": "fallo duro", "duro": True},
                ],
            }
        )
        == "fallo duro"
    )
    assert motivo_principal({"motivos": [], "hechos": []}) is None
    assert motivo_principal({"motivos": [""], "hechos": [{"ok": True}]}) is None


def test_divisa_principal_elige_la_que_no_es_la_del_erp():
    """De las divisas declaradas manda la que discrepa, porque es la que escala."""
    # Sin declarar nada se asume la del ERP: el silencio no es una duda.
    assert divisa_principal({}) == "EUR"
    assert divisa_principal({"divisa_documento": [], "divisa_erp": "EUR"}) == "EUR"
    # Declarar la del ERP tampoco cambia nada.
    assert divisa_principal({"divisa_documento": ["EUR"], "divisa_erp": "EUR"}) == "EUR"
    # La que discrepa es la que hay que enseñar, aunque venga detras.
    assert divisa_principal({"divisa_documento": ["EUR", "USD"], "divisa_erp": "EUR"}) == "USD"
    assert divisa_principal({"divisa_documento": ["USD"], "divisa_erp": "EUR"}) == "USD"
    # Si el ERP no fuese euros, la del documento seguiria siendo la que discrepa.
    assert divisa_principal({"divisa_documento": ["USD"], "divisa_erp": "GBP"}) == "USD"
    # Dos divisas raras y ninguna del ERP: la primera, que es el orden del motor.
    assert divisa_principal({"divisa_documento": ["USD", "GBP"], "divisa_erp": "EUR"}) == "USD"


def test_divisa_principal_normaliza_lo_que_viene_de_la_traza():
    """La traza es texto libre de un motor en Python: no se le supone la forma."""
    # Una sola divisa puede llegar como cadena en vez de como lista.
    assert divisa_principal({"divisa_documento": "USD"}) == "USD"
    # Y en minusculas o con espacios, que es como la escribe un OCR.
    assert divisa_principal({"divisa_documento": [" usd "], "divisa_erp": "eur"}) == "USD"
    # Una entrada vacia no es una divisa declarada.
    assert divisa_principal({"divisa_documento": ["", "  "], "divisa_erp": "EUR"}) == "EUR"
