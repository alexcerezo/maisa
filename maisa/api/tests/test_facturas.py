"""Facturas: listado, filtros, paginacion, detalle y PDF."""

from __future__ import annotations

from pathlib import Path

from app.routers.facturas import resolver_pdf

from .conftest import PDF_BYTES, PDF_VALIDO


def _items(respuesta) -> list[dict]:
    return respuesta.json()["items"]


def test_listado_basico(client):
    respuesta = client.get("/api/facturas")
    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert datos["total"] == 4
    assert datos["devueltas"] == 4
    assert [fila["file_id"] for fila in datos["items"]] == [
        "2026-01-08_P001.pdf",
        "2026-02-01_P002.pdf",
        "2026-02-02_P003.pdf",
        "2026-02-03_P004.pdf",
    ]
    primera = datos["items"][0]
    assert primera["resultado"] == "PAGAR"
    assert primera["motivo_principal"] is None
    assert primera["proveedor"] == "Suministros Levante S.L."
    assert primera["total"] == 3012.89
    assert primera["importe_erp"] == 3012.89
    assert primera["desvio_importe"] == 0.0
    assert primera["calidad_lectura"] == 1.0
    assert primera["lote"] == 1
    assert primera["metodo_lectura"] == "texto_determinista"
    assert primera["asiento"] == "AS-00096"


def test_motivo_principal_derivado_de_los_hechos(client):
    """Sin `motivos`, el resumen usa el primer hecho incumplido."""
    filas = {fila["file_id"]: fila for fila in _items(client.get("/api/facturas"))}
    assert filas["2026-02-03_P004.pdf"]["motivo_principal"] == "el pedido esta PENDIENTE en el ERP"
    assert filas["2026-02-01_P002.pdf"]["motivo_principal"] == "fecha invalida: 31/02/2026"


def test_filtro_por_resultado(client):
    respuesta = client.get("/api/facturas", params={"resultado": "ESCALAR"})
    assert respuesta.status_code == 200
    assert respuesta.json()["total"] == 1
    assert _items(respuesta)[0]["file_id"] == "2026-02-01_P002.pdf"


def test_filtro_por_resultado_invalido(client):
    respuesta = client.get("/api/facturas", params={"resultado": "QUIZAS"})
    assert respuesta.status_code == 400
    assert respuesta.json()["error"]["codigo"] == "resultado_invalido"


def test_filtro_por_lote_y_proveedor(client):
    assert client.get("/api/facturas", params={"lote": 2}).json()["total"] == 2
    assert client.get("/api/facturas", params={"lote": 1}).json()["total"] == 2
    # El filtro de proveedor no distingue mayusculas ni exige coincidencia exacta.
    respuesta = client.get("/api/facturas", params={"proveedor": "levante"})
    assert respuesta.json()["total"] == 2
    assert client.get("/api/facturas", params={"proveedor": "nadie"}).json()["total"] == 0


def test_paginacion_y_orden(client):
    respuesta = client.get("/api/facturas", params={"limit": 2, "offset": 1})
    datos = respuesta.json()
    assert datos["total"] == 4
    assert datos["devueltas"] == 2
    assert datos["limit"] == 2
    assert datos["offset"] == 1
    assert [fila["file_id"] for fila in datos["items"]] == ["2026-02-01_P002.pdf", "2026-02-02_P003.pdf"]


def test_tope_maximo_de_paginacion(client):
    """Un `limit` por encima del tope no falla: se recorta y se informa."""
    datos = client.get("/api/facturas", params={"limit": 99999}).json()
    assert datos["limit"] == 500  # MAX_LIMIT
    assert datos["devueltas"] == 4
    assert datos["total"] == 4


def test_limites_invalidos(client):
    assert client.get("/api/facturas", params={"limit": 0}).status_code == 422
    assert client.get("/api/facturas", params={"offset": -1}).status_code == 422


