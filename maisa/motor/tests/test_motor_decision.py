"""Motor de decision: hechos duros, caso feliz, casos limite y herencia.

Todo aqui es sintetico (maestro y ERP en memoria) salvo donde se diga lo
contrario, asi que corre en milisegundos y no depende del corpus.
"""
from __future__ import annotations

import dataclasses
import re
from decimal import ROUND_HALF_UP, Decimal

import pytest

from maisa import norma

from conftest import AUTO, lee_texto

PAGAR = norma.PAGAR
ESCALAR = norma.ESCALAR
NO_PAGAR = norma.NO_PAGAR


def duros(decision: norma.Decision) -> list[norma.Hecho]:
    return [h for h in decision.hechos if h.duro]


def algun_motivo(decision: norma.Decision, fragmento: str) -> bool:
    return any(fragmento in m for m in decision.motivos)


# ------------------------------------------------------------ 2. caso feliz
def test_factura_limpia_se_paga_sin_motivos(sintetica, mundo):
    decision = sintetica.decide(pedido=mundo.pedido_base)
    assert decision.resultado == PAGAR
    assert decision.motivos == []
    assert decision.identificacion_fiable is True
    assert decision.campos["pedido"] == mundo.pedido_base
    assert decision.campos["estado_erp"] == "PENDIENTE"
    assert not duros(decision)


def test_el_cif_del_destinatario_no_identifica_al_emisor(sintetica, mundo):
    """La factura lleva el CIF del cliente; no debe leerse como NIF del emisor."""
    lectura = sintetica.lectura(pedido=mundo.pedido_base)
    assert lectura.valores("nif") == [mundo.nif(mundo.pedido_base)]


# ------------------------------------------------- 1. hechos duros / NO_PAGAR
def test_pedido_pagado_en_el_erp_no_se_paga(sintetica, mundo):
    pedido = "PO-2026-0803"
    assert mundo.estado(pedido) == "PAGADA"
    decision = sintetica.decide(pedido=pedido)
    assert decision.resultado == NO_PAGAR
    assert algun_motivo(decision, "ya esta PAGADA en el ERP")
    assert [h.nombre for h in duros(decision)] == ["pago_duplicado"]


def test_un_hecho_duro_no_manda_si_no_se_sabe_que_pedido_es(sintetica, mundo):
    """Un pago ya hecho solo autoriza NO_PAGAR si el pedido es exacto.

    Aqui el OCR se come el anio: el pedido se repara al PAGADA del ERP, pero la
    identificacion no es literal, asi que se escala en vez de no pagar.
    """
    pedido = "PO-2026-0803"
    decision = sintetica.decide(
        pedido="PO-2028-0803", total=mundo.importe(pedido), metodo="vision_ocr",
        nif=mundo.nif(pedido), iban=mundo.iban(pedido),
    )
    assert decision.campos["pedido"] == pedido
    assert decision.identificacion_fiable is False
    assert [h.nombre for h in duros(decision)] == ["pago_duplicado"]
    assert decision.resultado == ESCALAR


def test_el_no_pagar_gana_al_escalar(sintetica, mundo):
    """Precedencia declarada: un pago ya hecho pesa mas que un desvio de IBAN."""
    decision = sintetica.decide(pedido="PO-2026-0803", iban=mundo.iban("PO-2026-0096"))
    assert decision.resultado == NO_PAGAR
    assert algun_motivo(decision, "IBAN de abono distinto")  # el desvio tambien se traza


# ------------------------------------------------- 3. pedido inexistente
@pytest.mark.parametrize("pedido", ["PO-2026-9999", "PO-2025-0096", "PO-2026-0000"])
def test_pedido_inexistente_escala(sintetica, mundo, pedido):
    decision = sintetica.decide(pedido=pedido, total=Decimal("999.99"),
                                nif=mundo.nif(mundo.pedido_base),
                                iban=mundo.iban(mundo.pedido_base))
    assert decision.resultado == ESCALAR
    assert decision.identificacion_fiable is False
    assert algun_motivo(decision, "pedido no identificable")
    assert any("no existe en el ERP" in n for n in decision.campos["notas"])
    assert not duros(decision)


