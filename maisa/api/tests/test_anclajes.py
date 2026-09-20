"""Anclajes: donde esta cada dato leido dentro del PDF.

Hay dos caminos que se prueban por separado porque son cosas distintas:

  * **Capa de texto**: la API solo dice que buscar (`tokens`), nunca donde. El
    rectangulo lo resuelve `pdf.js` al pintar, porque dos extractores de texto no
    parten las lineas igual y una caja calculada con otro extractor no cuadraria.
  * **OCR**: no hay texto que buscar, asi que la caja viene de la cache del motor
    y se prueba que se convierte de pixeles del bitmap a puntos del PDF.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.anclajes import (
    CONFIANZA_APROXIMADA,
    CONFIANZA_CON_PISTA,
    CONFIANZA_SIN_PISTA,
    CacheGeo,
    _busca,
    _casa,
    _pagina_publica,
    _sanea_geo,
    _tokens,
    _tokens_fecha,
    construir,
    normaliza,
)

from .conftest import API_KEY

SHA_OCR = "b" * 64
FILE_OCR = "2026-02-01_P002.pdf"
FILE_TEXTO = "2026-01-08_P001.pdf"

# Geometria como la escribe el motor: cajas en pixeles del bitmap y la escala
# con la que se renderizo. A escala 2.0, la pagina de 1191x1684 px es un A4.
GEO = [
    {
        "pagina": 0,
        "escala": 2.0,
        "ancho": 1191.0,
        "alto": 1684.0,
        "lineas": [
            {"texto": "Talleres Rios (S.A.)", "caja": [100.0, 100.0, 700.0, 140.0]},
            {"texto": "Total factura: 1.512,50 EUR", "caja": [100.0, 900.0, 700.0, 940.0]},
        ],
    }
]


def registro_ocr(**campos) -> dict:
    base = {"total": "1512.50", "fecha": "2026-02-01"}
    base.update(campos)
    return {
        "file_id": FILE_OCR,
        "sha256": SHA_OCR,
        "escalon_lectura": "cache_ocr",
        "campos": base,
    }


def registro_texto(**campos) -> dict:
    base = {"total": "3012.89", "nif_candidatos": ["B46102331"], "fecha": "2026-01-08"}
    base.update(campos)
    return {
        "file_id": FILE_TEXTO,
        "sha256": "a" * 64,
        "escalon_lectura": "capa_texto",
        "campos": base,
    }


def escribe_cache(carpeta: Path, sha: str, geo) -> Path:
    ruta = carpeta / f"{sha}.json"
    ruta.write_text(json.dumps({"sha256": sha, "version": 3, "geo": geo}), encoding="utf-8")
    return ruta


def _campo(datos: dict, nombre: str) -> dict:
    return next(campo for campo in datos["campos"] if campo["campo"] == nombre)


# --------------------------------------------------------------------------- #
# Normalizacion: la razon de que esto funcione
# --------------------------------------------------------------------------- #
def test_normaliza_iguala_el_valor_del_motor_y_el_del_pdf():
    """`3.012,89` y `3012.89` son el mismo token: es lo que hace posible buscar."""
    assert normaliza("3.012,89 EUR") == "301289eur"
    assert normaliza("3012.89").startswith("301289")
    assert normaliza("ES21 0049 1500 0512 3456 7890") == normaliza("ES2100491500051234567890")


def test_normaliza_quita_acentos_porque_el_ocr_los_pierde():
    assert normaliza("Facturación") == normaliza("Facturacion")


def test_un_token_corto_no_se_usa():
    """`21` (el tipo de IVA) sale en medio documento: mejor no resaltar."""
    assert _tokens("iva_pct", "21") == []
    assert _tokens("total", "3012.89") == ["301289"]


def test_tokens_de_fecha_cubren_las_formas_del_corpus():
    tokens = _tokens_fecha("2026-01-08")
    assert "08012026" in tokens  # 08/01/2026
    assert "8012026" in tokens  # 8/01/2026
    assert "20260108" in tokens  # ISO
    assert "8deenerode2026" in tokens  # "8 de enero de 2026"


def test_tokens_de_fecha_con_valor_no_iso():
    assert _tokens_fecha("31/02/2026") == ["31022026"]


# --------------------------------------------------------------------------- #
# Coincidencia: exacta y con erratas del OCR
# --------------------------------------------------------------------------- #
def test_casa_exacto():
    assert _casa("totalfactura151250eur", "151250") == 0


def test_casa_tolera_una_errata_del_ocr():
    """`B90233414` sale como `B9023341`: exigir exactitud dejaria sin resaltar
    justo las escaneadas, que son las que mas lo necesitan."""
    assert _casa("nifb9023341", "b90233414") == 1
    assert _casa("pedidapd20280480", "po20280480") == 1


def test_casa_tolera_una_letra_de_mas_o_de_menos_en_un_iban():
    """El OCR tambien inventa y se come caracteres en medio de un IBAN, que es
    largo: con un solo fallo tiene que casar."""
    iban = "es4721007788103000445566"
    assert _casa("ibanes4721007788z103000445566", iban) == 1  # letra de mas
    assert _casa("ibanes472100778810300044566", iban) == 1  # letra de menos


def test_casa_no_tolera_cualquier_cosa():
    assert _casa("pedidopo20260200", "es2100491500051234567890") is None
    assert _casa("", "151250") is None


def test_casa_no_aproxima_tokens_cortos():
    """Con tokens cortos una errata los convierte en cualquier cosa."""
    assert _casa("factura21", "22") is None


def test_busca_devuelve_las_coincidencias_y_marca_la_etiquetada():
    """Un mismo numero puede salir en varias lineas: se dan todas, y la que trae
    la etiqueta del campo es la que va con mas confianza."""
    geo = [
        {
            "pagina": 0,
            "escala": 1.0,
            "lineas": [
                {"texto": "Cliente 1.512,50", "caja": [0.0, 0.0, 10.0, 10.0]},
                {"texto": "Total factura 1.512,50", "caja": [0.0, 20.0, 10.0, 30.0]},
            ],
        }
    ]
    anclas = _busca(["151250"], ("total",), geo)
    assert [ancla["texto"] for ancla in anclas] == ["Cliente 1.512,50", "Total factura 1.512,50"]
    assert [ancla["confianza"] for ancla in anclas] == [CONFIANZA_SIN_PISTA, CONFIANZA_CON_PISTA]


def test_busca_marca_la_coincidencia_aproximada():
    geo = [{"pagina": 0, "escala": 1.0, "lineas": [{"texto": "Total 1512,51", "caja": [0.0, 0.0, 1.0, 1.0]}]}]
    anclas = _busca(["151250"], ("total",), geo)
    assert anclas[0]["aproximado"] is True
    assert anclas[0]["confianza"] == CONFIANZA_APROXIMADA


def test_busca_sin_pista_baja_la_confianza():
    geo = [{"pagina": 0, "escala": 1.0, "lineas": [{"texto": "1.512,50", "caja": [0.0, 0.0, 1.0, 1.0]}]}]
    assert _busca(["151250"], ("total",), geo)[0]["confianza"] == CONFIANZA_SIN_PISTA


def test_busca_sin_coincidencias_devuelve_vacio():
    assert _busca(["151250"], ("total",), []) == []


# --------------------------------------------------------------------------- #
# Conversion de pixeles del bitmap a puntos del PDF
# --------------------------------------------------------------------------- #
def test_pagina_publica_divide_por_la_escala():
    """El OCR mide en pixeles del bitmap (hasta 288 dpi) y el visor en puntos."""
    pagina = _pagina_publica({"pagina": 0, "escala": 4.0, "ancho": 2382.0, "alto": 3368.0})
    assert pagina == {"pagina": 0, "ancho": 595.5, "alto": 842.0}


def test_pagina_sin_escala_se_descarta():
    """Sin escala la caja no se puede pintar: mejor no ofrecer geometria que mal."""
    assert _pagina_publica({"pagina": 0, "ancho": 2382.0}) is None
    assert _pagina_publica({"pagina": 0, "escala": 0}) is None


def test_sanea_geo_descarta_lo_que_no_encaja():
    paginas = _sanea_geo(
        [
            "no soy una pagina",
            {"pagina": 0, "escala": 1.0, "lineas": [{"texto": "sin caja"}]},
            {"pagina": 1, "escala": 1.0, "lineas": [{"texto": "ok", "caja": [0, 0, 1, 1]}]},
        ]
    )
    assert [pagina["pagina"] for pagina in paginas] == [1]


def test_sanea_geo_tolera_basura():
    assert _sanea_geo(None) == []
    assert _sanea_geo({"pagina": 0}) == []


# --------------------------------------------------------------------------- #
# construir(): la respuesta de la API
# --------------------------------------------------------------------------- #
def test_capa_de_texto_da_tokens_y_no_geometria():
    datos = construir(registro_texto(), None)
    assert datos["origen"] == "capa_texto"
    assert datos["paginas"] == []
    assert datos["aviso"] is None
    total = _campo(datos, "total")
    assert total["tokens"] == ["301289"]
    # Sin geometria el cliente resuelve: la API no inventa cajas.
    assert total["anclas"] == []


def test_ocr_da_anclas_en_puntos_del_pdf():
    datos = construir(registro_ocr(), GEO)
    assert datos["origen"] == "ocr"
    assert datos["paginas"] == [{"pagina": 0, "ancho": 595.5, "alto": 842.0}]
    total = _campo(datos, "total")
    assert total["anclas"][0]["texto"] == "Total factura: 1.512,50 EUR"
    # 100..700 px a escala 2.0 -> 50..350 pt
    assert total["anclas"][0]["bbox"] == [50.0, 450.0, 350.0, 470.0]


def test_ocr_sin_geometria_lo_dice_en_vez_de_parecer_vacio():
    datos = construir(registro_ocr(), [])
    assert datos["origen"] == "ocr"
    assert datos["campos"] != []
    assert datos["aviso"] is not None


def test_campos_del_erp_no_se_buscan_en_el_pdf():
    """`asiento` o `importe_erp` no estan en el documento: buscarlos da falsos positivos."""
    datos = construir(
        registro_texto(asiento="AS-00096", importe_erp="3012.89", nif_maestro="B46102331"),
        GEO,
    )
    assert {campo["campo"] for campo in datos["campos"]} == {"nif", "fecha", "total"}


def test_campo_sin_valor_no_aparece():
    datos = construir(registro_texto(total=None, nif_candidatos=[], fecha=""), GEO)
    assert datos["campos"] == []


def test_el_candidato_manda_sobre_el_valor_conciliado():
    """Lo que esta escrito en el papel es lo que hay que resaltar."""
    datos = construir(registro_texto(pedido="PO-2026-9999", pedido_candidatos=["PO-2026-0096"]), GEO)
    assert _campo(datos, "pedido")["valor"] == "PO-2026-0096"


# --------------------------------------------------------------------------- #
# CacheGeo: lectura de la cache del motor
# --------------------------------------------------------------------------- #
def test_cache_geo_lee_y_memoriza(ocr_cache_dir: Path):
    ruta = escribe_cache(ocr_cache_dir, SHA_OCR, GEO)
    cache = CacheGeo(ocr_cache_dir)
    assert cache.disponible
    assert cache.leer(SHA_OCR) == _sanea_geo(GEO)
    # Se reescribe el fichero: la memoria tiene que notarlo (mtime/tamano).
    escribe_cache(ocr_cache_dir, SHA_OCR, GEO[:0])
    assert cache.leer(SHA_OCR) == []
    assert ruta.is_file()


def test_cache_geo_rechaza_un_nombre_que_no_es_sha(ocr_cache_dir: Path):
    """El sha viene de la traza, pero se valida igual: es un nombre de fichero."""
    cache = CacheGeo(ocr_cache_dir)
    assert cache.ruta("../../etc/passwd") is None
    assert cache.ruta("b" * 63) is None
    assert cache.ruta(None) is None
    assert cache.ruta(SHA_OCR) == ocr_cache_dir / f"{SHA_OCR}.json"


def test_cache_geo_tolera_fichero_corrupto(ocr_cache_dir: Path):
    (ocr_cache_dir / f"{SHA_OCR}.json").write_text("{esto no es json", encoding="utf-8")
    assert CacheGeo(ocr_cache_dir).leer(SHA_OCR) == []


def test_cache_geo_sin_carpeta_no_revienta(tmp_path: Path):
    cache = CacheGeo(tmp_path / "no-existe")
    assert not cache.disponible
    assert cache.leer(SHA_OCR) == []


def test_cache_geo_acepta_una_entrada_sin_geo(ocr_cache_dir: Path):
    """Las entradas anteriores a la v3 son validas para el motor, pero sin cajas."""
    (ocr_cache_dir / f"{SHA_OCR}.json").write_text(
        json.dumps({"sha256": SHA_OCR, "texto": "legacy"}), encoding="utf-8"
    )
    assert CacheGeo(ocr_cache_dir).leer(SHA_OCR) == []


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #
def test_anclajes_de_una_factura_con_capa_de_texto(client):
    respuesta = client.get(f"/api/facturas/{FILE_TEXTO}/anclajes")
    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert datos["origen"] == "capa_texto"
    assert _campo(datos, "total")["tokens"] == ["301289"]
    assert "08012026" in _campo(datos, "fecha")["tokens"]


def test_anclajes_de_una_escaneada_usa_la_cache_del_motor(client, ocr_cache_dir: Path):
    escribe_cache(ocr_cache_dir, SHA_OCR, GEO)
    respuesta = client.get(f"/api/facturas/{FILE_OCR}/anclajes")
    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert datos["origen"] == "ocr"
    assert datos["paginas"] == [{"pagina": 0, "ancho": 595.5, "alto": 842.0}]
    assert _campo(datos, "total")["anclas"][0]["bbox"] == [50.0, 450.0, 350.0, 470.0]


def test_anclajes_de_una_escaneada_sin_cache_avisa(client):
    """La cache del motor no esta montada: se dice, no se devuelve vacio a secas."""
    respuesta = client.get(f"/api/facturas/{FILE_OCR}/anclajes")
    assert respuesta.status_code == 200
    assert respuesta.json()["aviso"] is not None


def test_anclajes_de_una_factura_inexistente(client):
    respuesta = client.get("/api/facturas/2026-01-01_P999.pdf/anclajes")
    assert respuesta.status_code == 404
    assert respuesta.json()["error"]["codigo"] == "factura_no_encontrada"


def test_anclajes_sin_traza_devuelve_503(settings, facturas_dir: Path, ui_dir: Path):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(settings)) as cliente:
        (settings.traza_paths[0]).unlink()
        cliente.app.state.traza.cargar()
        respuesta = cliente.get(f"/api/facturas/{FILE_TEXTO}/anclajes")
    assert respuesta.status_code == 503


def test_anclajes_exige_api_key(client_con_api_key):
    assert client_con_api_key.get(f"/api/facturas/{FILE_TEXTO}/anclajes").status_code == 401
    respuesta = client_con_api_key.get(
        f"/api/facturas/{FILE_TEXTO}/anclajes", headers={"X-API-Key": API_KEY}
    )
    assert respuesta.status_code == 200


@pytest.mark.parametrize("file_id", ["..", "a/b.pdf"])
def test_anclajes_rechaza_file_id_invalido(client, file_id: str):
    assert client.get(f"/api/facturas/{file_id}/anclajes").status_code in (404, 422)