def test_busqueda_texto_libre(client):
    assert client.get("/api/facturas", params={"q": "PO-2026-0476"}).json()["total"] == 1
    assert client.get("/api/facturas", params={"q": "as-00200"}).json()["total"] == 1
    assert client.get("/api/facturas", params={"q": "2026-01-08_P001.pdf"}).json()["total"] == 1
    assert client.get("/api/facturas", params={"q": "talleres"}).json()["total"] == 1
    assert client.get("/api/facturas", params={"q": "inexistente"}).json()["total"] == 0


def test_busqueda_escapa_metacaracteres(client):
    """El texto del usuario se escapa: ni inyeccion de patrones ni ReDoS."""
    # "P001+" solo coincide literalmente con el proveedor "P001+ Logistica".
    respuesta = client.get("/api/facturas", params={"q": "P001+"})
    assert respuesta.json()["total"] == 1
    assert _items(respuesta)[0]["proveedor"] == "P001+ Logistica"

    # Un parentesis suelto (regex invalida si no se escapara) no rompe nada.
    assert client.get("/api/facturas", params={"q": "("}).json()["total"] == 1

    # ".*" como patron lo abarcaria todo; escapado no coincide con nada.
    assert client.get("/api/facturas", params={"q": ".*"}).json()["total"] == 0

    # Patron catastrofico tipico: escapado es una busqueda literal, no un ReDoS.
    assert client.get("/api/facturas", params={"q": "(a+)+$"}).json()["total"] == 0


def test_busqueda_demasiado_larga(client):
    respuesta = client.get("/api/facturas", params={"q": "x" * 200})
    assert respuesta.status_code in (400, 422)


def test_detalle_completo(client):
    respuesta = client.get("/api/facturas/2026-02-01_P002.pdf")
    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert datos["file_id"] == "2026-02-01_P002.pdf"
    assert datos["resultado"] == "ESCALAR"
    assert datos["motivos"] == ["fecha invalida: 31/02/2026"]
    assert datos["hechos"][0]["regla"] == "R4_fecha"
    assert datos["campos"]["proveedor"] == "Talleres Rios (S.A.)"
    assert datos["lectura"]["metodo_lectura"] == "vision_ocr"
    assert datos["lectura"]["calidad_lectura"] == 0.87
    assert datos["lectura"]["segundos_lectura"] == 1.5
    assert datos["lectura"]["sospechosos"] == ["2026-02-31"]
    assert datos["lote"] == 1
    assert datos["version_norma"] == "norma_v3.1"
    assert datos["resumen"]["motivo_principal"] == "fecha invalida: 31/02/2026"


def test_detalle_inexistente_da_404(client):
    respuesta = client.get("/api/facturas/2026-12-31_P999.pdf")
    assert respuesta.status_code == 404
    assert respuesta.json()["error"]["codigo"] == "factura_no_encontrada"


def test_pdf_inline(client):
    respuesta = client.get(f"/api/facturas/{PDF_VALIDO}/pdf")
    assert respuesta.status_code == 200
    assert respuesta.headers["content-type"] == "application/pdf"
    assert respuesta.headers["content-disposition"].startswith("inline")
    assert respuesta.headers["content-length"] == str(len(PDF_BYTES))
    assert respuesta.content == PDF_BYTES


def test_pdf_inexistente_da_404(client):
    respuesta = client.get("/api/facturas/2026-12-31_P999.pdf/pdf")
    assert respuesta.status_code == 404
    assert respuesta.json()["error"]["codigo"] == "pdf_no_encontrado"


def test_resolver_pdf_no_permite_salir_del_directorio(facturas_dir: Path):
    assert resolver_pdf(facturas_dir, PDF_VALIDO) is not None
    assert resolver_pdf(facturas_dir, "../outputs/outcomes.jsonl") is None
    assert resolver_pdf(facturas_dir, "/etc/passwd") is None
    assert resolver_pdf(facturas_dir, "..") is None
    assert resolver_pdf(facturas_dir, "") is None
    assert resolver_pdf(facturas_dir, "no-existe.pdf") is None


def test_traversal_por_http_da_404(client):
    respuesta = client.get("/api/facturas/%2e%2e%2f%2e%2e%2fetc%2fpasswd/pdf")
    assert respuesta.status_code in (400, 404)
