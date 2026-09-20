"""Guarda de divisa: el importe llega pelado y el maestro no guarda la moneda.

Dos mitades que se prueban juntas a proposito:

- **Deteccion** (``maisa.texto.divisa_extranjera``): el documento declara una
  divisa distinta del euro.
- **Consumo** (``maisa.norma``): esa declaracion escala. No se registra como
  evidencia ni se obedece: es una anomalia.

El motivo de que exista esta guarda: ``normaliza._limpia_importe`` borra la
divisa antes de convertir a decimal, asi que "USD 930.20" y "EUR 930.20" llegan
al decisor como el mismo ``930.20``, y el maestro no tiene columna de moneda
(``Importe_Total`` es un numero). Una factura en dolares se pagaria como si el
tipo de cambio no existiera, sin descuadre, sin NIF ajeno y sin IBAN raro que
mirar: solo un importe que "cuadra". Es el unico fallo del sistema que no deja
nada incoherente que detectar, y por eso se comprueba aparte.
"""
from __future__ import annotations

import re

import pytest

from maisa import norma, texto

from conftest import lee_texto

PAGAR = norma.PAGAR
ESCALAR = norma.ESCALAR


def algun_motivo(decision: norma.Decision, fragmento: str) -> bool:
    return any(fragmento in m for m in decision.motivos)


def con_divisa(cuerpo: str, marca: str) -> str:
    """Pone la divisa **detras** del importe, que es como la imprimen las facturas.

    La posicion no es un detalle del test, es el caso que hay que cubrir. Los
    patrones de `base`/`iva`/`total` admiten ``EUR`` o ``€`` entre la etiqueta y
    el numero, pero no otro codigo: con ``TOTAL: USD 930,20`` el importe **no se
    lee** y la factura escala sola, sin necesidad de esta guarda. El agujero es
    el otro orden -``TOTAL: 930,20 USD``, o la divisa en una cabecera, una linea
    de detalle o la nota-, donde el importe se lee limpio y nada registra que la
    moneda no es el euro.
    """
    return re.sub(r"(TOTAL:\s*[\d.,]+)", lambda m: f"{m.group(1)} {marca}", cuerpo)


# ------------------------------------------------------------- 1. deteccion
@pytest.mark.parametrize("cuerpo, esperado", [
    # El euro y sus formas son la moneda de casa: no son senal. Si lo fueran,
    # 297 de los 471 documentos con capa escalarian y el aviso no informaria.
    ("Importe total: EUR 930,20", []),
    ("Importe total: 930,20 \u20ac", []),
    ("Importe total: 930,20 euros", []),
    ("Importe total: 930,20", []),
    ("", []),
    # Codigos ISO y palabras: lo que `_limpia_importe` borra sin dejar rastro.
    ("Total: USD 930.20", ["USD"]),
    ("Total: 930.20 GBP", ["GBP"]),
    ("Total: 930.20 CHF", ["CHF"]),
    ("Total: 930.20 JPY", ["JPY"]),
    ("Total: 930.20 MXN", ["MXN"]),
    ("Total: 930.20 BRL", ["BRL"]),
    ("Total: 930,20 dolares", ["DOLARES"]),
    ("Total: 930,20 d\u00f3lares", ["DOLARES"]),
    ("Total: 500 libras esterlinas", ["LIBRAS"]),
    ("Total: 100 francos suizos", ["FRANCOS"]),
    ("Total: 100 yenes", ["YENES"]),
    ("Total: 100 reales", ["REALES"]),
    # Simbolos: solo los que no son el euro.
    ("Total: 930.20 $", ["USD"]),
    ("Total: 930.20 \u00a3", ["GBP"]),
    ("Total: 930.20 \u00a5", ["JPY"]),
    ("Total: 930.20 \u20b9", ["INR"]),
])
def test_deteccion_de_divisa(cuerpo, esperado):
    assert texto.divisa_extranjera(cuerpo) == esperado


def test_la_deteccion_no_depende_del_acento_del_emisor():
    """Cada emisor escribe "DOLARES" a su manera; el dato impreso es el mismo."""
    assert texto.divisa_extranjera("TOTAL: DOLARES 930.20") == ["DOLARES"]
    assert texto.divisa_extranjera("TOTAL: d\u00f3lares 930.20") == ["DOLARES"]


