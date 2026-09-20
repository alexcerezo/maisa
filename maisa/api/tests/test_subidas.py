"""Subida de facturas: POST /api/facturas y sus vistas en /api/expedientes.

El almacen real (Mongo + GridFS) se sustituye por `FakeAlmacen`, que reutiliza
las funciones de validacion de `app.almacen`; lo que se prueba aqui es el
contrato HTTP: codigos, idempotencia, limites y que un fallo del OCR no tumba
la subida.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from app.errors import ApiError
from app.main import create_app

from .conftest import PDF_BYTES, construir_settings

NOMBRE = "2026-03-01_P004.pdf"
OTRO_NOMBRE = "2026-03-02_P005.pdf"


def sobre(respuesta) -> dict:
    """Cuerpo del error de la API: {"error": {"codigo", "mensaje", "detalle"}}."""
    return respuesta.json()["error"]


def subir(cliente, *, nombre=NOMBRE, contenido=PDF_BYTES, **params):
    return cliente.post(
        "/api/facturas",
        files={"file": (nombre, contenido, "application/pdf")},
        params=params,
    )


# --------------------------------------------------------------------------- #
# Camino feliz
# --------------------------------------------------------------------------- #
def test_sube_un_pdf_y_crea_el_expediente(cliente_con_fakes, fake_almacen):
    respuesta = subir(cliente_con_fakes)
    assert respuesta.status_code == 201
    datos = respuesta.json()

    assert datos["file_id"] == NOMBRE
    assert datos["duplicado"] is False
    assert datos["lote_id"] == "lote1"
    assert datos["estado_proceso"] == "PENDIENTE"
    assert datos["tamano_bytes"] == len(PDF_BYTES)
    assert datos["eventos"] == ["EXPEDIENTE_ESTADO"]
    assert datos["urls"]["pdf"] == f"/api/facturas/{NOMBRE}/pdf"

    # El PDF quedo en GridFS y el expediente en Mongo.
    assert fake_almacen.pdfs[NOMBRE] == PDF_BYTES
    documento = fake_almacen.expedientes[NOMBRE]
    assert documento["documento"]["sha256"] == datos["sha256"]
    assert documento["decision"] is None, "INV-2/INV-3: sin decision hasta COMPLETADA"
    assert documento["ocr"]["motor"] == "ninguno"
    assert fake_almacen.eventos[0]["tipo"] == "EXPEDIENTE_ESTADO"


def test_el_lote_se_traduce_al_enum_del_esquema(cliente_con_fakes):
    assert subir(cliente_con_fakes, lote=2).json()["lote_id"] == "lote2"
    assert subir(cliente_con_fakes, nombre=OTRO_NOMBRE, lote=1).json()["lote_id"] == "lote1"


def test_sube_con_ocr_y_guarda_las_lineas(cliente_con_fakes, fake_almacen, fake_ocr):
    respuesta = subir(cliente_con_fakes, ocr=True, engine="local")
    assert respuesta.status_code == 201
    datos = respuesta.json()

    assert datos["estado_proceso"] == "OCR"
    assert datos["eventos"] == ["EXPEDIENTE_ESTADO", "OCR_OK"]
    assert datos["ocr"]["solicitado"] is True
    assert datos["ocr"]["ejecutado"] is True
    assert datos["ocr"]["motor"] == "rapidocr"
    assert datos["ocr"]["engine"] == "local"
    assert datos["ocr"]["aviso"] is None

    # El OCR se pidio con detalle para poder guardar las lineas.
    assert fake_ocr.llamadas[0]["detalle"] is True
    assert fake_ocr.llamadas[0]["engine"] == "local"
    assert fake_ocr.llamadas[0]["nombre"] == NOMBRE

    guardado = fake_almacen.expedientes[NOMBRE]["ocr"]
    assert [ln["texto"] for ln in guardado["lineas"]] == ["FACTURA 123", "TOTAL 100,00"]
    assert guardado["lineas"][0]["bbox"] == [100.0, 200.0, 500.0, 260.0]


def test_sube_con_ocr_y_guarda_la_geometria_para_resaltar(cliente_con_fakes, fake_almacen):
    """Sin `escala` la caja no se puede volver a poner sobre el PDF."""
    subir(cliente_con_fakes, ocr=True)

    geo = fake_almacen.expedientes[NOMBRE]["ocr"]["paginas_geo"]
    (entrada,) = geo
    assert entrada["pagina"] == 0
    assert entrada["escala"] == 4.0
    assert entrada["ancho"] == 2382.0
    assert entrada["alto"] == 3368.0
    assert entrada["lineas"][1] == {
        "texto": "TOTAL 100,00",
        "caja": [100.0, 3000.0, 900.0, 3070.0],
        "score": 0.91,
    }


def test_sin_ocr_no_hay_geometria_que_resaltar(cliente_con_fakes, fake_almacen):
    subir(cliente_con_fakes, ocr=False)
    assert "paginas_geo" not in fake_almacen.expedientes[NOMBRE]["ocr"]


def test_engine_auto_no_se_propaga_al_ocr(cliente_con_fakes, fake_ocr):
    subir(cliente_con_fakes, ocr=True, engine="auto")
    assert fake_ocr.llamadas[0]["engine"] is None


def test_el_motor_cloud_se_guarda_como_paddleocr_vl(cliente_con_fakes, fake_ocr):
    """`expedientes.ocr.motor` es un enum cerrado: `cloud` no es un valor valido."""
    datos = subir(cliente_con_fakes, ocr=True, engine="cloud").json()
    assert datos["ocr"]["motor"] == "paddleocr_vl"
    assert datos["ocr"]["engine"] == "cloud"


# --------------------------------------------------------------------------- #
# Idempotencia y conflictos
# --------------------------------------------------------------------------- #
def test_resubir_el_mismo_fichero_es_idempotente(cliente_con_fakes, fake_almacen):
    primera = subir(cliente_con_fakes)
    segunda = subir(cliente_con_fakes)

    assert primera.status_code == 201
    assert segunda.status_code == 200, "mismo nombre y mismo contenido: no es un error"
    assert segunda.json()["duplicado"] is True
    assert segunda.json()["sha256"] == primera.json()["sha256"]
    # No se duplico nada.
    assert len(fake_almacen.expedientes) == 1
    assert len(fake_almacen.eventos) == 1


def test_mismo_nombre_con_otro_contenido_devuelve_409(cliente_con_fakes):
    assert subir(cliente_con_fakes).status_code == 201
    respuesta = subir(cliente_con_fakes, contenido=PDF_BYTES + b"% otro\n")
    assert respuesta.status_code == 409
    error = sobre(respuesta)
    assert error["codigo"] == "factura_ya_existe"
    assert error["detalle"]["file_id"] == NOMBRE


# --------------------------------------------------------------------------- #
# Validacion de la peticion
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "nombre",
    [
        "factura.txt",
        "sin_extension",
        "factura.PDFX",
        "mal nombre.pdf",
        ".",
        "..",
    ],
)
def test_nombre_invalido_devuelve_400(cliente_con_fakes, nombre):
    respuesta = subir(cliente_con_fakes, nombre=nombre)
    assert respuesta.status_code == 400
    assert sobre(respuesta)["codigo"] == "nombre_invalido"


@pytest.mark.parametrize(
    ("nombre", "esperado"),
    [
        # Una barra no es un ataque: se guarda el basename. Lo que no se acepta
        # es que el `file_id` resultante lleve ruta.
        ("sub/dir/factura.pdf", "factura.pdf"),
        ("../fuera.pdf", "fuera.pdf"),
        (f"C:\\facturas\\{NOMBRE}", NOMBRE),
    ],
)
def test_una_ruta_se_reduce_al_nombre(cliente_con_fakes, fake_almacen, nombre, esperado):
    respuesta = subir(cliente_con_fakes, nombre=nombre)
    assert respuesta.status_code == 201
    assert respuesta.json()["file_id"] == esperado
    assert esperado in fake_almacen.expedientes


def test_sin_nombre_de_fichero_es_una_peticion_invalida(cliente_con_fakes):
    """Con `filename` vacio el cuerpo deja de ser un fichero: lo corta FastAPI."""
    respuesta = subir(cliente_con_fakes, nombre="")
    assert respuesta.status_code == 422
    assert sobre(respuesta)["codigo"] == "peticion_invalida"


def test_fichero_sin_firma_pdf_devuelve_415(cliente_con_fakes):
    respuesta = subir(cliente_con_fakes, contenido=b"esto no es un pdf")
    assert respuesta.status_code == 415
    assert sobre(respuesta)["codigo"] == "formato_no_soportado"


def test_fichero_vacio_devuelve_400(cliente_con_fakes):
    respuesta = subir(cliente_con_fakes, contenido=b"")
    assert respuesta.status_code == 400
    assert sobre(respuesta)["codigo"] == "fichero_vacio"


def test_fichero_demasiado_grande_devuelve_413(cliente_con_fakes, settings):
    grande = b"%PDF-1.4\n" + b"x" * settings.max_upload_bytes
    respuesta = subir(cliente_con_fakes, contenido=grande)
    assert respuesta.status_code == 413
    assert sobre(respuesta)["codigo"] == "demasiado_grande"


def test_lote_fuera_de_rango_lo_rechaza_fastapi(cliente_con_fakes):
    assert subir(cliente_con_fakes, lote=3).status_code == 422
    assert subir(cliente_con_fakes, lote=0).status_code == 422


def test_engine_desconocido_devuelve_400(cliente_con_fakes):
    respuesta = subir(cliente_con_fakes, ocr=True, engine="magico")
    assert respuesta.status_code == 400
    assert sobre(respuesta)["codigo"] == "engine_invalido"


def test_sin_fichero_devuelve_422(cliente_con_fakes):
    respuesta = cliente_con_fakes.post("/api/facturas")
    assert respuesta.status_code == 422
    assert sobre(respuesta)["codigo"] == "peticion_invalida"


def test_subidas_desactivadas_devuelven_403(outputs_dir, facturas_dir, ui_dir, fake_almacen):
    settings = dataclasses.replace(
        construir_settings(outputs_dir, facturas_dir, ui_dir), subidas_habilitadas=False
    )
    app = create_app(settings)
    from app.deps import get_almacen

    app.dependency_overrides[get_almacen] = lambda: fake_almacen
    with TestClient(app) as cliente:
        respuesta = subir(cliente)
    assert respuesta.status_code == 403
    assert sobre(respuesta)["codigo"] == "subidas_deshabilitadas"


# --------------------------------------------------------------------------- #
# El OCR es best-effort
# --------------------------------------------------------------------------- #
def test_si_el_ocr_falla_la_factura_se_guarda_igual(cliente_con_fakes, fake_almacen, fake_ocr):
    fake_ocr._error = ApiError(502, "respuesta_ocr_invalida", "el OCR devolvio basura")

    respuesta = subir(cliente_con_fakes, ocr=True)
    assert respuesta.status_code == 201, "perder la lectura es mejor que perder el PDF"
    datos = respuesta.json()

    assert datos["estado_proceso"] == "PENDIENTE"
    assert datos["ocr"]["ejecutado"] is False
    assert datos["ocr"]["aviso"]["codigo"] == "respuesta_ocr_invalida"
    assert datos["eventos"] == ["EXPEDIENTE_ESTADO", "OCR_FAIL"]
    assert [evento["tipo"] for evento in fake_almacen.eventos] == ["EXPEDIENTE_ESTADO", "OCR_FAIL"]


def test_mongo_caido_devuelve_503(cliente_con_fakes, fake_almacen):
    from app.mongo_repo import MongoNoDisponible

    fake_almacen._error = MongoNoDisponible("conexion rechazada")
    respuesta = subir(cliente_con_fakes)
    assert respuesta.status_code == 503
    assert sobre(respuesta)["codigo"] == "mongo_no_disponible"


def test_el_esquema_rechazado_devuelve_500(cliente_con_fakes, fake_almacen):
    from app.almacen import DocumentoRechazado

    async def rechazar(**kwargs):
        raise DocumentoRechazado("Document failed validation")

    fake_almacen.guardar = rechazar
    respuesta = subir(cliente_con_fakes)
    assert respuesta.status_code == 500
    assert sobre(respuesta)["codigo"] == "esquema_incompatible"


# --------------------------------------------------------------------------- #
# Lectura de lo subido
# --------------------------------------------------------------------------- #
def test_el_pdf_subido_se_sirve_si_no_esta_en_disco(cliente_con_fakes, facturas_dir):
    subir(cliente_con_fakes, nombre=NOMBRE)
    assert not (facturas_dir / NOMBRE).exists(), "GridFS, no disco"

    respuesta = cliente_con_fakes.get(f"/api/facturas/{NOMBRE}/pdf")
    assert respuesta.status_code == 200
    assert respuesta.headers["content-type"] == "application/pdf"
    assert respuesta.content == PDF_BYTES


def test_el_pdf_de_disco_tiene_prioridad(cliente_con_fakes, facturas_dir):
    """Los PDF que dejo el motor se sirven desde disco, sin tocar Mongo."""
    from .conftest import PDF_VALIDO

    respuesta = cliente_con_fakes.get(f"/api/facturas/{PDF_VALIDO}/pdf")
    assert respuesta.status_code == 200
    assert respuesta.content == PDF_BYTES


def test_pdf_inexistente_devuelve_404(cliente_con_fakes):
    respuesta = cliente_con_fakes.get(f"/api/facturas/{NOMBRE}/pdf")
    assert respuesta.status_code == 404
    assert sobre(respuesta)["codigo"] == "pdf_no_encontrado"


def test_expedientes_lista_y_detalla(cliente_con_fakes):
    subir(cliente_con_fakes)
    subir(cliente_con_fakes, nombre=OTRO_NOMBRE, lote=2)

    listado = cliente_con_fakes.get("/api/expedientes").json()
    assert listado["total"] == 2
    assert {item["_id"] for item in listado["items"]} == {NOMBRE, OTRO_NOMBRE}

    filtrado = cliente_con_fakes.get("/api/expedientes", params={"lote_id": "lote2"}).json()
    assert filtrado["total"] == 1
    assert filtrado["items"][0]["_id"] == OTRO_NOMBRE

    detalle = cliente_con_fakes.get(f"/api/expedientes/{NOMBRE}").json()
    assert detalle["_id"] == NOMBRE
    assert detalle["lote_id"] == "lote1"


def test_expediente_inexistente_devuelve_404(cliente_con_fakes):
    respuesta = cliente_con_fakes.get(f"/api/expedientes/{NOMBRE}")
    assert respuesta.status_code == 404
    assert sobre(respuesta)["codigo"] == "expediente_no_encontrado"


def test_filtros_de_expedientes_invalidos_devuelven_400(cliente_con_fakes):
    assert cliente_con_fakes.get("/api/expedientes", params={"lote_id": "lote9"}).status_code == 400
    assert cliente_con_fakes.get("/api/expedientes", params={"estado": "INVENTADO"}).status_code == 400


def test_la_subida_exige_api_key_si_esta_configurada(client_con_api_key):
    respuesta = client_con_api_key.post(
        "/api/facturas", files={"file": (NOMBRE, PDF_BYTES, "application/pdf")}
    )
    assert respuesta.status_code == 401
    assert sobre(respuesta)["codigo"] == "no_autorizado"
