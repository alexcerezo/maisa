"""Mapeo del payload del OCR a los campos de `expedientes.ocr`.

`lineas_desde_ocr` alimenta `ocr.lineas` (texto y caja, para leer) y
`paginas_geo_desde_ocr` alimenta `ocr.paginas_geo` (texto, caja **y escala**, para
pintar). Se prueban por separado porque la segunda existe justo por lo que la
primera no puede guardar.
"""

from __future__ import annotations

import json

import pytest

from app.almacen import (
    MAX_LINEAS_OCR,
    MAX_PAGINAS_GEO,
    lineas_desde_ocr,
    paginas_geo_desde_ocr,
)
from app.anclajes import _sanea_geo


CAJA = [[0, 0], [10, 0], [10, 10], [0, 10]]


def pagina(indice: int = 0, escala: float = 4.0, lineas: list | None = None, size: dict | None = None) -> dict:
    return {
        "page": indice + 1,
        "scale": escala,
        "size": size if size is not None else {"width": 2382, "height": 3368},
        "lines": lineas if lineas is not None else [linea("TOTAL 100,00")],
    }


def linea(texto: str = "TOTAL 100,00", box: object = CAJA, score: object = 0.9) -> dict:
    fila: dict = {"text": texto}
    if box is not None:
        fila["box"] = box
    if score is not None:
        fila["score"] = score
    return fila


# ---------------------------------------------------------------------- #
# paginas_geo_desde_ocr
# ---------------------------------------------------------------------- #
def test_una_pagina_se_convierte_a_la_forma_de_la_cache():
    (entrada,) = paginas_geo_desde_ocr({"results": [pagina()]})
    assert entrada["pagina"] == 0
    assert entrada["escala"] == 4.0
    assert entrada["ancho"] == 2382.0
    assert entrada["alto"] == 3368.0
    (linea_geo,) = entrada["lineas"]
    assert linea_geo == {"texto": "TOTAL 100,00", "caja": [0.0, 0.0, 10.0, 10.0], "score": 0.9}


def test_el_poligono_del_ocr_se_reduce_a_la_caja_envolvente():
    box = [[30, 40], [90, 40], [90, 80], [30, 80]]
    (entrada,) = paginas_geo_desde_ocr({"results": [pagina(lineas=[linea(box=box)])]})
    assert entrada["lineas"][0]["caja"] == [30.0, 40.0, 90.0, 80.0]


def test_un_poligono_desordenado_sigue_dando_la_envolvente():
    box = [[90, 80], [30, 40], [30, 80], [90, 40]]
    (entrada,) = paginas_geo_desde_ocr({"results": [pagina(lineas=[linea(box=box)])]})
    assert entrada["lineas"][0]["caja"] == [30.0, 40.0, 90.0, 80.0]


def test_una_caja_plana_de_cuatro_numeros_se_acepta():
    (entrada,) = paginas_geo_desde_ocr({"results": [pagina(lineas=[linea(box=[1, 2, 3, 4])])]})
    assert entrada["lineas"][0]["caja"] == [1.0, 2.0, 3.0, 4.0]


def test_el_indice_de_pagina_es_el_del_array_no_el_del_ocr():
    """El OCR numera desde 1; el visor y el motor cuentan desde 0."""
    paginas = [pagina(0), pagina(1), pagina(2)]
    assert [p["pagina"] for p in paginas_geo_desde_ocr({"results": paginas})] == [0, 1, 2]


def test_sin_escala_la_pagina_se_descarta():
    """Una caja sin su escala no se puede pasar a puntos: no se puede pintar."""
    bruto = {"results": [pagina(escala=0), pagina(1, escala=2.0)]}
    paginas = paginas_geo_desde_ocr(bruto)
    assert len(paginas) == 1
    assert paginas[0]["escala"] == 2.0


@pytest.mark.parametrize("escala", [None, "4", [], 0, -1])
def test_escala_invalida_descarta_la_pagina(escala):
    bruto = {"results": [{**pagina(), "scale": escala}]}
    assert paginas_geo_desde_ocr(bruto) == []


def test_sin_tamano_de_pagina_la_geometria_sigue_sirviendo():
    """`ancho`/`alto` son informativos: la conversion solo necesita `escala`."""
    bruto = {"results": [pagina(size={})]}
    (entrada,) = paginas_geo_desde_ocr(bruto)
    assert "ancho" not in entrada and "alto" not in entrada
    assert entrada["escala"] == 4.0


def test_un_tamano_a_medias_no_se_cuela():
    bruto = {"results": [pagina(size={"width": 2382})]}
    (entrada,) = paginas_geo_desde_ocr(bruto)
    assert "ancho" not in entrada and "alto" not in entrada


@pytest.mark.parametrize("size", [{"width": 0, "height": 100}, {"width": 100, "height": 0}, {"width": -1, "height": -1}])
def test_un_tamano_no_positivo_no_se_escribe(size):
    """El validador exige escala y tamanos > 0; se filtra aqui para no escribir en balde."""
    (entrada,) = paginas_geo_desde_ocr({"results": [pagina(size=size)]})
    assert "ancho" not in entrada and "alto" not in entrada


