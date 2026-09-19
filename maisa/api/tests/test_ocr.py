"""Proxy del OCR: contrato de respuesta, tope de tamano y propagacion de errores."""

from __future__ import annotations

import io

from fastapi.testclient import TestClient

from app.deps import get_mongo, get_ocr
from app.errors import ApiError
from app.main import create_app

from .conftest import FakeMongo, FakeOcr


def _fichero(contenido: bytes = b"%PDF-1.4 factura", nombre: str = "factura.pdf"):
    return {"file": (nombre, io.BytesIO(contenido), "application/pdf")}


def test_ocr_resumen_por_defecto(cliente_con_fakes, fake_ocr):
    respuesta = cliente_con_fakes.post("/api/ocr", files=_fichero(), params={"engine": "local"})
    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert datos["texto"].startswith("FACTURA 123")
    assert datos["score"] == 0.93
    assert datos["motor"] == "local"
    assert datos["segundos_proxy"] >= 0
    assert datos["bytes_enviados"] == len(b"%PDF-1.4 factura")
    assert "bruto" not in datos  # en modo resumen no se reenvia el payload entero

    llamada = fake_ocr.llamadas[-1]
    assert llamada["engine"] == "local"
    assert llamada["detalle"] is False


def test_ocr_con_detalle_conserva_el_payload_completo(cliente_con_fakes, fake_ocr):
    respuesta = cliente_con_fakes.post("/api/ocr", files=_fichero(), params={"detalle": True})
    assert respuesta.status_code == 200
    assert respuesta.json()["bruto"]["engine"] == "local"
    assert fake_ocr.llamadas[-1]["detalle"] is True


def test_ocr_rechaza_motor_desconocido(cliente_con_fakes, fake_ocr):
    respuesta = cliente_con_fakes.post("/api/ocr", files=_fichero(), params={"engine": "magia"})
    assert respuesta.status_code == 400
    assert respuesta.json()["error"]["codigo"] == "engine_invalido"
    assert fake_ocr.llamadas == []


def test_ocr_rechaza_fichero_vacio(cliente_con_fakes):
    respuesta = cliente_con_fakes.post("/api/ocr", files=_fichero(b""))
    assert respuesta.status_code == 400
    assert respuesta.json()["error"]["codigo"] == "fichero_vacio"


def test_ocr_limita_el_tamano(settings, fake_mongo, fake_ocr):
    """max_upload_mb=1 en los tests: 2 MB se rechazan con 413."""
    app = create_app(settings)
    app.dependency_overrides[get_mongo] = lambda: fake_mongo
    app.dependency_overrides[get_ocr] = lambda: fake_ocr
    with TestClient(app) as cliente:
        respuesta = cliente.post("/api/ocr", files=_fichero(b"x" * (2 * 1024 * 1024)))
        assert respuesta.status_code == 413
        assert respuesta.json()["error"]["codigo"] == "demasiado_grande"
        assert fake_ocr.llamadas == []
    app.dependency_overrides.clear()


def test_ocr_sin_fichero(cliente_con_fakes):
    assert cliente_con_fakes.post("/api/ocr").status_code == 422


def test_errores_del_ocr_se_propagan(settings, fake_mongo):
    """Un 503 del OCR no se convierte en un 500 opaco."""
    app = create_app(settings)
    app.dependency_overrides[get_mongo] = lambda: fake_mongo
    app.dependency_overrides[get_ocr] = lambda: FakeOcr(
        error=ApiError(503, "ocr_no_disponible", "El servicio de OCR no esta disponible.")
    )
    with TestClient(app) as cliente:
        respuesta = cliente.post("/api/ocr", files=_fichero())
        assert respuesta.status_code == 503
        assert respuesta.json()["error"]["codigo"] == "ocr_no_disponible"

        salud = cliente.get("/health")
        assert salud.status_code == 200
        assert salud.json()["dependencias"]["ocr"]["ok"] is False
        assert salud.json()["estado"] == "degradado"  # mongo si responde, ocr no
    app.dependency_overrides.clear()


def test_error_400_del_ocr_llega_como_400(settings, fake_mongo):
    app = create_app(settings)
    app.dependency_overrides[get_mongo] = lambda: fake_mongo
    app.dependency_overrides[get_ocr] = lambda: FakeOcr(
        error=ApiError(400, "error_ocr", "El PDF esta protegido.")
    )
    with TestClient(app) as cliente:
        respuesta = cliente.post("/api/ocr", files=_fichero())
    app.dependency_overrides.clear()

    assert respuesta.status_code == 400
    assert respuesta.json()["error"]["mensaje"] == "El PDF esta protegido."
