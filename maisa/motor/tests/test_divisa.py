"""Frontera entre la extraccion y la regla 7: las dos formas de marcar la divisa.

La regla de divisa vive en ``norma.py`` (R7, "el importe que se coteja viene en
la divisa del ERP") y se prueba entera en ``tests/test_motor_decision.py``,
seccion 8: que escala aunque los digitos cuadren, que una mencion en la nota no
escala, que ``EUR`` y ``€`` son la misma declaracion y que ``divisa_aceptada``
es politica y no codigo.

Lo que se fija aqui es el otro lado de la frontera: que hace la **extraccion**
con la marca de divisa. Importa porque de eso depende por que camino llega cada
factura a escalar, y son dos caminos distintos.

``normaliza._limpia_importe`` borra "USD" y los simbolos antes de convertir el
importe a ``Decimal``, asi que "930,20 USD" y "930,20 EUR" llegan al decisor
como el mismo ``930.20`` y el maestro no tiene columna de moneda. La marca solo
se puede leer del texto crudo, y R7 la lee **pegada al importe que se coteja**
(base, IVA o total), no en el documento entero: una nota que mencione otra
divisa no cambia la moneda en que se emitio la factura.

Las dos posiciones no son simetricas, y por eso se prueban juntas:

- ``TOTAL: 930,20 USD`` (marca detras): el importe se lee limpio y **solo** R7
  lo delata. Es el agujero que la regla existe para tapar.
- ``TOTAL: USD 930,20`` (marca delante): el patron de ``total`` no reconoce el
  codigo entre la etiqueta y el numero, el importe queda sin leer y la factura
  escala por aritmetica, sin que R7 llegue a decidir nada.
"""
from __future__ import annotations

from maisa import norma

from conftest import lee_texto

ESCALAR = norma.ESCALAR


def test_la_divisa_delante_del_importe_deja_el_importe_sin_leer(sintetica, mundo):
    """Marca delante: escala por lectura, no por la regla 7.

    Se deja escrito cual de los dos caminos cubre cada orden, para que nadie
    atribuya a R7 un escalado que en realidad viene de no haber podido leer el
    importe. La divisa si queda registrada: R7 lee las dos posiciones.
    """
    cuerpo = sintetica.texto(pedido=mundo.pedido_base).replace("TOTAL:", "TOTAL: USD")
    decision = mundo.decisor.decide(lee_texto(cuerpo))

    assert decision.resultado == ESCALAR
    assert decision.campos["total"] is None
    assert decision.campos["divisa_documento"] == ["USD"]
