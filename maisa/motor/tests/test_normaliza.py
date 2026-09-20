"""Normalizadores, cotejo de identificadores y reconstruccion de importes OCR.

Todo sintetico: no toca el corpus ni el OCR, corre en milisegundos.
"""
from __future__ import annotations

import dataclasses
from datetime import date
from decimal import Decimal

import pytest

from maisa import norma, normaliza as nz

from conftest import mundo_sintetico

# ----------------------------------------------------------- 8. normalizadores
@pytest.mark.parametrize("crudo, esperado", [
    ("b-46102331", "B46102331"),
    ("B46102331", "B46102331"),
    ("b 46102331", "B46102331"),
    ("A58231074S", "A58231074"),          # lector goloso: se corta a 9
    ("46102331", "46102331"),
    ("46.102.331", "46102331"),
    ("", ""),
    (None, ""),
])
def test_norm_nif(crudo, esperado):
    assert nz.norm_nif(crudo) == esperado


def test_norm_nif_no_repara_confusiones_de_ocr():
    """Norm no adivina: reparar O->0 es cosa de `repara_ocr`/`match_estricto`."""
    assert nz.norm_nif("B461O2331") == "B461O2331"


@pytest.mark.parametrize("crudo, esperado", [
    ("ES93 6888 4400 1235 8890 0142", "ES9368884400123588900142"),
    ("es91-2100-0418-4502-0513-3372", "ES9121000418450205133372"),
    ("ES9368884400123588900142CLIENTE", "ES9368884400123588900142"),  # corta a 24
    ("ES9368884400123588900142", "ES9368884400123588900142"),
    (None, ""),
])
def test_norm_iban(crudo, esperado):
    assert nz.norm_iban(crudo) == esperado


def test_norm_iban_no_corta_ibanes_de_otro_pais():
    """El corte es por longitud legal del pais; sin tabla conocida no se toca."""
    assert nz.norm_iban("DE89370400440532013000") == "DE89370400440532013000"


@pytest.mark.parametrize("crudo, esperado", [
    ("PO-2026-0096", "PO-2026-0096"),
    ("po 2026 96", "PO-2026-0096"),
    ("PO 2026 0096", "PO-2026-0096"),
    # Anio con un digito comido por el OCR: se conserva, no se repara aqui.
    ("PO-2028-0480", "PO-2028-0480"),
    ("PO-2026-0480", "PO-2026-0480"),
    # Referencia desnuda, sin prefijo: se recompone con el anio delante.
    ("2026-0718", "PO-2026-0718"),
    ("2026-718", "PO-2026-0718"),
    (None, ""),
])
def test_norm_pedido(crudo, esperado):
    assert nz.norm_pedido(crudo) == esperado


def test_norm_pedido_conserva_el_anio_ilegible():
    """La clave: no se "arregla" el anio a 2026, eso lo decide el ERP."""
    assert nz.norm_pedido("PO-2028-0480") == "PO-2028-0480"
    assert nz.norm_pedido("PO-2028-0480") != "PO-2026-0480"


@pytest.mark.parametrize("crudo, esperado", [
    ("12.874,40", Decimal("12874.40")),      # espanol
    ("930.20", Decimal("930.20")),           # ingles / miles ES ambiguo: decide la forma
    ("1.234,5", Decimal("1234.5")),
    ("1,234.56", Decimal("1234.56")),
    ("-12,50", Decimal("-12.50")),
    ("TOTAL: 3012,89 EUR", Decimal("3012.89")),
    ("3.012,89 \u20ac", Decimal("3012.89")),
    ("0,00", Decimal("0.00")),
    (1234.5, Decimal("1234.5")),
    (Decimal("9.99"), Decimal("9.99")),
    ("1.076.90", None),                      # tres "puntos": no es dinero valido
    ("sin numero", None),
    ("", None),
    (None, None),
    (True, None),
])
def test_a_decimal(crudo, esperado):
    assert nz.a_decimal(crudo) == esperado


@pytest.mark.parametrize("crudo, esperado", [
    ("19/09/2026", date(2026, 9, 19)),
    ("19-09-2026", date(2026, 9, 19)),
    ("2026-09-19", date(2026, 9, 19)),
    ("9/9/26", date(2026, 9, 9)),
    ("17 de mayo de 2026", date(2026, 5, 17)),
    ("31/02/2026", None),                    # fecha imposible
    ("hoy", None),
    ("", None),
    (None, None),
])
def test_a_fecha(crudo, esperado):
    assert nz.a_fecha(crudo) == esperado


def test_cuantiza_a_centimos():
    assert nz.cuantiza(Decimal("3012.894")) == Decimal("3012.89")
    assert nz.cuantiza(Decimal("3012.895")) == Decimal("3012.90")
    assert nz.cuantiza(Decimal("3012.891")) == Decimal("3012.89")
    # ROUND_HALF_UP aleja del cero, asi que en un negativo el empate baja:
    # -12.505 -> -12.51 (el banquero habria dejado -12.50).
    assert nz.cuantiza(Decimal("-12.505")) == Decimal("-12.51")
    assert nz.cuantiza(None) is None