def test_pedido_inexistente_no_se_corrige_a_lo_difuso(sintetica, mundo):
    """PO-2026-0806 se parece un 0,92 a PO-2026-0096... pero no es el mismo.

    El mundo de prueba tiene PO-2026-0096 y PO-2026-0806 no existe: una
    correccion difusa convertiria un pedido inventado en uno real.
    """
    decision = sintetica.decide(pedido="PO-2026-0806", total=Decimal("999.99"),
                                nif=mundo.nif(mundo.pedido_base),
                                iban=mundo.iban(mundo.pedido_base))
    assert decision.resultado != PAGAR
    assert "PO-2026-0806" in decision.campos["pedido_candidatos"]


def test_pedido_ambiguo_escala(sintetica, mundo):
    """Dos pedidos distintos citados en el mismo documento: no se elige uno."""
    cuerpo = sintetica.lectura(pedido=mundo.pedido_base).texto.replace(
        "Pedido: " + mundo.pedido_base,
        "Pedido: " + mundo.pedido_base + "\nPedido: PO-2026-0492",
    )
    decision = mundo.decisor.decide(lee_texto(cuerpo))
    assert decision.resultado == ESCALAR
    assert algun_motivo(decision, "pedido ambiguo")


# ------------------------------------------------- 4. importe descuadrado
def test_importe_descuadrado_escala(sintetica, mundo):
    """1234,50 impreso contra 1200,00 en el ERP."""
    decision = sintetica.decide(pedido="PO-2026-0814", total=Decimal("1234.50"),
                                base=Decimal("1020.25"), iva=Decimal("214.25"))
    assert mundo.importe("PO-2026-0814") == Decimal("1200.00")
    assert decision.resultado == ESCALAR
    assert algun_motivo(decision, "importe descuadrado")


def test_importe_cuadrado_al_centimo_se_paga(sintetica, mundo):
    """La tolerancia declarada es de 0,01: el centimo exacto cuadra."""
    decision = sintetica.decide(pedido=mundo.pedido_base)
    assert decision.campos["desvio_importe"] == "0.00"
    assert decision.resultado == PAGAR


def test_aritmetica_incoherente_escala(sintetica, mundo):
    """Total igual al del pedido pero base+IVA que no suma: defecto real."""
    decision = sintetica.decide(pedido=mundo.pedido_base, base=Decimal("2000.00"),
                                iva=Decimal("100.00"), total=mundo.importe(mundo.pedido_base))
    assert decision.resultado == ESCALAR
    assert algun_motivo(decision, "aritmetica incoherente")


def test_total_ilegible_escala(sintetica, mundo):
    decision = sintetica.decide(pedido=mundo.pedido_base, total=Decimal("0.00"))
    assert decision.resultado == ESCALAR
    assert algun_motivo(decision, "total de factura no legible")


def test_el_total_bien_leido_no_se_anota_como_recompuesto(sintetica, mundo):
    """Un escaneo cuyo total ya cuadra con el ERP no tiene nada que reconstruir.

    `_repara_importes_ocr` acepta el importe por la via `por_total` tambien
    cuando el OCR lo leyo **bien** (basta con que cuadre con el ERP), y en ese
    caso anotaba una reparacion que no existia: el mismo importe a los dos lados
    de los dos puntos. Inflaba el censo de error de extraccion -- 19 de las 25
    notas de "importe recompuesto" del lote eran de este tipo -- y ademas
    acusaba al OCR de desalinear un separador que habia leido correctamente.
    """
    decision = sintetica.decide(metodo="vision_local")
    assert decision.resultado == PAGAR
    assert decision.campos["total"] == str(mundo.importe(mundo.pedido_base))
    assert [n for n in decision.campos["notas_importe"] if "recompuesto" in n] == []


def test_el_total_con_el_separador_desalineado_si_deja_nota(sintetica, mundo):
    """El caso que la nota describe de verdad: `3.012,89` leido `3.012.89`.

    La reparacion (y por tanto la nota) sigue viva: lo que cambia es que ahora
    solo se anota cuando el importe cambia.
    """
    cuerpo = sintetica.texto().replace("3.012,89", "3.012.89")
    decision = mundo.decisor.decide(lee_texto(cuerpo, metodo="vision_local"))
    assert decision.resultado == PAGAR
    assert decision.campos["total"] == str(mundo.importe(mundo.pedido_base))
    notas = decision.campos["notas_importe"]
    assert any(n.startswith("importe recompuesto a 3012.89") for n in notas)