def test_una_pagina_sin_lineas_no_aparece():
    """Una entrada vacia solo ensuciaria el documento."""
    assert paginas_geo_desde_ocr({"results": [pagina(lineas=[])]}) == []


def test_una_linea_sin_caja_se_descarta_pero_la_pagina_sobrevive():
    bruto = {"results": [pagina(lineas=[linea("sin caja", box=None), linea("con caja")])]}
    (entrada,) = paginas_geo_desde_ocr(bruto)
    assert [ln["texto"] for ln in entrada["lineas"]] == ["con caja"]


@pytest.mark.parametrize("box", [None, [], [1, 2], [1, 2, 3], [[1, 2], [3, 4]], "caja", 7])
def test_una_caja_ilegible_no_rompe_la_lectura(box):
    assert paginas_geo_desde_ocr({"results": [pagina(lineas=[linea(box=box)])]}) == []


@pytest.mark.parametrize("texto", [None, "", "   ", 42, ["TOTAL"]])
def test_una_linea_sin_texto_no_entra(texto):
    assert paginas_geo_desde_ocr({"results": [pagina(lineas=[linea(texto)])]}) == []


def test_el_score_fuera_de_rango_se_omite():
    bruto = {"results": [pagina(lineas=[linea(score=1.7)])]}
    (entrada,) = paginas_geo_desde_ocr(bruto)
    assert "score" not in entrada["lineas"][0]


def test_sin_score_la_linea_sigue_entrando():
    bruto = {"results": [pagina(lineas=[linea(score=None)])]}
    (entrada,) = paginas_geo_desde_ocr(bruto)
    assert "score" not in entrada["lineas"][0]


@pytest.mark.parametrize("bruto", [{}, {"results": None}, {"results": "x"}, {"lines": []}])
def test_un_payload_sin_geometria_devuelve_vacio(bruto):
    assert paginas_geo_desde_ocr(bruto) == []


def test_una_pagina_que_no_es_dict_se_salta():
    bruto = {"results": ["basura", None, pagina()]}
    (entrada,) = paginas_geo_desde_ocr(bruto)
    assert entrada["pagina"] == 2


def test_se_cortan_las_paginas_de_mas():
    bruto = {"results": [pagina(i) for i in range(MAX_PAGINAS_GEO + 5)]}
    assert len(paginas_geo_desde_ocr(bruto)) == MAX_PAGINAS_GEO


def test_el_motor_cloud_sin_cajas_no_produce_geometria():
    """`paddleocr_vl` no devuelve `box`: la factura se lee, pero no se resalta."""
    bruto = {"results": [{"page": 1, "scale": 1.0, "size": {"width": 10, "height": 10}, "lines": [{"text": "TOTAL"}]}]}
    assert paginas_geo_desde_ocr(bruto) == []


def test_la_geometria_no_toca_las_lineas_del_payload():
    original = pagina()
    copia = pagina()
    paginas_geo_desde_ocr({"results": [original]})
    assert original == copia


# ---------------------------------------------------------------------- #
# Convivencia con `lineas_desde_ocr`
# ---------------------------------------------------------------------- #
def test_las_dos_vistas_salen_del_mismo_payload():
    bruto = {"results": [pagina()]}
    lineas = lineas_desde_ocr(bruto)
    (entrada,) = paginas_geo_desde_ocr(bruto)
    assert lineas[0]["texto"] == entrada["lineas"][0]["texto"]
    assert lineas[0]["bbox"] == entrada["lineas"][0]["caja"]
    # Y solo la de geo conserva la escala.
    assert "escala" not in lineas[0]


def test_el_tope_de_lineas_es_el_mismo_en_las_dos_vistas():
    muchas = [linea(f"L{i}") for i in range(MAX_LINEAS_OCR + 10)]
    assert len(lineas_desde_ocr({"results": [pagina(lineas=muchas)]})) == MAX_LINEAS_OCR
    (entrada,) = paginas_geo_desde_ocr({"results": [pagina(lineas=muchas)]})
    assert len(entrada["lineas"]) == MAX_LINEAS_OCR


def test_la_geometria_guardada_es_la_que_entiende_el_resaltado():
    """El contrato real: lo que se escribe en Mongo lo tiene que poder leer el visor.

    Se pasa por JSON para imitar el viaje a BSON y vuelta (asi el `pagina` deja
    de ser el int de Python que se acaba de crear).
    """
    bruto = {"results": [pagina()]}
    guardado = json.loads(json.dumps(paginas_geo_desde_ocr(bruto)))
    (saneada,) = _sanea_geo(guardado)
    assert saneada["pagina"] == 0
    assert saneada["escala"] == 4.0
    assert [ln["texto"] for ln in saneada["lineas"]] == ["TOTAL 100,00"]
    assert saneada["lineas"][0]["caja"] == [0.0, 0.0, 10.0, 10.0]