def test_cuantiza_no_usa_el_redondeo_bancario():
    """Un empate exacto al centimo sube, no va al par.

    ``quantize()`` sin modo usa ROUND_HALF_EVEN, que en 2.385 redondea al par y
    da 2.38. En facturacion el empate sube: la base y el IVA de una factura
    tienen que poder sumar el total.
    """
    assert nz.cuantiza(Decimal("2.385")) == Decimal("2.39")
    assert nz.cuantiza(Decimal("2.375")) == Decimal("2.38")
    assert nz.cuantiza(Decimal("0.005")) == Decimal("0.01")


# ------------------------------------------- 9. match_estricto vs match_seguro
VOCAB = ["PO-2026-0006", "PO-2026-0096"]


def test_match_estricto_no_adivina_un_pedido_inexistente():
    """0,92 de parecido con PO-2026-0006 no basta: son pedidos distintos."""
    assert nz.similitud("PO-2026-0806", "PO-2026-0006") > 0.9
    assert nz.match_estricto("PO-2026-0806", VOCAB) is None


def test_match_estricto_si_repara_confusiones_de_ocr():
    assert nz.match_estricto("PO-2026-0096", VOCAB) == ("PO-2026-0096", "exacto")
    assert nz.match_estricto("PO-2026-O096", VOCAB) == ("PO-2026-0096", "reparado")


def test_match_seguro_hace_difuso_solo_a_igual_longitud():
    assert nz.match_seguro("PO-2026-0806", VOCAB, 0.85) == ("PO-2026-0006", "difuso 0.92")
    # Un caracter de menos (o de mas) cambia la longitud: no se admite difuso.
    assert nz.match_seguro("PO-2026-080", VOCAB, 0.80) is None
    assert nz.match_seguro("PO-2026-08061", VOCAB, 0.80) is None


def test_match_seguro_respeta_el_umbral():
    assert nz.match_seguro("PO-2026-0806", VOCAB, 0.95) is None


def test_match_seguro_en_ibanes_de_longitud_fija():
    ibanes = ["ES2100491500051234567890", "ES1800815290070001234567"]
    # Un caracter mal en un IBAN de 24 es error de lectura, no otra cuenta.
    assert nz.match_seguro("ES2100491500059234567890", ibanes, 0.95) == (
        "ES2100491500051234567890", "difuso 0.96")
    # Truncado (23 caracteres) no se admite como difuso.
    assert nz.match_seguro("ES210049150005923456789", ibanes, 0.95) is None
    assert nz.match_seguro("ES2100491500051234567890", ibanes, 0.95) == (
        "ES2100491500051234567890", "exacto")


# ---------------------------------------------- 10. candidatos_importe
@pytest.mark.parametrize("crudo, esperado", [
    ("52498", Decimal("524.98")),
    ("1.56438", Decimal("1564.38")),
    ("124084", Decimal("1240.84")),
    ("2385.80", Decimal("2385.80")),
    ("1.240.84", Decimal("1240.84")),
    ("1113.20", Decimal("1113.20")),
    ("1.113,20", Decimal("1113.20")),
])
def test_candidatos_importe_reconstruye_el_separador(crudo, esperado):
    assert esperado in nz.candidatos_importe(crudo)


def test_candidatos_importe_no_inventa_digitos():
    """Un descuadre real tiene otros digitos: ningun candidato coincide."""
    assert Decimal("2395.80") not in nz.candidatos_importe("2.385,80")
    assert Decimal("2395.80") not in nz.candidatos_importe("2385,80")
    assert nz.candidatos_importe("") == []


def test_descuadre_real_no_se_repara_en_el_motor(politica):
    """2.385,80 leido contra 2.395,80 en el ERP: escala, no se repara."""
    mundo = mundo_sintetico(politica)
    pedido = "PO-2026-0096"
    asiento = dataclasses.replace(mundo.asientos[pedido], importe=Decimal("2395.80"))
    asientos = dict(mundo.asientos) | {pedido: asiento}
    decisor = norma.Decisor(mundo.maestro, asientos, politica)

    def decide(total: Decimal):
        from conftest import Sintetica
        lectura = Sintetica(mundo).lectura(pedido=pedido, total=total,
                                           metodo="vision_ocr")
        return decisor.decide(lectura)

    malo = decide(Decimal("2385.80"))
    assert malo.resultado == norma.ESCALAR
    assert any("descuadrado" in m for m in malo.motivos)
    # Control positivo: el mismo desalineamiento con los digitos correctos si
    # se repara y se paga, para probar que lo que bloquea es el descuadre.
    bueno = decide(Decimal("2395.80"))
    assert bueno.resultado == norma.PAGAR
    assert bueno.campos["total"] in ("2395.80", "2395.8")