# ------------------------------------------------------- 5. fecha
def test_fecha_futura_escala(sintetica, mundo, politica):
    assert politica.hoy == "2026-09-19"
    decision = sintetica.decide(pedido=mundo.pedido_base, fecha="01/01/2027")
    assert decision.resultado == ESCALAR
    assert algun_motivo(decision, "fecha futura")


def test_fecha_de_hoy_no_es_futura(sintetica, mundo, politica):
    decision = sintetica.decide(pedido=mundo.pedido_base, fecha="19/09/2026")
    assert decision.campos["fecha"] == politica.hoy
    assert decision.resultado == PAGAR


def test_fecha_imposible_escala(sintetica, mundo):
    decision = sintetica.decide(pedido=mundo.pedido_base, fecha="31/02/2026")
    assert decision.resultado == ESCALAR
    assert algun_motivo(decision, "fecha invalida")


def test_fecha_ilegible_no_escala_si_la_factura_esta_probada(sintetica, mundo):
    """El pedido literal y el importe cuadrado prueban la factura: la fecha no.

    Que la fecha no se lea no impide pagar: la fecha no autoriza el importe ni
    elige al proveedor, asi que no es un hecho de peso (Norma, v3.3). El hecho
    se sigue registrando, pero como evidencia: no cierra la decision.
    """
    decision = sintetica.decide(pedido=mundo.pedido_base, fecha="")
    assert decision.resultado == PAGAR
    assert not algun_motivo(decision, "fecha no legible")
    ilegible = [h for h in decision.hechos if h.regla == "R4_fecha" and not h.ok]
    assert ilegible and ilegible[0].informativo is True


def test_fecha_ilegible_escala_si_la_factura_no_esta_probada(sintetica, mundo):
    """Sin el anclaje, la fecha ilegible vuelve a escalar.

    Aqui el pedido del documento no es literal (el OCR se come el anio), asi que
    no hay dos pruebas independientes de que sepamos de que factura hablamos.
    """
    decision = sintetica.decide(
        pedido="PO-2028-0096", total=mundo.importe(mundo.pedido_base), fecha="",
        nif=mundo.nif(mundo.pedido_base), iban=mundo.iban(mundo.pedido_base),
    )
    assert decision.resultado == ESCALAR
    assert algun_motivo(decision, "fecha no legible")


# ------------------------------------------------------- 6. IBAN desviado
def test_iban_de_otra_cuenta_escala(sintetica, mundo):
    """Mismo proveedor en el maestro, pero la cuenta de abono es otra."""
    decision = sintetica.decide(pedido=mundo.pedido_base, iban=mundo.iban("PO-2026-0492"))
    assert decision.resultado == ESCALAR
    assert algun_motivo(decision, "IBAN de abono distinto")


def test_iban_ilegible_escala(sintetica, mundo):
    decision = sintetica.decide(pedido=mundo.pedido_base, iban=None)
    assert decision.resultado == ESCALAR
    assert algun_motivo(decision, "IBAN no legible")


def test_una_letra_mal_en_el_iban_se_corrige(sintetica, mundo):
    """Una sustitucion en un identificador de longitud fija es un error de OCR."""
    iban = mundo.iban(mundo.pedido_base)
    decision = sintetica.decide(pedido=mundo.pedido_base, iban=iban[:14] + "9" + iban[15:])
    assert decision.resultado == PAGAR
    assert decision.campos["iban_candidatos"] == [iban]


def test_la_correccion_difusa_de_iban_deja_nota(sintetica, mundo):
    iban = mundo.iban(mundo.pedido_base)
    decision = sintetica.decide(pedido=mundo.pedido_base, iban=iban[:14] + "9" + iban[15:])
    assert any("corregido a" in n for n in decision.campos["notas"])


def test_la_correccion_de_un_nif_tambien_deja_nota(sintetica, mundo):
    """El censo de error de extraccion cuenta las reparaciones por `campos['notas']`.

    Si R1 vuelve a calcular sus notas y tirarlas, el censo diria que no hubo
    ninguna correccion de NIF: una metrica que miente por lo bajo.
    """
    nif = mundo.nif(mundo.pedido_base)
    decision = sintetica.decide(pedido=mundo.pedido_base, nif=nif[:-1] + "I")
    assert decision.resultado == PAGAR
    assert any("NIF" in n and "corregido a" in n for n in decision.campos["notas"])