def test_el_campo_aparece_en_la_traza():
    """La traza tiene que poder mostrar por que escalo."""
    lectura = lee_texto("TOTAL: USD 930.20")
    assert lectura.divisa_extranjera == ["USD"]
    assert lectura.como_dict()["divisa_extranjera"] == ["USD"]


def test_una_factura_en_euros_no_declara_divisa():
    lectura = lee_texto("TOTAL: 930,20 \u20ac")
    assert lectura.divisa_extranjera == []


# -------------------------------------------------------------- 2. consumo
def test_factura_en_dolares_escala(sintetica, mundo):
    """El caso central: importe legible, otra moneda, ningun descuadre que lo delate."""
    cuerpo = con_divisa(sintetica.texto(pedido=mundo.pedido_base), "USD")
    decision = mundo.decisor.decide(lee_texto(cuerpo))

    assert decision.resultado == ESCALAR
    assert algun_motivo(decision, "no en euros")
    assert decision.campos["divisa_extranjera"] == ["USD"]
    nombres = [h.nombre for h in decision.hechos if not h.ok]
    assert "divisa_extranjera" in nombres


def test_la_divisa_no_se_registra_como_evidencia_sino_como_anomalia(sintetica, mundo):
    """A diferencia de una instruccion inyectada (ADR 4), esto no se ignora.

    Una instruccion dirigida al sistema se muestra y la factura se paga si esta
    limpia. Una divisa distinta no: no hay nada mas que comprobar que la delate.
    """
    cuerpo = con_divisa(sintetica.texto(pedido=mundo.pedido_base), "GBP")
    decision = mundo.decisor.decide(lee_texto(cuerpo))

    hecho = next(h for h in decision.hechos if h.nombre == "divisa_extranjera")
    assert hecho.informativo is False
    assert hecho.ok is False


def test_la_misma_factura_en_euros_se_paga(sintetica, mundo):
    """El contraste: sin la divisa, esa factura no tiene nada que objetar."""
    decision = sintetica.decide(pedido=mundo.pedido_base)
    assert decision.resultado == PAGAR
    assert decision.motivos == []
    assert decision.campos.get("divisa_extranjera") is None


def test_la_guarda_no_toca_los_importes(sintetica, mundo):
    """Escala por la moneda, no porque el importe se haya leido mal.

    Es lo que distingue este caso de un error de lectura: el importe esta bien,
    el pedido esta identificado y no hay desvio. Lo unico que no cuadra es la
    moneda en que esta expresado.
    """
    cuerpo = con_divisa(sintetica.texto(pedido=mundo.pedido_base), "USD")
    decision = mundo.decisor.decide(lee_texto(cuerpo))

    assert decision.campos["total"] == str(mundo.importe(mundo.pedido_base))
    assert decision.identificacion_fiable is True
    anomalias = [h for h in decision.hechos if not h.ok and not h.informativo]
    assert [h.nombre for h in anomalias] == ["divisa_extranjera"]


def test_el_simbolo_solo_tambien_escala(sintetica, mundo):
    """Sin codigo ISO ni palabra: el simbolo basta, porque tambien se borra."""
    cuerpo = con_divisa(sintetica.texto(pedido=mundo.pedido_base), "$")
    decision = mundo.decisor.decide(lee_texto(cuerpo))

    assert decision.resultado == ESCALAR
    assert decision.campos["divisa_extranjera"] == ["USD"]


def test_la_divisa_delante_del_importe_ya_escalaba_sola(sintetica, mundo):
    """Los dos caminos, para que quede escrito cual cubre la guarda y cual no.

    Con ``TOTAL: USD 930,20`` el patron no reconoce el codigo entre la etiqueta
    y el numero, el importe queda sin leer y la factura escala por aritmetica
    antes de que esta guarda entre. El fallo silencioso es el otro orden.
    """
    cuerpo = sintetica.texto(pedido=mundo.pedido_base).replace("TOTAL:", "TOTAL: USD")
    decision = mundo.decisor.decide(lee_texto(cuerpo))

    assert decision.resultado == ESCALAR
    assert decision.campos["total"] is None
