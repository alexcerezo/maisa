"""Tests de la metrica de campos de `ocr_service/scripts/corpus_eval.py`.

Viven en la suite del motor porque es la unica que corre la CI, pero solo tocan
la parte **pura** del script: `normalize`, `content_metrics`, `fields_of` y
`compare_fields`. Nada de esto abre un PDF ni levanta el servicio de vision, que
es justo lo que arregla el diferido de `pypdfium2` y `app.server`: antes, para
probar el recall de campos habia que estar dentro del contenedor.

Lo que se fija aqui es la parte que mas facil es romper sin enterarse:

* que el recall siga midiendo lo que media (no cambia con este trabajo);
* que la precision nueva no cuente como inventado un campo que el OCR leyo con
  un glifo de mas (eso es fallo de lectura, y ya lo paga el recall);
* que un IBAN partido en dos lineas por el OCR no se cuente como IBAN de mas.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
SCRIPTS = RAIZ.parent / "ocr_service" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import corpus_eval  # noqa: E402

#: Factura sintetica con los cuatro campos que se comparan. El texto de la
#: verdad y el de la lectura son el mismo en los casos "perfectos": aqui se mide
#: la metrica, no el OCR.
FACTURA = (
    "FACTURA\n"
    "Factura: 2026/11604    Fecha: 05/01/2026\n"
    "Pedido: PO-2026-0096\n"
    "Suministros Levante S.L.\n"
    "NIF: B46102331\n"
    "IBAN: ES21 0049 1500 0512 3456 7890\n"
    "Base: 2.489,99\n"
    "IVA (21%): 522,90\n"
    "TOTAL: 3.012,89\n"
)


def test_el_modulo_se_importa_sin_el_contenedor_de_ocr() -> None:
    """`pypdfium2` y `app.server` se cargan al usarlos, no al importar."""
    assert not hasattr(corpus_eval, "pdfium")
    assert callable(corpus_eval._pdfium)
    assert callable(corpus_eval._servicio)


# ------------------------------------------------------------------- normalize
def test_normalize_deja_solo_alfanumericos_en_minusculas() -> None:
    assert corpus_eval.normalize("NIF: B-46102331.\n") == "nifb46102331"
    assert corpus_eval.normalize(None) == ""
    assert corpus_eval.normalize("  ") == ""


# -------------------------------------------------------------- content_metrics
def test_content_metrics_sin_ruido_da_cero() -> None:
    assert corpus_eval.content_metrics(FACTURA, FACTURA) == {
        "cer": 0.0, "cer_sorted": 0.0, "chars": len(corpus_eval.normalize(FACTURA)),
    }


def test_content_metrics_sin_verdad_no_inventa_division() -> None:
    assert corpus_eval.content_metrics("", "lo que sea") == {
        "cer": 0.0, "cer_sorted": 0.0, "chars": 0,
    }


def test_el_desorden_de_lineas_sube_cer_pero_no_cer_sorted() -> None:
    """El par de metricas separa "lee mal los glifos" de "desordena lineas"."""
    lineas = FACTURA.strip().splitlines()
    desordenado = "\n".join([lineas[-1], *lineas[:-1]]) + "\n"  # el TOTAL arriba
    metrics = corpus_eval.content_metrics(FACTURA, desordenado)
    assert metrics["cer"] > 0.05
    assert metrics["cer_sorted"] == 0.0
    assert metrics["cer_sorted"] < metrics["cer"]


def test_un_glifo_bailado_sube_los_dos_cer() -> None:
    metrics = corpus_eval.content_metrics(FACTURA, FACTURA.replace("B46102331", "B4610233I"))
    assert metrics["cer"] > 0
    assert metrics["cer_sorted"] > 0


# -------------------------------------------------------------------- fields_of
def test_fields_of_saca_los_cuatro_campos() -> None:
    campos = corpus_eval.fields_of(FACTURA)
    assert campos["nif"] == {"B46102331"}
    assert campos["iban"] == {"ES2100491500051234567890"}
    assert campos["fechas"] == {"05/01/2026"}
    assert "3012.89" in campos["importes"]
    assert "2489.99" in campos["importes"]


def test_el_iban_se_compara_sin_espacios() -> None:
    """El OCR reparte los espacios del IBAN como quiere: no es una diferencia."""
    con_espacios = corpus_eval.fields_of("IBAN: ES21 0049 1500 0512 3456 7890")
    sin_espacios = corpus_eval.fields_of("IBAN: ES2100491500051234567890")
    assert con_espacios["iban"] == sin_espacios["iban"] == {"ES2100491500051234567890"}


def test_fields_of_sin_campos_devuelve_conjuntos_vacios() -> None:
    campos = corpus_eval.fields_of("FACTURA sin nada mas")
    assert campos["nif"] == set()
    assert campos["iban"] == set()
    assert campos["fechas"] == set()


# --------------------------------------------------------------- compare_fields
def test_lectura_perfecta_da_recall_precision_y_f1_uno() -> None:
    out = corpus_eval.compare_fields(FACTURA, FACTURA)
    assert out["recall"] == 1.0
    assert out["precision"] == 1.0
    assert out["f1"] == 1.0
    assert out["missing"] == {}
    assert out["spurious"] == {}
    assert out["checked"] == out["produced_total"] == out["legitimos"]


def test_un_campo_que_falta_baja_el_recall_pero_no_la_precision() -> None:
    """Leer de menos no es inventar: la precision se queda en 1."""
    sin_nif = FACTURA.replace("NIF: B46102331\n", "")
    out = corpus_eval.compare_fields(FACTURA, sin_nif)
    assert out["recall"] < 1.0
    assert "nif" in out["missing"]
    assert out["spurious"] == {}
    assert out["precision"] == 1.0
    assert out["f1"] < 1.0


def test_un_valor_inventado_baja_la_precision() -> None:
    """Este es el agujero que tapaba el recall solo."""
    con_ruido = FACTURA + "NIF: B99999999\n"
    out = corpus_eval.compare_fields(FACTURA, con_ruido)
    assert out["recall"] == 1.0
    assert out["spurious"]["nif"] == ["B99999999"]
    assert out["precision"] < 1.0
    assert out["legitimos"] == out["produced_total"] - 1


def test_un_glifo_bailado_no_cuenta_como_campo_inventado() -> None:
    """Un NIF con un caracter mal leido es fallo de lectura, no un campo de mas."""
    out = corpus_eval.compare_fields(FACTURA, FACTURA.replace("B46102331", "B4610233I"))
    assert out["spurious"] == {}
    assert "nif" in out["missing"]
    assert out["precision"] == 1.0
    assert out["recall"] < 1.0


def test_un_iban_partido_en_dos_lineas_no_se_cuenta_como_inventado() -> None:
    """El OCR reordena lineas: eso no puede salir como falso positivo."""
    partido = FACTURA.replace("ES21 0049 1500 0512 3456 7890", "ES21 0049 1500\n0512 3456 7890")
    out = corpus_eval.compare_fields(FACTURA, partido)
    assert "iban" not in out["spurious"]
    assert "iban" in out["missing"]


def test_el_orden_de_las_lineas_no_cambia_la_precision() -> None:
    lineas = FACTURA.strip().splitlines()
    desordenado = "\n".join([lineas[-1], *lineas[:-1]]) + "\n"
    out = corpus_eval.compare_fields(FACTURA, desordenado)
    assert out["recall"] == 1.0
    assert out["precision"] == 1.0


def test_sin_campos_en_la_verdad_no_hay_metrica() -> None:
    out = corpus_eval.compare_fields("FACTURA", "FACTURA")
    assert out["recall"] is None
    assert out["precision"] is None
    assert out["f1"] is None
    assert out["checked"] == 0
    assert out["produced_total"] == 0


def test_compare_fields_avisa_de_los_importes_mal_formados() -> None:
    out = corpus_eval.compare_fields(FACTURA, FACTURA + "\nTOTAL: 3.012,899\n")
    assert "3.012,899" in out["malformed"]


# -------------------------------------------------------------------------- f1
def test_f1_es_la_media_armonica() -> None:
    assert corpus_eval._f1(1.0, 1.0) == 1.0
    assert corpus_eval._f1(1.0, 1 / 3) == 0.5
    assert corpus_eval._f1(0.5, 0.5) == 0.5


def test_un_recall_de_cero_da_f1_cero_y_no_ausencia() -> None:
    """No encontrar nada es un resultado (0), no un dato que falta (None)."""
    assert corpus_eval._f1(0.0, 1.0) == 0.0
    assert corpus_eval._f1(0.0, 0.0) == 0.0


@pytest.mark.parametrize("recall,precision", [(None, 0.5), (0.5, None), (None, None)])
def test_f1_sin_los_dos_lados_no_se_inventa(recall, precision) -> None:
    assert corpus_eval._f1(recall, precision) is None


def test_el_umbral_de_parecido_es_el_que_decide() -> None:
    """El parecido se mide sobre el valor sin separadores de formato."""
    assert corpus_eval._coincide("B4610233I", {"B46102331"})          # un glifo
    assert corpus_eval._coincide("ES2100491500051234567890", {"ES21 0049 1500 0512 3456 7890"})
    assert not corpus_eval._coincide("B99999999", {"B46102331"})      # otro NIF
    assert not corpus_eval._coincide("3012.89", {"2489.99"})          # otro importe


# -------------------------------------------------------------------- evaluate
class _FuenteFalsa:
    """Lo minimo que `evaluate` le pide a la `_Source` del servicio."""

    def __init__(self, path) -> None:
        self.path = path
        self.count = 1

    def scales(self, _):  # noqa: ANN001 - firma del servicio
        return [1.0]

    def close(self) -> None:
        pass


def _monta_evaluate(monkeypatch, verdad: list[str], lectura: str) -> None:
    """Sustituye el PDF y el motor de vision por texto ya dado.

    `evaluate` es el unico sitio donde se juntan las metricas de todas las
    paginas, y es donde se puede colar un error de agregacion. Fingiendo las dos
    entradas (la verdad y la lectura) se prueba sin contenedor.
    """
    monkeypatch.setattr(corpus_eval, "truth_pages", lambda path: list(verdad))
    monkeypatch.setattr(
        corpus_eval, "_servicio",
        lambda: (_FuenteFalsa,
                 lambda source, i, scales, flag: {
                     "text": lectura, "page": i, "scale": scales[0],
                     "lines": lectura.splitlines(),
                 }),
    )


def test_evaluate_agrega_recall_precision_y_f1(monkeypatch, carpeta) -> None:
    _monta_evaluate(monkeypatch, [FACTURA], FACTURA + "NIF: B99999999\n")
    fila = corpus_eval.evaluate(carpeta / "sintetica.pdf")
    assert fila is not None
    assert fila["field_recall"] == 1.0
    assert fila["field_precision"] < 1.0
    assert fila["field_f1"] < 1.0
    assert fila["spurious"]["nif"] == ["B99999999"]
    # El detalle por pagina y el resumen tienen que decir lo mismo.
    assert fila["detail"][0]["fields"]["precision"] == fila["field_precision"]


def test_evaluate_de_una_lectura_perfecta_es_todo_uno(monkeypatch, carpeta) -> None:
    _monta_evaluate(monkeypatch, [FACTURA], FACTURA)
    fila = corpus_eval.evaluate(carpeta / "sintetica.pdf")
    assert fila is not None
    assert fila["field_recall"] == fila["field_precision"] == fila["field_f1"] == 1.0
    assert fila["missing"] == {} and fila["spurious"] == {}


def test_evaluate_ignora_los_escaneos_sin_capa_de_texto(monkeypatch, carpeta) -> None:
    """Sin verdad que comparar no hay metrica: el documento se salta."""
    _monta_evaluate(monkeypatch, ["", "  "], "lo que haya leido el OCR")
    assert corpus_eval.evaluate(carpeta / "scan.pdf") is None


# ---------------------------------------------------------------------- report


def fila_evaluada(**extra) -> dict:
    """Una fila como las que produce `evaluate`, para poder probar `report`."""
    fila = {
        "file": "sintetica.pdf", "pages": 1, "elapsed": 0.5,
        "cer": 0.0, "cer_sorted": 0.0, "chars": 100,
        "field_recall": 1.0, "field_precision": 1.0, "field_f1": 1.0,
        "fields_checked": 4, "fields_produced": 4,
        "missing": {}, "spurious": {}, "malformed": [], "detail": [],
    }
    fila.update(extra)
    return fila


def test_report_resume_precision_y_f1(capsys) -> None:
    corpus_eval.report([fila_evaluada()], skipped=0)
    salida = capsys.readouterr().out
    assert "recall de campos (NIF/IBAN/fecha/importe)" in salida
    assert "precision de campos (cuanto de lo leido existe)" in salida
    assert "F1 de campos (media de recall y precision)" in salida
    assert "campos inventados (falsos positivos): 0" in salida


def test_report_saca_los_campos_inventados(capsys) -> None:
    """Un documento con recall perfecto y precision baja tiene que salir."""
    corpus_eval.report([
        fila_evaluada(field_precision=0.5, field_f1=0.6667,
                      spurious={"nif": ["B99999999"]}),
    ], skipped=0)
    salida = capsys.readouterr().out
    assert "falsos positivos): 1" in salida
    assert "inventa nif" in salida
    assert "B99999999" in salida


def test_report_aguanta_un_documento_sin_campos(capsys) -> None:
    """`field_recall` y `field_precision` a `None` no pueden romper el informe."""
    corpus_eval.report([
        fila_evaluada(field_recall=None, field_precision=None, field_f1=None),
    ], skipped=3)
    salida = capsys.readouterr().out
    assert "saltados (escaneos) : 3" in salida
    assert "recall de campos (NIF/IBAN/fecha/importe)" not in salida
    assert "precision de campos (" not in salida


def test_report_sin_documentos_no_revienta(capsys) -> None:
    corpus_eval.report([], skipped=500)
    assert "evaluados           : 0" in capsys.readouterr().out