def test_las_notas_de_r1_no_pisan_las_del_pedido(sintetica, mundo):
    """`campos['notas']` acumula: la nota del NIF no puede borrar la del pedido."""
    nif = mundo.nif(mundo.pedido_base)
    decision = sintetica.decide(
        metodo="vision_local",
        pedido="PO-2028-0096",  # anio que no existe; el cuerpo (0096) lo identifica
        total=mundo.importe(mundo.pedido_base),
        nif=nif[:-1] + "I",
        iban=mundo.iban(mundo.pedido_base),
    )
    notas = decision.campos["notas"]
    assert any(n.startswith("pedido") and "reparado" in n for n in notas)
    assert any("NIF" in n and "corregido a" in n for n in notas)


# ------------------------------------------------------- 7. NIF ajeno
def test_nif_de_otro_proveedor_del_maestro_escala(sintetica, mundo):
    decision = sintetica.decide(pedido=mundo.pedido_base, nif=mundo.nif("PO-2026-0492"))
    assert decision.resultado == ESCALAR
    assert algun_motivo(decision, "pertenece a otro proveedor del maestro")
    assert decision.campos["nif_maestro"] == mundo.nif(mundo.pedido_base)


def test_nif_desconocido_escala(sintetica, mundo):
    decision = sintetica.decide(pedido=mundo.pedido_base, nif="B99999999")
    assert decision.resultado == ESCALAR
    assert algun_motivo(decision, "desconocido en el maestro")


def test_nif_ilegible_escala(sintetica, mundo):
    decision = sintetica.decide(pedido=mundo.pedido_base, nif=None)
    assert decision.resultado == ESCALAR
    assert algun_motivo(decision, "NIF del emisor no legible")


# ------------------------------------------- 12. herencia de identidad (OCR)
@pytest.mark.parametrize("sin_nif,sin_iban", [(True, False), (False, True), (True, True)])
def test_ocr_hereda_la_identidad_del_erp(sintetica, mundo, sin_nif, sin_iban):
    """Pedido exacto + importe confirmado: el campo ilegible se hereda.

    Solo con lectura de vision: un NIF que falta en una capa de texto exacta es
    un dato que falta, no un escaneo sucio.

    Un IBAN ilegible es distinto: rellenar el hueco no es comprobarlo (Norma,
    punto 1), asi que la identidad se hereda pero la factura escala por R6
    (`si_documento_no_legible`). El NIF si se puede heredar sin escalar: el
    asiento del ERP lo confirma con el pedido y el importe exactos.
    """
    decision = sintetica.decide(
        pedido=mundo.pedido_base, metodo="vision_ocr",
        nif=None if sin_nif else AUTO, iban=None if sin_iban else AUTO,
    )
    assert decision.campos["identidad_heredada"] is True
    if sin_iban:
        assert decision.resultado == ESCALAR
        assert algun_motivo(decision, "documento no legible")
    else:
        assert decision.resultado == PAGAR
        assert decision.motivos == []
        assert not algun_motivo(decision, "no legible")


def test_el_escaneo_sin_nif_ni_fecha_paga_si_el_iban_se_lee(sintetica, mundo):
    """El caso `scan_021.pdf`: IBAN legible, NIF y fecha ilegibles -> PAGAR.

    Es el **unico** desacuerdo de riesgo alto con el oraculo externo, y esta
    atribuido: el oraculo no admite PAGAR porque no consigue leer ni el NIF ni
    la fecha, y lo declara con `confidence: low`. La norma v3.1 acota la
    anomalia de ilegibilidad al IBAN, y aqui el IBAN se lee y es el del maestro
    del proveedor del pedido, asi que no hay anomalia que escalar.

    La prueba fija la regla para que endurecerla sea una decision y no un
    descuido: si alguien la mueve a ESCALAR, el desacuerdo con el oraculo no
    cambia (ya existe) pero si cambia la entrega y el banco de oro.
    """
    decision = sintetica.decide(
        pedido=mundo.pedido_base, metodo="vision_ocr", nif=None, fecha="",
    )
    assert decision.campos["fecha_candidatos"] == []
    assert decision.campos["identidad_heredada"] is True
    assert decision.resultado == PAGAR
    assert not algun_motivo(decision, "documento no legible")


