"""Tests del censo del error de extraccion (`tools/censo_extraccion.py`).

El censo es una **medida**, no una puerta: si el numero sube no rompe nada, pero
si el clasificador deja de reconocer una nota de `norma.py` el censo miente por
lo bajo. Estos tests fijan las dos cosas que si importan:

* que cada nota que escribe `norma.py` case con su patron (si alguien reescribe
  un texto alli, aqui salta en `sin_clasificar`);
* que reparacion (error de lectura reconstruido) y hueco (falta el dato) no se
  mezclen, porque el primero es error de extraccion y el segundo no.

No tocan el corpus: los casos son trazas sinteticas.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ / "tools") not in sys.path:
    sys.path.insert(0, str(RAIZ / "tools"))

import censo_extraccion as censo  # noqa: E402


# --------------------------------------------------------------- clasificador
#: Un texto real por patron. Son literalmente los que escriben
#: `norma.Decisor._resuelve_pedido`, `_resuelve_nif`, `_resuelve_iban` y
#: `_repara_importes_ocr`; si alli cambia una redaccion, este test cae.
NOTAS_REALES = (
    ("pedido PO-2026-0996 reparado a PO-2026-0096 (un digito bailado)", "pedido", censo.REPARACION),
    ("pedido ilegible recuperado por estructura (NIF B46102331 + total 3012.89)",
     "pedido", censo.REPARACION),
    ("pedido PO-2026-0096 reparado a PO-2026-0096 (anio ilegible en el escaneo; "
     "el cuerpo identifica el pedido)", "pedido", censo.REPARACION),
    ("NIF B4610233I corregido a B46102331 (confusion I/1 del escaneo)",
     "nif", censo.REPARACION),
    ("IBAN ES2100491500051234567890 corregido a ES2100491500051234567891 (digito de control)",
     "iban", censo.REPARACION),
    ("importe recompuesto a 3.012,89 (el OCR desalineo el separador decimal del total "
     "impreso: 301.289)", "importe", censo.REPARACION),
    ("total ilegible o ruidoso (3012.8) confirmado por la aritmetica del documento: "
     "base 2.489,99 + IVA 522,90 = 3.012,89", "importe", censo.REPARACION),
    ("base 248.999 recompuesta a 2.489,99", "importe", censo.REPARACION),
    ("pedido PO-2026-9999 no existe en el ERP", "pedido", censo.HUECO),
    ("NIF B00000000 no figura en el maestro", "nif", censo.HUECO),
    ("IBAN ES0000000000000000000000 no figura en el maestro", "iban", censo.HUECO),
)


@pytest.mark.parametrize("nota,campo,tipo", NOTAS_REALES)
def test_clasifica_las_notas_que_escribe_norma(nota: str, campo: str, tipo: str) -> None:
    patron = censo.clasifica(nota)
    assert patron is not None, f"nota sin clasificar: {nota!r}"
    assert patron.campo == campo
    assert patron.tipo == tipo


def test_cada_nota_real_va_a_su_familia() -> None:
    familias = {patron.tipo for patron in censo.PATRONES}
    assert familias == {censo.REPARACION, censo.HUECO}


def test_clasifica_ignora_espacios_y_notas_vacias() -> None:
    assert censo.clasifica("  pedido PO-2026-9999 no existe en el ERP  ") is not None
    assert censo.clasifica("") is None
    assert censo.clasifica("   ") is None
    assert censo.clasifica("cualquier cosa que no sea una nota de correccion") is None


def test_el_pedido_no_existe_es_hueco_y_no_reparacion() -> None:
    """El pedido inventado no es un fallo de lectura: es que no esta en el ERP."""
    patron = censo.clasifica("pedido PO-2026-9999 no existe en el ERP")
    assert patron is not None and patron.tipo == censo.HUECO
    # El texto de "reparado" y el de "no existe" empiezan igual: el orden de
    # PATRONES no debe confundirlos.
    otro = censo.clasifica("pedido PO-2026-9999 reparado a PO-2026-0096 (difuso)")
    assert otro is not None and otro.tipo == censo.REPARACION


# -------------------------------------------------------------------- censo()
def fila(file_id: str, *, notas=(), notas_importe=(), escalon="capa_texto",
         calidad=1.0, result="PAGAR", sospechosos=False) -> dict:
    """Una entrada de traza con lo minimo que lee el censo."""
    return {
        "file_id": file_id,
        "result": result,
        "escalon_lectura": escalon,
        "calidad_lectura": calidad,
        "sospechosos": sospechosos,
        "campos": {"notas": list(notas), "notas_importe": list(notas_importe)},
    }


def test_censo_separa_reparaciones_de_huecos() -> None:
    resumen = censo.censo([
        fila("a.pdf", notas=["NIF B4610233I corregido a B46102331 (confusion I/1)"]),
        fila("b.pdf", notas=["pedido PO-2026-9999 no existe en el ERP"]),
        fila("c.pdf"),
    ])
    assert resumen["facturas"] == 3
    assert resumen[censo.REPARACION]["facturas"] == 1
    assert resumen[censo.REPARACION]["notas"] == 1
    assert resumen[censo.HUECO]["facturas"] == 1
    assert resumen[censo.REPARACION]["tasa"] == round(1 / 3, 4)
    assert resumen[censo.REPARACION]["por_campo"] == {"nif": 1}
    assert resumen[censo.HUECO]["por_campo"] == {"pedido": 1}


def test_una_factura_con_dos_notas_cuenta_una_vez_como_factura() -> None:
    """La tasa es de facturas afectadas; el recuento de notas va aparte."""
    resumen = censo.censo([
        fila("a.pdf",
             notas=["NIF B4610233I corregido a B46102331 (I/1)"],
             notas_importe=["base 248.999 recompuesta a 2.489,99"]),
    ])
    reparacion = resumen[censo.REPARACION]
    assert reparacion["notas"] == 2
    assert reparacion["facturas"] == 1
    assert reparacion["tasa"] == 1.0
    assert reparacion["por_campo"] == {"importe": 1, "nif": 1}
    assert len(resumen["detalle"]) == 2


def test_lo_que_no_reconocemos_no_desaparece() -> None:
    """Una nota nueva de `norma.py` sale en `sin_clasificar`, no se pierde."""
    resumen = censo.censo([fila("a.pdf", notas=["nota que aun no tiene patron"])])
    assert resumen["sin_clasificar"] == {"nota que aun no tiene patron": 1}
    assert resumen[censo.REPARACION]["notas"] == 0
    assert resumen[censo.HUECO]["notas"] == 0


def test_censo_lee_escalones_calidad_y_sospechosos() -> None:
    resumen = censo.censo([
        fila("a.pdf", escalon="capa_texto", calidad=1.0, result="PAGAR"),
        fila("b.pdf", escalon="cache_ocr", calidad=0.5, result="ESCALAR", sospechosos=True),
        fila("c.pdf", escalon="cache_ocr", calidad=0.25, result="ESCALAR"),
    ])
    assert resumen["escalones"] == {"cache_ocr": 2, "capa_texto": 1}
    assert resumen["lectura"]["sospechosos"] == 1
    assert resumen["lectura"]["calidad_media"] == round((1.0 + 0.5 + 0.25) / 3, 4)
    assert resumen["lectura"]["calidad_por_resultado"] == {"ESCALAR": 0.375, "PAGAR": 1.0}
    assert resumen["lectura"]["calidad_medidas"] == 3
    assert resumen["lectura"]["calidad_sin_medida"] == 0


def test_la_calidad_sin_medir_no_entra_en_la_media() -> None:
    """Un escaneo publica `calidad_lectura: null`, que no es lo mismo que 0.0.

    Contarlo como cero hundia la media del lote (0.741 con ESCALAR frente a
    0.950 con PAGAR) y hacia parecer que las escaladas venian de leer mal,
    cuando su lectura es indistinguible del texto embebido. La media se calcula
    sobre lo medido y el censo declara la cobertura.
    """
    resumen = censo.censo([
        fila("a.pdf", escalon="capa_texto", calidad=0.99, result="PAGAR"),
        fila("b.pdf", escalon="cache_ocr", calidad=None, result="ESCALAR"),
        fila("c.pdf", escalon="cache_ocr", calidad=None, result="ESCALAR"),
    ])
    lectura = resumen["lectura"]
    assert lectura["calidad_media"] == 0.99
    assert lectura["calidad_medidas"] == 1
    assert lectura["calidad_sin_medida"] == 2
    assert lectura["calidad_por_resultado"] == {"PAGAR": 0.99}


def test_censo_aguanta_filas_sin_campos_ni_calidad() -> None:
    """La traza puede venir de un lote antiguo: no debe reventar por un hueco."""
    resumen = censo.censo([{"file_id": "a.pdf", "result": "PAGAR"}])
    assert resumen["facturas"] == 1
    assert resumen["escalones"] == {"?": 1}
    assert resumen["lectura"]["calidad_media"] is None
    assert resumen[censo.REPARACION]["facturas"] == 0
    assert resumen[censo.REPARACION]["tasa"] == 0.0


def test_censo_de_traza_vacia_no_divide_por_cero() -> None:
    resumen = censo.censo([])
    assert resumen["facturas"] == 0
    assert resumen[censo.REPARACION]["tasa"] is None
    assert resumen["lectura"]["calidad_media"] is None


def test_el_detalle_apunta_la_factura_y_la_nota() -> None:
    resumen = censo.censo([
        fila("b.pdf", notas_importe=["importe recompuesto a 3.012,89 (desalineado: 301.289)"]),
    ])
    assert resumen["detalle"] == [{
        "file_id": "b.pdf",
        "tipo": censo.REPARACION,
        "campo": "importe",
        "etiqueta": "total recompuesto (separador decimal desalineado)",
        "nota": "importe recompuesto a 3.012,89 (desalineado: 301.289)",
    }]


# --------------------------------------------------------------------- main()
def test_main_falla_si_no_esta_la_traza(carpeta: Path, capsys) -> None:
    codigo = censo.main(["--traza", str(carpeta / "no_existe.jsonl")])
    assert codigo == 1
    assert "no existe la traza" in capsys.readouterr().err


def test_main_cuenta_la_traza_y_vuelca_el_json(carpeta: Path, capsys) -> None:
    traza = carpeta / "traza.jsonl"
    traza.write_text(
        "\n".join(json.dumps(f, ensure_ascii=False) for f in (
            fila("a.pdf", notas=["NIF B4610233I corregido a B46102331 (I/1)"]),
            fila("b.pdf", notas=["pedido PO-2026-9999 no existe en el ERP"]),
        )) + "\n",
        encoding="utf-8",
    )
    destino = carpeta / "censo.json"
    assert censo.main(["--traza", str(traza), "--json", str(destino)]) == 0

    salida = capsys.readouterr().out
    assert "REPARACIONES" in salida and "HUECOS" in salida
    crudo = json.loads(destino.read_text(encoding="utf-8"))
    assert crudo["facturas"] == 2
    assert crudo[censo.REPARACION]["facturas"] == 1


def test_main_avisa_de_una_linea_ilegible(carpeta: Path, capsys) -> None:
    traza = carpeta / "traza.jsonl"
    traza.write_text('{"file_id": "a.pdf"}\n{no es json}\n', encoding="utf-8")
    assert censo.main(["--traza", str(traza)]) == 1
    assert "linea ilegible" in capsys.readouterr().err


def test_main_verbose_lista_una_linea_por_nota(carpeta: Path, capsys) -> None:
    traza = carpeta / "traza.jsonl"
    traza.write_text(
        json.dumps(fila("a.pdf", notas=["NIF B4610233I corregido a B46102331 (I/1)"]),
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    assert censo.main(["--traza", str(traza), "--verbose"]) == 0
    salida = capsys.readouterr().out
    assert "DETALLE POR FACTURA" in salida
    assert "a.pdf" in salida
