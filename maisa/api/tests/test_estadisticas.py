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
    assert datos["entrega"] == {
        "total": 4,
        "lineas_invalidas": 0,
        "coincide_con_traza": True,
        "lotes": {
            "1": {"traza": 2, "entrega": 2, "entregado": True},
            "2": {"traza": 2, "entrega": 2, "entregado": True},
        },
        "faltan_en_traza": [],
    }
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


def test_traza_encadenada_da_una_fila_por_factura(tmp_path):
    """La traza de produccion es un diario (`lote`/`lectura`/`decision`/`fin`).

    Leerla como si fuera la forma plana daba una fila por **evento** y, como
    `setdefault` se queda con la primera, la `lectura` —que no trae `result`—
    ganaba: 1084 filas con `resultado` a `null` en vez de 540 con decision.
    """
    ruta = tmp_path / "traza.jsonl"
    eventos = [
        {"seq": 0, "tipo": "lote", "file_id": "*", "datos": {"facturas": 1}},
        {
            "seq": 1,
            "tipo": "lectura",
            "file_id": "a.pdf",
            "datos": {"escalon": "capa_texto", "calidad": 1.0, "segundos": 0.25},
        },
        {
            "seq": 2,
            "tipo": "decision",
            "file_id": "a.pdf",
            "datos": {
                "result": "PAGAR",
                "campos": {"proveedor": "Suministros Levante S.L.", "total": "3012.89"},
                "escalon_lectura": "capa_texto",
                "calidad_lectura": 1.0,
                "lote": 1,
            },
        },
        {"seq": 3, "tipo": "fin", "file_id": "*", "datos": {"resultados": {"PAGAR": 1}}},
    ]
    ruta.write_text(
        "".join(json.dumps(evento, ensure_ascii=False) + "\n" for evento in eventos),
        encoding="utf-8",
    )
    traza = TrazaStore(ruta)
    assert traza.total() == 1
    # Los eventos que no deciden nada no son lineas invalidas: son la pasada.
    assert traza.lineas_invalidas() == 0
    fila = traza.obtener("a.pdf")
    assert fila is not None
    assert fila["result"] == "PAGAR"
    assert fila["campos"]["proveedor"] == "Suministros Levante S.L."
    assert fila["escalon_lectura"] == "capa_texto"
    assert fila["lote"] == 1
    assert traza.estadisticas()["por_resultado"]["PAGAR"] == 1


def test_traza_plana_sigue_leyendose_igual(tmp_path):
    """Sin `--traza-hash` la traza no lleva `tipo`, y tiene que seguir valiendo."""
    ruta = tmp_path / "traza.jsonl"
    ruta.write_text(
        json.dumps({"file_id": "a.pdf", "result": "NO_PAGAR", "campos": {}}) + "\n",
        encoding="utf-8",
    )
    traza = TrazaStore(ruta)
    assert traza.total() == 1
    assert traza.obtener("a.pdf")["result"] == "NO_PAGAR"


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


def test_coincide_con_traza_con_dos_lotes(tmp_path):
    """La traza lleva mas lotes que la entrega, y eso no es un descuadre.

    Es el caso real: la entrega (`outcomes.jsonl`) es del lote 1 y la traza cubre
    el lote 1 y el lote 2. Comparando los dos conjuntos enteros el resultado es
    `false` por construccion, asi que la bandera mentia todos los dias.
    """
    lote1 = tmp_path / "outcomes_traza.jsonl"
    lote2 = tmp_path / "outcomes_lote2_traza.jsonl"
    escribir = lambda ruta, ids, lote: ruta.write_text(
        "".join(json.dumps({"file_id": i, "lote": lote, "result": "PAGAR"}) + "\n" for i in ids),
        encoding="utf-8",
    )
    escribir(lote1, ["a.pdf", "b.pdf"], 1)
    escribir(lote2, ["c.pdf"], 2)
    traza = TrazaStore((lote1, lote2))

    # La entrega solo lleva el lote 1: coincide con su lote, y el panel puede
    # decir que el lote 2 esta por entregar en vez de dar un `false` opaco.
    cobertura = traza.cobertura_entrega({"a.pdf", "b.pdf"})
    assert cobertura["coincide"] is True
    assert cobertura["lotes"] == {
        "1": {"traza": 2, "entrega": 2, "entregado": True},
        "2": {"traza": 1, "entrega": 0, "entregado": False},
    }
    assert cobertura["faltan_en_traza"] == []

    # Con los dos lotes entregados tambien coincide, y ambos quedan entregados.
    cobertura = traza.cobertura_entrega({"a.pdf", "b.pdf", "c.pdf"})
    assert cobertura["coincide"] is True
    assert all(dato["entregado"] for dato in cobertura["lotes"].values())

    # Y un descuadre de verdad si se ve: falta una fila del lote 1.
    cobertura = traza.cobertura_entrega({"a.pdf"})
    assert cobertura["coincide"] is False
    assert cobertura["lotes"]["1"] == {"traza": 2, "entrega": 1, "entregado": False}

    # Un file_id entregado que no esta en la traza se nombra, no se cuenta.
    cobertura = traza.cobertura_entrega({"a.pdf", "b.pdf", "fantasma.pdf"})
    assert cobertura["coincide"] is False
    assert cobertura["faltan_en_traza"] == ["fantasma.pdf"]