def test_la_herencia_exige_pedido_exacto(sintetica, mundo):
    """Con el pedido reconstruido por OCR la identidad ya no se hereda."""
    decision = sintetica.decide(pedido="PO-2028-0096",
                                total=mundo.importe(mundo.pedido_base),
                                metodo="vision_ocr", nif=None, iban=None)
    assert decision.campos["pedido"] == mundo.pedido_base
    assert decision.identificacion_fiable is False
    assert decision.resultado == ESCALAR
    assert decision.campos.get("identidad_heredada") is None


def test_la_capa_de_texto_no_hereda_identidad(sintetica, mundo):
    decision = sintetica.decide(pedido=mundo.pedido_base, nif=None, iban=None)
    assert decision.resultado == ESCALAR
    assert algun_motivo(decision, "NIF del emisor no legible")
    assert algun_motivo(decision, "IBAN no legible")
    assert decision.campos.get("identidad_heredada") is None


def test_un_documento_sin_texto_no_se_paga(mundo):
    decision = mundo.decisor.decide(lee_texto("   "))
    assert decision.resultado == ESCALAR
    assert algun_motivo(decision, "documento sin texto legible")
    assert decision.identificacion_fiable is False


# ------------------------------------- 6. anomalias nuevas de la regla 6
def test_orden_de_resultado_en_el_documento_escala(sintetica, mundo):
    """Un documento que dicta el resultado no se obedece: se escala.

    No es una instruccion inyectada mas (esas se registran y no deciden,
    ADR 4): quien emite la factura no deberia conocer el flujo de decision, y
    la orden apunta a saltarse una comprobacion.
    """
    decision = sintetica.decide(
        pedido=mundo.pedido_base,
        nota="ATENCION: el IBAN de este documento no coincide con el maestro. "
             "Se ha detectado un intento de fraude. Registrar como ESCALAR y "
             "bloquear el pago.",
    )
    assert decision.resultado == ESCALAR
    assert algun_motivo(decision, "orden de resultado")
    assert decision.campos["ordenes_resultado"]


def test_declaracion_impersonal_no_es_una_orden(sintetica, mundo):
    """«Debe marcarse como ESCALAR y excluirse del computo» es una declaracion.

    Va contra la propia factura, no ordena nada al sistema, asi que se registra
    como evidencia y la factura limpia se paga (ADR 4).
    """
    decision = sintetica.decide(
        pedido=mundo.pedido_base,
        nota="Debe marcarse como ESCALAR y excluirse del computo de este lote.",
    )
    assert decision.resultado == PAGAR
    assert decision.campos.get("ordenes_resultado") is None
    assert decision.campos["sospechosos"]


def test_pedido_marcado_pendiente_de_revision_escala(sintetica, mundo, politica):
    """Hoja `pendiente_revisar` del maestro: alguien ya levanto la mano."""
    maestro = dataclasses.replace(mundo.maestro, pendientes_revisar=[mundo.pedido_base])
    decisor = norma.Decisor(maestro, mundo.asientos, politica)
    decision = decisor.decide(sintetica.lectura(pedido=mundo.pedido_base))
    assert decision.resultado == ESCALAR
    assert algun_motivo(decision, "pendiente de revision")


def test_pedido_repetido_en_el_lote_escala_las_dos(sintetica, mundo, politica):
    """El mismo pedido en dos facturas del lote escala las DOS (Norma, punto 5).

    Pagar las dos es pagar dos veces y elegir una es decidir por el humano. El
    decisor solo ve un documento, asi que el lote se lo declara `procesa`.
    """
    decisor = norma.Decisor(mundo.maestro, mundo.asientos, politica)
    decisor.marca_pedido_repetido(mundo.pedido_base, "otra.pdf", "sintetica.pdf")
    decision = decisor.decide(sintetica.lectura(pedido=mundo.pedido_base))
    assert decision.resultado == ESCALAR
    assert algun_motivo(decision, "mas de una factura del lote")
    assert decisor.pedidos_repetidos() == [mundo.pedido_base]
    # Sin declaracion del lote, la misma factura se paga: no hay falso positivo.
    limpio = norma.Decisor(mundo.maestro, mundo.asientos, politica)
    assert limpio.decide(sintetica.lectura(pedido=mundo.pedido_base)).resultado == PAGAR


