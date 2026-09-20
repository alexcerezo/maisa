"""Tests del verificador externo de conformidad (`tools/conformidad.py`).

`conformidad.py` es la **unica** comprobacion que mira nuestras decisiones desde
fuera: `oro.py` compara nuestro criterio con nuestro criterio y
`valida_entrega.py` solo mira el formato. Si se rompe la clasificacion de
severidad, la herramienta dice CONFORME y nosotros damos por bueno un pago que
la referencia externa no admite; si se rompe la lectura de la referencia,
compara contra nada y tambien dice CONFORME. Es el peor fallo posible: un
instrumento que miente en verde.

Por eso estos tests fijan, sin red y sin corpus:

* el mapeo decision -> severidad (`FUERA_ALTO` solo si pagamos y no es admisible);
* el contrato de codigos de salida (0 estricto / 2 matices / 1 alto o medio);
* que una factura de la referencia sin decision nuestra cuenta como ALTO
  (el contrato "un outcome por archivo");
* la lectura de los dos formatos de referencia (envoltorio `oracle.json` y JSONL),
  autodetectados por estructura y no por extension;
* que la referencia sigue siendo un dato de entrada obligatorio y no un fichero
  versionado, que es lo que permite usar cualquier verificador externo.

La referencia real (un `oracle.json` de terceros) no se versiona: aqui se
sintetizan ficheros en `tmp_path`.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ / "tools") not in sys.path:
    sys.path.insert(0, str(RAIZ / "tools"))

import conformidad as conf  # noqa: E402


# ------------------------------------------------------------------- utilidades
def _escribe(ruta: Path, contenido) -> Path:
    """Vuelca texto tal cual, o un objeto como JSON, y devuelve la ruta."""
    if not isinstance(contenido, str):
        contenido = json.dumps(contenido, ensure_ascii=False)
    ruta.write_text(contenido, encoding="utf-8")
    return ruta


def _jsonl_texto(registros) -> str:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in registros)


def _entrada(primary, acceptable=None, **verdict):
    """Una factura del oraculo: `{"verdict": {...}, "findings": [...]}`."""
    v = {"primary": primary}
    if acceptable is not None:
        v["acceptable"] = list(acceptable)
    v.update(verdict)
    return {"verdict": v}


def _oracle(tmp_path: Path, files: dict, **meta) -> Path:
    return _escribe(tmp_path / "oracle.json", {"meta": meta, "files": files})


def _ejecuta(capsys, args):
    codigo = conf.main(args)
    return codigo, capsys.readouterr().out


def _corre(tmp_path: Path, capsys, decisiones, referencia, *extra):
    """Ejecuta la herramienta como la usa el equipo: (codigo, stdout)."""
    ruta = _escribe(tmp_path / "outcomes.jsonl", _jsonl_texto(decisiones))
    return _ejecuta(capsys, ["--outcomes", str(ruta), "--referencia", str(referencia), *extra])


def _salida_json(tmp_path: Path, capsys, decisiones, referencia):
    codigo, texto = _corre(tmp_path, capsys, decisiones, referencia, "--json")
    return codigo, json.loads(texto)


# ------------------------------------------------------------------ normalizacion
@pytest.mark.parametrize("crudo, esperado", [
    ("PAGAR", "PAGAR"),
    ("pagar", "PAGAR"),
    ("Pago", "PAGAR"),
    ("  pagar  ", "PAGAR"),
    ("NO_PAGAR", "NO_PAGAR"),
    ("no pagar", "NO_PAGAR"),
    ("No Pagar", "NO_PAGAR"),
    ("no_pagar", "NO_PAGAR"),
    ("ESCALAR", "ESCALAR"),
    ("escalado", "ESCALAR"),
    ("revisar", "ESCALAR"),
    ("Revisión", "ESCALAR"),
    ("rechazar", "NO_PAGAR"),
])
def test_normaliza_resultado_acepta_alias_y_tildes(crudo, esperado):
    assert conf.normaliza_resultado(crudo) == esperado


@pytest.mark.parametrize("crudo", [
    None, 7, 3.5, True, "", "   ", "PAGAR YA", "pagado", "dudoso",
    ["PAGAR"], {"result": "PAGAR"},
])
def test_normaliza_resultado_rechaza_lo_que_no_es_del_enum(crudo):
    assert conf.normaliza_resultado(crudo) is None


# -------------------------------------------------------------------- severidad
def _opinion(primary, acceptable=None):
    return conf.Opinion(
        file_id="x.pdf", primary=primary,
        acceptable=tuple(acceptable) if acceptable is not None else (primary,))


@pytest.mark.parametrize("nuestro, opinion, clase", [
    # dentro de lo admisible
    ("PAGAR", _opinion("PAGAR"), conf.OK),
    ("PAGAR", _opinion("PAGAR", ["PAGAR", "ESCALAR"]), conf.OK),
    ("ESCALAR", _opinion("PAGAR", ["PAGAR", "ESCALAR"]), conf.NO_PRIMARIO),
    ("NO_PAGAR", _opinion("ESCALAR", ["ESCALAR", "NO_PAGAR"]), conf.NO_PRIMARIO),
    # fuera de lo admisible: el orden PAGAR > ESCALAR > NO_PAGAR marca la gravedad
    ("PAGAR", _opinion("ESCALAR", ["ESCALAR", "NO_PAGAR"]), conf.FUERA_ALTO),
    ("ESCALAR", _opinion("PAGAR", ["PAGAR"]), conf.FUERA_MEDIO),
    ("NO_PAGAR", _opinion("ESCALAR", ["ESCALAR"]), conf.FUERA_BAJO),
    # sin contrapartida a un lado u otro
    (None, _opinion("PAGAR"), conf.FALTA),
    ("PAGAR", None, conf.EXTRA),
])
def test_clasifica_mapea_decision_y_opinion_a_la_clase(nuestro, opinion, clase):
    assert conf.clasifica(nuestro, opinion) == clase


def test_pagar_solo_es_error_alto_si_la_referencia_no_lo_admite():
    """La distincion que separa un error real de una discrepancia de politica."""
    admitido = conf.clasifica("PAGAR", _opinion("ESCALAR", ["PAGAR", "ESCALAR"]))
    assert admitido == conf.NO_PRIMARIO
    assert admitido != conf.FUERA_ALTO
    assert conf.clasifica("PAGAR", _opinion("ESCALAR", ["ESCALAR"])) == conf.FUERA_ALTO


def test_la_severidad_declarada_de_cada_clase():
    assert conf.SEVERIDAD[conf.FUERA_ALTO] == conf.ALTO
    assert conf.SEVERIDAD[conf.FALTA] == conf.ALTO
    assert conf.SEVERIDAD[conf.FUERA_MEDIO] == conf.MEDIO
    assert conf.SEVERIDAD[conf.FUERA_BAJO] == conf.BAJO
    assert conf.SEVERIDAD[conf.EXTRA] == conf.BAJO
    assert conf.SEVERIDAD[conf.NO_PRIMARIO] == conf.INFO
    assert conf.SEVERIDAD[conf.OK] == conf.NADA
    assert set(conf.ORDEN) == set(conf.SEVERIDAD)


@pytest.mark.parametrize("clase, codigo", [
    (conf.OK, 0),
    (conf.NO_PRIMARIO, 2),
    (conf.FUERA_BAJO, 2),
    (conf.EXTRA, 2),
    (conf.FUERA_ALTO, 1),
    (conf.FUERA_MEDIO, 1),
    (conf.FALTA, 1),
])
def test_codigo_de_salida_por_clase(clase, codigo):
    assert conf._salida_codigo(Counter({clase: 1})) == codigo


def test_el_codigo_de_salida_es_el_del_desacuerdo_mas_grave():
    assert conf._salida_codigo(Counter({conf.OK: 400, conf.NO_PRIMARIO: 10})) == 2
    assert conf._salida_codigo(Counter({conf.OK: 400, conf.NO_PRIMARIO: 10,
                                        conf.FUERA_BAJO: 1})) == 2
    assert conf._salida_codigo(Counter({conf.OK: 400, conf.NO_PRIMARIO: 10,
                                        conf.FUERA_BAJO: 1, conf.FUERA_ALTO: 1})) == 1
    assert conf._salida_codigo(Counter({conf.OK: 400, conf.FALTA: 1})) == 1


def test_veredicto_estricto_solo_si_coincidimos_en_todas():
    veredicto, motivo = conf._veredicto(Counter({conf.OK: 3}))
    assert veredicto == "CONFORME (ESTRICTO)"
    assert "todas" in motivo


@pytest.mark.parametrize("extra", [
    {conf.NO_PRIMARIO: 2}, {conf.FUERA_BAJO: 1}, {conf.EXTRA: 1},
    {conf.NO_PRIMARIO: 10, conf.FUERA_BAJO: 2, conf.EXTRA: 1},
])
def test_veredicto_con_matices_cuando_solo_hay_info_o_discrepancias_de_politica(extra):
    conteo = Counter({conf.OK: 489})
    conteo.update(extra)
    veredicto, motivo = conf._veredicto(conteo)
    assert veredicto == "CONFORME CON MATICES"
    assert "cero FUERA_ALTO/MEDIO" in motivo


@pytest.mark.parametrize("clase", [conf.FUERA_ALTO, conf.FUERA_MEDIO, conf.FALTA])
def test_veredicto_no_conforme_con_cualquier_alto_medio_o_falta(clase):
    veredicto, motivo = conf._veredicto(Counter({conf.OK: 1, clase: 1}))
    assert veredicto == "NO CONFORME"
    assert motivo.startswith("la referencia externa no admite")


def test_las_reglas_soft_se_marcan_al_imprimir_la_fila():
    fila = conf.Fila(file_id="a.pdf", clase=conf.OK, nuestro="PAGAR", primario="PAGAR",
                     acceptable=("PAGAR",), fallos=("N2c", "N6"), blandos=("N6",))
    assert fila.reglas() == "N2c, N6 (soft)"
    assert fila.severidad == conf.NADA
    vacia = conf.Fila(file_id="b.pdf", clase=conf.OK, nuestro="PAGAR", primario="PAGAR",
                      acceptable=("PAGAR",))
    assert vacia.reglas() == "-"


# ----------------------------------------------------------- lectura de la opinion
def test_opinion_de_oracle_funde_el_veredicto_con_los_findings():
    opinion = conf._opinion_de_oracle("scan_021.pdf", {
        "verdict": {
            "primary": "ESCALAR",
            "acceptable": ["ESCALAR", "NO_PAGAR"],
            "hard_failures": ["N0_legible", "N1b_iban_coincide"],
            "soft_flags": ["N4_fecha_valida_no_futura"],
            "confidence": "low",
            "rationale": "el oraculo no ha podido leer ['date','supplier_nif']",
        },
        "findings": [
            {"rule": "N1b_iban_coincide", "passed": False,
             "message": "no se puede comparar el IBAN sin proveedor en el maestro"},
            {"rule": "N2a_pedido_existe", "passed": True},
            {"rule": "N6_instrucciones", "passed": False, "soft": True},
        ],
    })
    assert opinion.primary == "ESCALAR"
    assert opinion.acceptable == ("ESCALAR", "NO_PAGAR")
    # el veredicto manda: anade lo que no estaba en `findings`, sin duplicar
    assert opinion.fallos == ("N1b_iban_coincide", "N6_instrucciones", "N0_legible",
                              "N4_fecha_valida_no_futura")
    assert opinion.blandos == ("N6_instrucciones", "N4_fecha_valida_no_futura")
    assert opinion.confianza == "low"
    assert "no ha podido leer" in opinion.rationale
    assert opinion.mensajes == (
        "N1b_iban_coincide: no se puede comparar el IBAN sin proveedor en el maestro",)


def test_opinion_de_oracle_sin_acceptable_toma_el_primario():
    opinion = conf._opinion_de_oracle("a.pdf", {"verdict": {"primary": "pagar"}})
    assert opinion.primary == "PAGAR"
    assert opinion.acceptable == ("PAGAR",)


def test_opinion_de_oracle_descarta_un_acceptable_que_no_es_del_enum():
    opinion = conf._opinion_de_oracle("a.pdf", {
        "verdict": {"primary": "PAGAR", "acceptable": ["PAGAR", "lo que sea", None, 7]}})
    assert opinion.acceptable == ("PAGAR",)


def test_opinion_de_oracle_tolera_entradas_vacias():
    opinion = conf._opinion_de_oracle("a.pdf", {})
    assert opinion.primary is None
    assert opinion.acceptable == ()
    assert opinion.fallos == ()


# ------------------------------------------------------ lectura de la referencia
def test_lee_referencia_autodetecta_el_envoltorio(tmp_path):
    ruta = _oracle(tmp_path, {"a.pdf": _entrada("PAGAR", ["PAGAR"])}, lote="lote1", n=500)
    informe = conf.Informe()
    opiniones, meta, tipo = conf.lee_referencia(ruta, informe)
    assert tipo == "referencia"
    assert meta == {"lote": "lote1", "n": 500}
    assert opiniones["a.pdf"].primary == "PAGAR"
    assert informe.problemas == []


def test_lee_referencia_autodetecta_el_jsonl_plano(tmp_path):
    ruta = _escribe(tmp_path / "ref.jsonl", _jsonl_texto([
        {"file_id": "a.pdf", "expected": "PAGAR"},
        {"filename": "b.pdf", "expected": "ESCALAR"},
    ]))
    informe = conf.Informe()
    opiniones, meta, tipo = conf.lee_referencia(ruta, informe)
    assert tipo == "jsonl"
    assert meta == {}
    # en un JSONL la unica decision es el conjunto admisible entero
    assert opiniones["b.pdf"].primary == "ESCALAR"
    assert opiniones["b.pdf"].acceptable == ("ESCALAR",)
    assert informe.problemas == []


def test_lee_el_formato_del_jsonl_de_trace_it(tmp_path):
    """La forma real de la referencia JSONL que usamos como contraste externo."""
    ruta = _escribe(tmp_path / "batch1_expected.jsonl", _jsonl_texto([
        {"file_id": "2026-01-08_P001.pdf", "expected": "PAGAR",
         "confidence": "high", "why": "clean: no finding"},
        {"file_id": "2026-01-11_P007.pdf", "expected": "ESCALAR",
         "confidence": "low", "why": "el total no cuadra con el pedido"},
    ]))
    opiniones = conf.lee_referencia(ruta, conf.Informe())[0]
    assert opiniones["2026-01-08_P001.pdf"].primary == "PAGAR"
    assert opiniones["2026-01-11_P007.pdf"].confianza == "low"
    assert opiniones["2026-01-11_P007.pdf"].rationale == "el total no cuadra con el pedido"


def test_en_un_jsonl_manda_una_sola_clave_de_resultado(tmp_path):
    """El lector se queda con la primera clave de resultado que reconoce.

    Es una decision de diseno (leer todo el fichero con la misma clave, sin
    mezclar `result` con `expected` segun la linea), pero tiene un coste: una
    linea que declare el resultado en otra clave se cae, y el aviso culpa al
    `file_id` cuando el `file_id` estaba bien. Se fija aqui para que el
    comportamiento sea conocido y no una sorpresa.
    """
    ruta = _escribe(tmp_path / "mixto.jsonl", _jsonl_texto([
        {"file_id": "a.pdf", "result": "PAGAR"},
        {"file_id": "b.pdf", "expected": "ESCALAR"},
    ]))
    informe = conf.Informe()
    opiniones = conf.lee_referencia(ruta, informe)[0]
    assert set(opiniones) == {"a.pdf"}
    assert any("1 linea(s) sin file_id/resultado reconocible" in p for p in informe.problemas)


def test_la_deteccion_es_por_estructura_y_no_por_extension(tmp_path):
    """Un JSONL con extension `.json` y un envoltorio con extension `.jsonl`."""
    envoltorio = _escribe(tmp_path / "raro.jsonl",
                          {"meta": {}, "files": {"a.pdf": _entrada("PAGAR")}})
    plano = _escribe(tmp_path / "raro.json", '{"file_id": "a.pdf", "result": "PAGAR"}\n')
    assert conf.lee_referencia(envoltorio, conf.Informe())[2] == "referencia"
    assert conf.lee_referencia(plano, conf.Informe())[2] == "jsonl"


def test_lee_referencia_descarta_las_facturas_sin_opinion_y_avisa(tmp_path):
    ruta = _oracle(tmp_path, {
        "buena.pdf": _entrada("PAGAR"),
        "sin_veredicto.pdf": {"findings": []},
        "mala.pdf": "no soy un dict",
    })
    informe = conf.Informe()
    opiniones, _, _ = conf.lee_referencia(ruta, informe)
    assert set(opiniones) == {"buena.pdf"}
    assert any("sin `acceptable` ni `primary`" in p for p in informe.problemas)
    assert any("entrada no valida para mala.pdf" in p for p in informe.problemas)


def test_lee_referencia_lee_reglas_falladas_de_la_lista(tmp_path):
    ruta = _escribe(tmp_path / "ref.jsonl", _jsonl_texto([
        {"file_id": "a.pdf", "result": "ESCALAR", "rules": ["N2c:FAIL", "N3a:OK", "N6:fail"]},
    ]))
    opinion = conf.lee_referencia(ruta, conf.Informe())[0]["a.pdf"]
    assert opinion.fallos == ("N2c", "N6")


def test_lee_referencia_acepta_la_clave_espanola_de_reglas(tmp_path):
    ruta = _escribe(tmp_path / "ref.jsonl", _jsonl_texto([
        {"file_id": "a.pdf", "result": "ESCALAR", "reglas": ["N1a:NO", "N1b:SI"]},
    ]))
    opinion = conf.lee_referencia(ruta, conf.Informe())[0]["a.pdf"]
    assert opinion.fallos == ("N1a",)


def test_lee_referencia_avisa_de_las_lineas_ilegibles_y_repetidas(tmp_path):
    ruta = _escribe(tmp_path / "ref.jsonl",
                    '{"file_id": "a.pdf", "result": "PAGAR"}\n'
                    '\n'
                    'no es json\n'
                    '[1, 2]\n'
                    '{"result": "PAGAR"}\n'
                    '{"file_id": "a.pdf", "result": "ESCALAR"}\n')
    informe = conf.Informe()
    opiniones = conf.lee_referencia(ruta, informe)[0]
    assert opiniones["a.pdf"].primary == "ESCALAR"
    assert any("3 linea(s) sin file_id/resultado" in p for p in informe.problemas)
    assert any("1 file_id repetido" in p for p in informe.problemas)


def test_las_reglas_falladas_toleran_entradas_con_basura(tmp_path):
    """`findings` con elementos que no son dict, y reglas que no son `REGLA:FAIL`."""
    ruta = _escribe(tmp_path / "ref.jsonl", _jsonl_texto([
        {"file_id": "a.pdf", "result": "ESCALAR",
         "findings": ["no soy un dict", 7, None,
                      {"rule": "N2c", "passed": False},
                      {"rule": "N3a", "passed": True},
                      {"passed": False}],
         "rules": ["sin dos puntos", 7, None, "N1b:OK"]},
    ]))
    opinion = conf.lee_referencia(ruta, conf.Informe())[0]["a.pdf"]
    assert opinion.fallos == ("N2c",)


def test_lee_outcomes_cuenta_lo_ilegible_y_se_queda_con_la_ultima(tmp_path):
    ruta = _escribe(tmp_path / "outcomes.jsonl",
                    '{"file_id": "a.pdf", "result": "PAGAR"}\n'
                    'no es json\n'
                    '[1, 2]\n'
                    '{"file_id": "b.pdf"}\n'
                    '{"file_id": "a.pdf", "result": "NO_PAGAR"}\n'
                    '\n')
    informe = conf.Informe()
    decisiones = conf.lee_outcomes(ruta, informe)
    assert decisiones == {"a.pdf": "NO_PAGAR"}
    assert any("outcomes: 3 linea(s)" in p for p in informe.problemas)
    assert any("outcomes: 1 file_id repetido" in p for p in informe.problemas)


def test_lee_outcomes_acepta_las_distintas_claves_de_resultado(tmp_path):
    ruta = _escribe(tmp_path / "outcomes.jsonl", _jsonl_texto([
        {"file_id": "a.pdf", "result": "PAGAR"},
        {"file_id": "b.pdf", "decision": "escalar"},
        {"filename": "c.pdf", "outcome": "no pagar"},
    ]))
    decisiones = conf.lee_outcomes(ruta, conf.Informe())
    assert decisiones == {"a.pdf": "PAGAR", "b.pdf": "ESCALAR", "c.pdf": "NO_PAGAR"}


# --------------------------------------------------------------- armado del informe
def test_construye_une_los_dos_lados_y_ordena_por_file_id(tmp_path):
    referencia = _oracle(tmp_path, {"b.pdf": _entrada("PAGAR"), "a.pdf": _entrada("PAGAR")})
    outcomes = _escribe(tmp_path / "outcomes.jsonl", _jsonl_texto([
        {"file_id": "c.pdf", "result": "PAGAR"},
        {"file_id": "b.pdf", "result": "PAGAR"},
    ]))
    informe, _, tipo, decisiones, opiniones = conf.construye(
        argparse.Namespace(outcomes=str(outcomes), referencia=str(referencia)))
    assert tipo == "referencia"
    assert [f.file_id for f in informe.filas] == ["a.pdf", "b.pdf", "c.pdf"]
    assert set(decisiones) == {"b.pdf", "c.pdf"}
    assert set(opiniones) == {"a.pdf", "b.pdf"}
    assert informe.problemas == []


def test_construye_avisa_de_que_el_jsonl_solo_declara_un_resultado(tmp_path):
    referencia = _escribe(tmp_path / "ref.jsonl", _jsonl_texto([{"file_id": "a.pdf",
                                                                 "result": "PAGAR"}]))
    outcomes = _escribe(tmp_path / "outcomes.jsonl", _jsonl_texto([{"file_id": "a.pdf",
                                                                   "result": "PAGAR"}]))
    informe, _, tipo, _, _ = conf.construye(
        argparse.Namespace(outcomes=str(outcomes), referencia=str(referencia)))
    assert tipo == "jsonl"
    assert any("conjunto admisible de un elemento" in n for n in informe.notas)


def test_construye_avisa_de_los_ficheros_que_faltan(tmp_path):
    informe, _, tipo, decisiones, opiniones = conf.construye(
        argparse.Namespace(outcomes=str(tmp_path / "no.jsonl"),
                           referencia=str(tmp_path / "tampoco.json")))
    assert (tipo, decisiones, opiniones) == ("", {}, {})
    assert any("outcomes: no existe" in p for p in informe.problemas)
    assert any("referencia: no existe" in p for p in informe.problemas)


# ------------------------------------------------------------------ de punta a punta
def test_con_todo_admisible_el_veredicto_es_estricto(tmp_path, capsys):
    referencia = _oracle(tmp_path, {
        "a.pdf": _entrada("PAGAR"),
        "b.pdf": _entrada("NO_PAGAR", ["ESCALAR", "NO_PAGAR"]),
    })
    codigo, salida = _salida_json(tmp_path, capsys, [
        {"file_id": "a.pdf", "result": "PAGAR"},
        {"file_id": "b.pdf", "result": "NO_PAGAR"},
    ], referencia)
    assert codigo == 0
    assert salida["veredicto"] == "CONFORME (ESTRICTO)"
    assert salida["desacuerdos"] == []
    assert salida["salida"] == 0


def test_el_caso_que_destapo_la_regresion_del_escaneo_ilegible(tmp_path, capsys):
    """El oraculo externo solo admite ESCALAR/NO_PAGAR para `scan_021.pdf`.

    Nuestro PAGAR cae fuera, asi que la herramienta tiene que decir NO CONFORME:
    es el unico desacuerdo de riesgo alto que quedaba en el lote de 500. Si
    alguien relaja `clasifica` (por ejemplo, dando PAGAR por admisible cuando la
    referencia no lo admite), este test cae y con el la unica red externa.
    """
    referencia = _oracle(tmp_path, {
        "scan_021.pdf": {
            "verdict": {
                "primary": "ESCALAR",
                "acceptable": ["ESCALAR", "NO_PAGAR"],
                "hard_failures": ["N0_legible", "N1b_iban_coincide",
                                  "N2b_pedido_del_proveedor"],
                "confidence": "low",
                "policy_dependent": True,
                "rationale": "el oraculo no ha podido leer ['date','supplier_nif']",
            },
            "findings": [{"rule": "N0_legible", "passed": False,
                          "message": "no legibles: ['date','supplier_nif']"}],
        },
    })
    codigo, salida = _salida_json(tmp_path, capsys,
                                  [{"file_id": "scan_021.pdf", "result": "PAGAR"}], referencia)
    assert codigo == 1
    assert salida["veredicto"] == "NO CONFORME"
    (desacuerdo,) = salida["desacuerdos"]
    assert desacuerdo["clase"] == conf.FUERA_ALTO
    assert desacuerdo["severidad"] == "ALTO"
    assert desacuerdo["acceptable"] == ["ESCALAR", "NO_PAGAR"]
    assert desacuerdo["confianza"] == "low"
    assert "N0_legible" in desacuerdo["reglas"]


def test_una_factura_sin_decision_nuestra_es_falta_y_bloquea(tmp_path, capsys):
    """El contrato "un outcome por archivo": dejar una factura fuera es ALTO."""
    referencia = _oracle(tmp_path, {"a.pdf": _entrada("PAGAR"), "b.pdf": _entrada("PAGAR")})
    codigo, salida = _salida_json(tmp_path, capsys,
                                  [{"file_id": "a.pdf", "result": "PAGAR"}], referencia)
    assert codigo == 1
    assert salida["resumen"]["conteo_clase"][conf.FALTA] == 1
    assert salida["resumen"]["conteo_severidad"]["ALTO"] == 1
    assert salida["veredicto"] == "NO CONFORME"
    assert salida["desacuerdos"][0]["nuestro"] is None


def test_decidir_una_factura_que_la_referencia_no_cubre_es_solo_un_matiz(tmp_path, capsys):
    referencia = _oracle(tmp_path, {"a.pdf": _entrada("PAGAR")})
    codigo, salida = _salida_json(tmp_path, capsys, [
        {"file_id": "a.pdf", "result": "PAGAR"},
        {"file_id": "sobrante.pdf", "result": "PAGAR"},
    ], referencia)
    assert codigo == 2
    assert salida["resumen"]["conteo_clase"][conf.EXTRA] == 1
    assert salida["resumen"]["conteo_severidad"]["BAJO"] == 1
    assert salida["veredicto"] == "CONFORME CON MATICES"


def test_el_resumen_cuenta_primario_acceptable_y_reglas_falladas(tmp_path, capsys):
    referencia = _oracle(tmp_path, {
        "a.pdf": _entrada("PAGAR", ["PAGAR"]),
        "b.pdf": _entrada("PAGAR", ["PAGAR", "ESCALAR"]),
        "c.pdf": _entrada("ESCALAR", ["ESCALAR", "NO_PAGAR"],
                          hard_failures=["N2c_importe_igual_pedido"]),
    })
    codigo, salida = _salida_json(tmp_path, capsys, [
        {"file_id": "a.pdf", "result": "PAGAR"},
        {"file_id": "b.pdf", "result": "ESCALAR"},
        {"file_id": "c.pdf", "result": "PAGAR"},
    ], referencia)
    assert codigo == 1
    resumen = salida["resumen"]
    assert resumen["facturas_en_ambos"] == 3
    assert resumen["coincidencia_primario"] == 1
    assert resumen["dentro_acceptable"] == 2
    assert resumen["conteo_clase"][conf.OK] == 1
    assert resumen["conteo_clase"][conf.NO_PRIMARIO] == 1
    assert resumen["conteo_clase"][conf.FUERA_ALTO] == 1
    assert resumen["reglas_falladas_desacuerdos"] == {"N2c_importe_igual_pedido": 1}
    assert resumen["distribucion_nuestra"] == {"ESCALAR": 1, "PAGAR": 2}


def test_la_meta_de_la_referencia_llega_al_json(tmp_path, capsys):
    referencia = _oracle(tmp_path, {"a.pdf": _entrada("PAGAR")}, lote="lote-1", n=500)
    _, salida = _salida_json(tmp_path, capsys, [{"file_id": "a.pdf", "result": "PAGAR"}],
                             referencia)
    assert salida["origen"]["meta_referencia"] == {"lote": "lote-1", "n": 500}
    assert salida["origen"]["tipo_referencia"] == "referencia"
    assert "problemas" not in salida


def test_el_json_tambien_publica_los_problemas_de_la_referencia(tmp_path, capsys):
    """Sin esto, quien consuma el JSON no se entera de que la comparacion se degrado."""
    referencia = _escribe(tmp_path / "ref.jsonl",
                          '{"file_id": "a.pdf", "result": "PAGAR"}\n'
                          'no es json\n')
    _, salida = _salida_json(tmp_path, capsys, [{"file_id": "a.pdf", "result": "PAGAR"}],
                             referencia)
    assert any("1 linea(s) sin file_id/resultado" in p for p in salida["problemas"])


def test_sin_ficheros_dice_que_no_puede_comparar_y_devuelve_1(tmp_path, capsys):
    codigo, texto = _ejecuta(capsys, ["--outcomes", str(tmp_path / "no.jsonl"),
                                      "--referencia", str(tmp_path / "tampoco.json")])
    assert codigo == 1
    assert "no se puede comparar" in texto
    assert "outcomes: no existe" in texto
    assert "referencia: no existe" in texto


def test_una_referencia_vacia_no_permite_comparar(tmp_path, capsys):
    referencia = _escribe(tmp_path / "vacia.jsonl", "")
    codigo, texto = _corre(tmp_path, capsys, [{"file_id": "a.pdf", "result": "PAGAR"}],
                           referencia)
    assert codigo == 1
    assert "no se puede comparar" in texto


def test_la_tabla_es_determinista_y_ordenada(tmp_path, capsys):
    referencia = _oracle(tmp_path, {f"{c}.pdf": _entrada("PAGAR") for c in "zyxw"})
    decisiones = [{"file_id": f"{c}.pdf", "result": "PAGAR"} for c in "wxyz"]
    _, primera = _corre(tmp_path, capsys, decisiones, referencia, "--verbose")
    _, segunda = _corre(tmp_path, capsys, decisiones, referencia, "--verbose")
    assert primera == segunda
    posiciones = [primera.index(f"{c}.pdf") for c in "wxyz"]
    assert posiciones == sorted(posiciones)


def test_la_referencia_es_un_dato_de_entrada_obligatorio(capsys):
    """No hay referencia por defecto: el repositorio no arrastra datos de terceros."""
    with pytest.raises(SystemExit) as exc:
        conf.main([])
    assert exc.value.code == 2
    assert "--referencia" in capsys.readouterr().err


# ------------------------------------------------------------------ informe de texto
def test_la_tabla_avisa_de_los_problemas_y_de_las_notas(tmp_path, capsys):
    """La unica señal de que la comparacion viene degradada es esta seccion."""
    referencia = _escribe(tmp_path / "ref.jsonl",
                          '{"file_id": "a.pdf", "result": "PAGAR"}\n'
                          'no es json\n')
    codigo, texto = _corre(tmp_path, capsys, [{"file_id": "a.pdf", "result": "PAGAR"}],
                           referencia)
    assert codigo == 0
    assert "-- problemas" in texto
    assert "! referencia JSONL: 1 linea(s)" in texto
    assert "-- notas" in texto
    assert "conjunto admisible de un elemento" in texto


def test_la_cabecera_resume_la_meta_del_oraculo(tmp_path, capsys):
    referencia = _oracle(tmp_path, {"a.pdf": _entrada("PAGAR")},
                         generated_at="2026-09-19", today="2026-09-20",
                         routes={"vision": 29, "texto": 471}, unreadable=["x.pdf"])
    _, texto = _corre(tmp_path, capsys, [{"file_id": "a.pdf", "result": "PAGAR"}], referencia)
    assert "meta     : generado 2026-09-19 · hoy 2026-09-20" in texto
    assert "rutas texto 471 vision 29" in texto
    assert "ilegibles 1" in texto


def test_el_detalle_verbose_incluye_mensaje_y_motivo(tmp_path, capsys):
    referencia = _oracle(tmp_path, {
        "scan_021.pdf": {
            "verdict": {"primary": "ESCALAR", "acceptable": ["ESCALAR"],
                        "rationale": "el oraculo no ha podido leer ['date','supplier_nif']"},
            "findings": [{"rule": "N0_legible", "passed": False,
                          "message": "no legibles: ['date','supplier_nif']"}],
        },
    })
    codigo, texto = _corre(tmp_path, capsys, [{"file_id": "scan_021.pdf", "result": "PAGAR"}],
                           referencia, "--verbose")
    assert codigo == 1
    assert "   · scan_021.pdf" in texto
    assert "       - N0_legible: no legibles: ['date','supplier_nif']" in texto
    assert "       = el oraculo no ha podido leer" in texto
    # en verboso tambien se listan las que van bien
    assert conf.OK in texto


def test_sin_verbose_las_facturas_ok_no_se_listan(tmp_path, capsys):
    referencia = _oracle(tmp_path, {"a.pdf": _entrada("PAGAR"),
                                    "b.pdf": _entrada("ESCALAR", ["ESCALAR"])})
    _, texto = _corre(tmp_path, capsys, [{"file_id": "a.pdf", "result": "PAGAR"},
                                         {"file_id": "b.pdf", "result": "PAGAR"}], referencia)
    assert conf.TITULO[conf.OK] not in texto
    assert conf.TITULO[conf.FUERA_ALTO] in texto
    assert "a.pdf" not in texto


def test_formato_dist_marca_la_referencia_vacia():
    assert conf._formato_dist(Counter()) == "vacio"
    assert conf._formato_dist(Counter({"PAGAR": 2, "ESCALAR": 1})) == "ESCALAR 1 PAGAR 2"