def test_los_caracteres_invisibles_no_parten_los_campos(sintetica, mundo):
    """Un IBAN o un total con anchura cero (U+200B) se leen enteros.

    Los emisores los meten para colar un dato distinto al ojo humano; `NFKC` no
    los borra y `\\s` tampoco los reconoce, asi que el IBAN no se reconocia y el
    total "2.637,80" se leia como "2".
    """
    iban = mundo.iban(mundo.pedido_base)
    cuerpo = sintetica.texto(pedido=mundo.pedido_base)
    cuerpo = re.sub(r"^IBAN:.*$", "IBAN: " + "\u200b".join(iban), cuerpo, flags=re.M)
    cuerpo = re.sub(r"^TOTAL: (.+)$", lambda m: "TOTAL: " + "\u200b".join(m.group(1)),
                    cuerpo, flags=re.M)
    lectura = lee_texto(cuerpo)
    assert lectura.valores("iban") == [iban]
    assert lectura.valores("total") == [str(mundo.importe(mundo.pedido_base))]
    assert mundo.decisor.decide(lectura).resultado == PAGAR


def test_la_etiqueta_y_su_importe_pueden_ir_en_lineas_distintas(sintetica, mundo):
    """El OCR de vision imprime la etiqueta y su importe en lineas distintas.

    `_normaliza_espacios` colapsa los espacios pero **respeta** los saltos de
    linea, asi que los separadores de base, IVA y total tienen que admitir
    `\\n`. Sin ellos el importe no se leia y el decisor no podia contrastarlo
    contra el ERP: cinco de los 29 escaneos del corpus escalaban solo por esto,
    pese a ser facturas limpias con NIF, IBAN y total legibles.
    """
    total = mundo.importe(mundo.pedido_base)
    base = (total / Decimal("1.21")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    cuerpo = sintetica.texto(pedido=mundo.pedido_base)
    cuerpo = re.sub(r"^(Base|IVA \(21%\)|TOTAL): (.+)$", r"\1:\n\2", cuerpo, flags=re.M)
    # Sin esto la prueba pasaria aunque el `re.sub` no hubiera casado nada.
    assert "Base:\n" in cuerpo and "IVA (21%):\n" in cuerpo and "TOTAL:\n" in cuerpo

    lectura = lee_texto(cuerpo, metodo="vision_ocr")
    assert lectura.valores("base") == [str(base)]
    assert lectura.valores("iva") == [str(total - base)]
    assert lectura.valores("total") == [str(total)]
    assert mundo.decisor.decide(lectura).resultado == PAGAR


def test_la_politica_de_la_regla_6_esta_declarada(politica):
    """Las anomalias de la regla 6 se nombran en el TOML, no en el codigo.

    El fallback que se pasa aqui es PAGAR a proposito: si una sub-clave no
    estuviera declarada, la prueba fallaria en vez de dar por buena la politica
    por defecto de la regla.
    """
    for clave in ("si_pedido_repetido", "si_instruccion", "si_pendiente_revision",
                  "si_documento_no_legible"):
        assert politica.politica_de("R6_anomalia", clave, PAGAR) == ESCALAR


# ------------------------------------------------------- 8. divisa (regla 7)
def _marca_divisa(cuerpo: str, marca: str) -> str:
    """Pega `marca` a los tres importes que se cotejan con el ERP."""
    return re.sub(r"^(Base|IVA \(21%\)|TOTAL): (.+)$", rf"\1: \2 {marca}", cuerpo,
                  flags=re.M)


def test_la_politica_de_la_regla_7_esta_declarada(politica):
    """La regla 7 es politica, no codigo. Fallback PAGAR a proposito: si la
    sub-clave faltara, la prueba falla en vez de colar la politica por defecto.
    """
    assert politica.divisa_aceptada == "EUR"
    for clave in ("si_falla", "si_divisa_distinta"):
        assert politica.politica_de("R7_divisa", clave, PAGAR) == ESCALAR


def test_la_factura_en_dolares_escala_aunque_los_digitos_cuadren(sintetica, mundo):
    """El fallo mas peligroso: un importe de 3.012,89 en dolares "cuadra".

    `normaliza._limpia_importe` borra la marca de divisa, asi que el motor
    compara un `Decimal` contra otro y no ve la diferencia: pagar 3.012,89 EUR
    por una factura de 3.012,89 USD es pagar de menos. La unica defensa es leer
    la marca del texto CRUDO (antes de normalizar) y escalar.
    """
    cuerpo = _marca_divisa(sintetica.texto(pedido=mundo.pedido_base), "USD")
    assert "TOTAL: 3.012,89 USD" in cuerpo
    decision = mundo.decisor.decide(lee_texto(cuerpo))

    assert decision.resultado == ESCALAR
    # El motivo nombra las dos unidades: `campos['importe_erp']` es un decimal
    # pelado, asi que sin decirlo aqui el revisor leeria "3012.89" y "3012.89".
    assert algun_motivo(decision, "divisa distinta")
    assert algun_motivo(decision, "3012.89 USD")
    assert algun_motivo(decision, "3012.89 EUR")
    assert decision.campos["divisa_documento"] == ["USD"]
    assert decision.campos["divisa_erp"] == "EUR"
    assert [h.nombre for h in decision.hechos if h.regla == "R7_divisa"] == \
        ["si_divisa_distinta"]
    # Escalar no es un hecho duro: no autoriza a NO_PAGAR.
    assert not duros(decision)


def test_una_mencion_a_otra_divisa_en_la_nota_no_escala(sintetica, mundo):
    """La divisa se lee **pegada al importe**, no en el documento entero.

    Una factura en euros que solo menciona dolares en las condiciones de pago
    es una factura en euros. Escalarla seria un falso positivo sobre un
    documento correcto, que es como se pierde la confianza en la regla.
    """
    decision = sintetica.decide(
        pedido=mundo.pedido_base,
        nota="Equipo valorado en 500 USD segun el proveedor del componente.",
    )
    assert decision.campos["divisa_documento"] == []
    assert decision.resultado == PAGAR
    assert not algun_motivo(decision, "divisa")


def test_la_divisa_impresa_se_publica_aunque_coincida(sintetica, mundo):
    """Ausencia de marca y marca en euros son cosas distintas, y se distinguen.

    Guardarlo permite decir "no lo declara" en vez de suponer euros: es lo que
    hara que el dia que el proveedor empiece a emitir en libras se note.
    """
    limpia = sintetica.decide(pedido=mundo.pedido_base)
    assert limpia.campos["divisa_documento"] == []

    con_marca = mundo.decisor.decide(lee_texto(_marca_divisa(
        sintetica.texto(pedido=mundo.pedido_base), "EUR")))
    assert con_marca.campos["divisa_documento"] == ["EUR"]
    assert con_marca.resultado == PAGAR


@pytest.mark.parametrize("marca", ["EUR", "€"])
def test_la_marca_en_euros_paga_escrita_como_codigo_o_como_simbolo(
        sintetica, mundo, marca):
    """`EUR` y `€` son la misma declaracion, y ninguna es una anomalia.

    El simbolo no puede ser un hueco: la mitad de las facturas reales escriben
    `€` y no `EUR`, y si el simbolo no se leyera, esas facturas se pagarian a
    ciegas. Que las dos formas acaben en `["EUR"]` es lo que garantiza que la
    tabla de tres estados no tenga un cuarto estado silencioso.
    """
    con_marca = mundo.decisor.decide(lee_texto(_marca_divisa(
        sintetica.texto(pedido=mundo.pedido_base), marca)))
    assert con_marca.campos["divisa_documento"] == ["EUR"]
    assert con_marca.resultado == PAGAR
    assert not algun_motivo(con_marca, "divisa")


def test_el_simbolo_del_dolar_escala_igual_que_el_codigo(sintetica, mundo):
    """El simbolo tambien declara: `$` es USD, y USD no es la divisa del ERP."""
    con_marca = mundo.decisor.decide(lee_texto(_marca_divisa(
        sintetica.texto(pedido=mundo.pedido_base), "$")))
    assert con_marca.campos["divisa_documento"] == ["USD"]
    assert con_marca.resultado == ESCALAR
    assert algun_motivo(con_marca, "divisa distinta")


def test_un_escaneo_en_divisa_ajena_no_se_recompone_con_el_importe_del_erp(
        sintetica, mundo):
    """La puerta de atras del OCR: `_repara_importes_ocr` devuelve `esperado`.

    Cuando el OCR desalinea el separador decimal, la reparacion acepta el
    importe del ERP como bueno. Con el documento en otra divisa eso sustituye
    la cifra que el proveedor imprimio por una que no es la suya, y la factura
    sale PAGAR con el "arreglo" tapando el problema.
    """
    cuerpo = _marca_divisa(sintetica.texto(pedido=mundo.pedido_base), "USD")
    cuerpo = cuerpo.replace("3.012,89", "3.012.89")  # separador que el OCR movio
    decision = mundo.decisor.decide(lee_texto(cuerpo, metodo="vision_local"))

    assert decision.campos["notas_importe"] == []
    assert decision.resultado == ESCALAR
    assert algun_motivo(decision, "divisa distinta")


def test_la_divisa_aceptada_es_politica_y_no_codigo(sintetica, mundo, politica):
    """Invertir el umbral basta: el mismo documento, la misma factura, PAGAR."""
    en_dolares = dataclasses.replace(politica, divisa_aceptada="USD")
    decisor = norma.Decisor(mundo.maestro, mundo.asientos, en_dolares)
    cuerpo = _marca_divisa(sintetica.texto(pedido=mundo.pedido_base), "USD")
    decision = decisor.decide(lee_texto(cuerpo))
    assert decision.resultado == PAGAR
    assert decision.campos["divisa_documento"] == ["USD"]
    assert decision.campos["divisa_erp"] == "USD"


# ------------------------- 13. campos ilegibles que no cierran la decision (v3.3)
def test_un_total_ilegible_se_reconstruye_por_la_aritmetica(sintetica, mundo):
    """Sin el TOTAL impreso, base + IVA lo calculan y el ERP lo confirma.

    Dos vias independientes --la aritmetica del papel y el asiento-- dicen el
    mismo importe, asi que no queda nada que un humano pueda aportar. La nota
    deja constancia de que el total se reconstruyo en vez de leerse.
    """
    cuerpo = "\n".join(
        linea for linea in sintetica.texto(pedido=mundo.pedido_base).splitlines()
        if not linea.startswith("TOTAL:")
    )
    decision = mundo.decisor.decide(lee_texto(cuerpo))

    assert decision.resultado == PAGAR
    assert decision.campos["total"] == str(mundo.importe(mundo.pedido_base))
    assert any("reconstruido" in n for n in decision.campos["notas_importe"])


def test_un_total_ilegible_no_se_inventa_si_la_suma_no_cuadra(sintetica, mundo):
    """Si base + IVA no da el importe del pedido, el total sigue ilegible.

    La reconstruccion no es una licencia para inventar el importe: sin la
    confirmacion del ERP no hay nada probado y la factura escala.
    """
    cuerpo = "\n".join(
        linea for linea in sintetica.texto(pedido=mundo.pedido_base).splitlines()
        if not linea.startswith("TOTAL:")
    ).replace("Base: 2.489,99", "Base: 2.000,00")
    decision = mundo.decisor.decide(lee_texto(cuerpo))

    assert decision.resultado == ESCALAR
    assert decision.campos["total"] is None
    assert decision.campos["notas_importe"] == []
    assert algun_motivo(decision, "total de factura no legible")


def test_base_e_iva_ilegibles_no_escalan_si_el_total_cuadra(sintetica, mundo):
    """El total impreso y cuadrado con el ERP prueba el importe a pagar.

    Que falten la base y el IVA impresos deja un hecho registrado, pero no
    cierra la decision: el dato que esos campos venian a probar --cuanto se
    paga-- ya esta probado.
    """
    cuerpo = "\n".join(
        linea for linea in sintetica.texto(pedido=mundo.pedido_base).splitlines()
        if not linea.startswith(("Base:", "IVA ("))
    )
    decision = mundo.decisor.decide(lee_texto(cuerpo))

    assert decision.resultado == PAGAR
    assert decision.campos["base"] is None and decision.campos["iva"] is None
    assert decision.campos["total"] == str(mundo.importe(mundo.pedido_base))
