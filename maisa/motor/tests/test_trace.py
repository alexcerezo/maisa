"""Tests de `maisa.trace`: la traza encadenada detecta manipulacion.

Solo se prueba lo que sostiene el pitch: que un cambio de **un caracter** en
cualquier linea se detecta, que una linea rota no revienta la verificacion y
que no se puede reescribir lo ya anotado.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ / "src") not in sys.path:  # pytest sin conftest: el paquete vive en src/
    sys.path.insert(0, str(RAIZ / "src"))

from maisa import trace  # noqa: E402

RELOJ_TS = "2026-09-19T10:00:00.000Z"


def registro_ejemplo(ruta: Path, decisiones: tuple[str, ...] = ("PAGAR", "ESCALAR")) -> trace.Registro:
    """Log minimo pero realista: cabecera de lote + lectura/decision por factura."""
    reg = trace.Registro(ruta, reloj=lambda: RELOJ_TS)
    reg.anota(trace.TIPO_LOTE, lote=1, version_norma="norma_v3", facturas=len(decisiones))
    for i, resultado in enumerate(decisiones, 1):
        file_id = f"2026-01-0{i}_P00{i}.pdf"
        reg.anota(
            trace.TIPO_LECTURA, file_id, sha256="ab" * 32, escalon="capa_texto",
            calidad=1.0, metodo_lectura="texto_determinista",
        )
        reg.anota(
            trace.TIPO_DECISION, file_id, result=resultado,
            motivos=[] if resultado == "PAGAR" else ["total no legible"],
            version_norma="norma_v3", total=Decimal("3012.89"),
            hechos=[{"regla": "R3_iva", "ok": resultado == "PAGAR", "motivo": "total = base + IVA"}],
        )
    reg.cierra()
    return reg


def escribe(ruta: Path, lineas: list[str], fin: bool = True) -> Path:
    ruta.write_text("\n".join(lineas) + ("\n" if fin else ""), encoding="utf-8")
    return ruta


def lineas(ruta: Path) -> list[str]:
    return ruta.read_text(encoding="utf-8").splitlines()


# --------------------------------------------------------------------------- #
# Cadena y determinismo
# --------------------------------------------------------------------------- #


def test_cadena_valida_sin_problemas(tmp_path: Path) -> None:
    ruta = registro_ejemplo(tmp_path / "log.jsonl").ruta
    assert trace.verifica(ruta) == []


def test_encadena_hashes_y_empieza_en_genesis(tmp_path: Path) -> None:
    reg = registro_ejemplo(tmp_path / "log.jsonl")
    eventos = reg.eventos
    assert [ev.seq for ev in eventos] == list(range(len(eventos)))
    assert eventos[0].hash_prev == trace.GENESIS
    assert all(b.hash_prev == a.hash for a, b in zip(eventos, eventos[1:]))
    assert all(ev.comprueba() for ev in eventos)


def test_misma_entrada_mismo_hash(tmp_path: Path) -> None:
    a = registro_ejemplo(tmp_path / "a.jsonl")
    b = registro_ejemplo(tmp_path / "b.jsonl")
    assert [ev.hash for ev in a.eventos] == [ev.hash for ev in b.eventos]


def test_decimal_se_canoniza_a_str(tmp_path: Path) -> None:
    ruta = tmp_path / "log.jsonl"
    reg = trace.Registro(ruta, reloj=lambda: RELOJ_TS)
    ev = reg.anota("decision", "f.pdf", total=Decimal("3012.89"), iva=Decimal("522.90"))
    reg.cierra()
    assert '"total":"3012.89"' in ev.canonico()
    assert trace.verifica(ruta) == []  # el ida y vuelta no cambia el hash


# --------------------------------------------------------------------------- #
# Manipulacion: un caracter, en cualquier linea
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("indice", range(5))
def test_un_caracter_cambiado_en_cualquier_linea(tmp_path: Path, indice: int) -> None:
    """Cambiar un digito del `ts` (JSON valido y canonico) debe cantar en el hash."""
    original = lineas(registro_ejemplo(tmp_path / "log.jsonl").ruta)
    linea = original[indice]
    corte = linea.index('"ts":"') + len('"ts":"')
    assert linea[corte] == "2"
    cambiada = linea[:corte] + "3" + linea[corte + 1 :]
    assert len(cambiada) == len(linea) and cambiada != linea
    ruta = escribe(
        tmp_path / "manipulado.jsonl", original[:indice] + [cambiada] + original[indice + 1 :]
    )
    problemas = trace.verifica(ruta)
    assert problemas, f"no se detecto la manipulacion de la linea {indice + 1}"
    assert "hash_roto" in {p.clase for p in problemas}


@pytest.mark.parametrize("indice", range(5))
def test_un_caracter_crudo_en_cualquier_linea_se_detecta(tmp_path: Path, indice: int) -> None:
    """Ni siquiera hace falta entender la linea: romper un caracter ya se nota."""
    original = lineas(registro_ejemplo(tmp_path / "log.jsonl").ruta)
    linea = original[indice]
    mitad = len(linea) // 2
    cambiada = linea[:mitad] + ("X" if linea[mitad] != "X" else "Y") + linea[mitad + 1 :]
    ruta = escribe(
        tmp_path / "crudo.jsonl", original[:indice] + [cambiada] + original[indice + 1 :]
    )
    clases = {p.clase for p in trace.verifica(ruta)}
    assert clases & {"hash_roto", "evento_invalido", "json_invalido"}, clases


def test_dato_reescrito_sin_rehash_se_detecta(tmp_path: Path) -> None:
    ruta = registro_ejemplo(tmp_path / "log.jsonl").ruta
    original = lineas(ruta)
    # Manipulacion "inteligente": el JSON sigue siendo valido y canonico.
    obj = json.loads(original[-1])
    obj["datos"]["result"] = "PAGAR"
    original[-1] = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    ruta = escribe(tmp_path / "manipulado.jsonl", original)
    problemas = trace.verifica(ruta)
    assert "hash_roto" in {p.clase for p in problemas}


def test_linea_borrada_rompe_la_cadena(tmp_path: Path) -> None:
    original = lineas(registro_ejemplo(tmp_path / "log.jsonl").ruta)
    ruta = escribe(tmp_path / "recortado.jsonl", original[:2] + original[3:])
    clases = {p.clase for p in trace.verifica(ruta)}
    assert "cadena_rota" in clases
    assert "seq_discontinuo" in clases


# --------------------------------------------------------------------------- #
# Resiliencia: no revienta y sigue mirando
# --------------------------------------------------------------------------- #


def test_linea_corrupta_se_reporta_y_no_revienta(tmp_path: Path) -> None:
    original = lineas(registro_ejemplo(tmp_path / "log.jsonl").ruta)
    ruta = escribe(tmp_path / "corrupto.jsonl", original[:2] + ["{esto no es json"] + original[2:])
    problemas = trace.verifica(ruta)
    assert "json_invalido" in {p.clase for p in problemas}
    assert len(trace.carga(ruta)) == len(original)  # el resto se sigue leyendo


def test_linea_corrupta_y_manipulada_despues_se_reportan_las_dos(tmp_path: Path) -> None:
    original = lineas(registro_ejemplo(tmp_path / "log.jsonl").ruta)
    linea = original[-1]
    corte = linea.index('"ts":"') + len('"ts":"')
    original[-1] = linea[:corte] + "3" + linea[corte + 1 :]
    ruta = escribe(tmp_path / "doble.jsonl", original[:2] + ["basura"] + original[2:])
    clases = {p.clase for p in trace.verifica(ruta)}
    assert {"json_invalido", "hash_roto"} <= clases


def test_linea_vacia_se_reporta(tmp_path: Path) -> None:
    original = lineas(registro_ejemplo(tmp_path / "log.jsonl").ruta)
    ruta = escribe(tmp_path / "hueco.jsonl", original[:2] + [""] + original[2:])
    assert "linea_vacia" in {p.clase for p in trace.verifica(ruta)}


def test_log_truncado_se_reporta(tmp_path: Path) -> None:
    ruta = registro_ejemplo(tmp_path / "log.jsonl").ruta
    truncado = escribe(tmp_path / "truncado.jsonl", lineas(ruta), fin=False)
    assert "truncado" in {p.clase for p in trace.verifica(truncado)}


def test_sello_ancla_la_traza(tmp_path: Path) -> None:
    ruta = registro_ejemplo(tmp_path / "log.jsonl").ruta
    assert trace.verifica(ruta, sello=trace.sello(ruta)) == []
    assert "sello_distinto" in {p.clase for p in trace.verifica(ruta, sello="0" * 64)}


# --------------------------------------------------------------------------- #
# Inmutabilidad
# --------------------------------------------------------------------------- #


def test_no_se_puede_reescribir_un_evento(tmp_path: Path) -> None:
    reg = registro_ejemplo(tmp_path / "log.jsonl")
    evento = reg[0]
    with pytest.raises(trace.RegistroInmutable):
        reg[0] = evento
    with pytest.raises(trace.RegistroInmutable):
        del reg[0]
    with pytest.raises(trace.RegistroInmutable):
        reg.pop()
    with pytest.raises(trace.RegistroInmutable):
        reg.clear()
    with pytest.raises(trace.RegistroInmutable):
        reg.borra()
    with pytest.raises(trace.RegistroInmutable):
        reg.append(evento)
    assert len(reg) == 5


def test_evento_congelado_y_datos_de_solo_lectura(tmp_path: Path) -> None:
    evento = registro_ejemplo(tmp_path / "log.jsonl")[0]
    with pytest.raises(Exception):
        evento.seq = 99  # type: ignore[misc]
    with pytest.raises(trace.RegistroInmutable):
        evento.datos["lote"] = 2
    assert isinstance(evento.datos, dict)
    assert dict(evento.datos)["lote"] == 1


def test_continuar_un_log_roto_falla(tmp_path: Path) -> None:
    ruta = registro_ejemplo(tmp_path / "log.jsonl").ruta
    lineas_ok = lineas(ruta)
    escribe(ruta, lineas_ok[:2] + ["basura"] + lineas_ok[2:])
    with pytest.raises(trace.TrazaError):
        trace.Registro(ruta, continuar=True)


# --------------------------------------------------------------------------- #
# Linaje y diff
# --------------------------------------------------------------------------- #


def test_linaje_y_explica(tmp_path: Path) -> None:
    reg = registro_ejemplo(tmp_path / "log.jsonl")
    cadena = reg.linaje("2026-01-02_P002.pdf")
    assert [ev.tipo for ev in cadena] == [trace.TIPO_LECTURA, trace.TIPO_DECISION]
    explicacion = reg.explica("2026-01-02_P002.pdf")
    assert explicacion is not None
    assert explicacion["resultado"] == "ESCALAR"
    assert "R3_iva: total = base + IVA" in explicacion["por_que"]
    assert explicacion["sha256"] == "ab" * 32
    assert reg.explica("no-existe.pdf") is None


def test_diff_dice_que_cambio_y_por_que(tmp_path: Path) -> None:
    a = registro_ejemplo(tmp_path / "a.jsonl", ("PAGAR", "ESCALAR"))
    b = registro_ejemplo(tmp_path / "b.jsonl", ("PAGAR", "PAGAR"))
    cambios = a.diff(b)
    assert [c.file_id for c in cambios] == ["2026-01-02_P002.pdf"]
    assert (cambios[0].antes, cambios[0].despues) == ("ESCALAR", "PAGAR")
    assert "motivos distintos" in cambios[0].motivo
    todos = a.diff(b, solo_cambios=False)
    assert len(todos) == 2 and {c.clase for c in todos} == {"cambio", "sin_cambio"}
    assert trace.diff(tmp_path / "a.jsonl", tmp_path / "b.jsonl")[0].clase == "cambio"
    assert trace.resumen_diff(cambios)["total"] == 1


def test_diff_detecta_ficheros_que_aparecen_o_desaparecen(tmp_path: Path) -> None:
    a = registro_ejemplo(tmp_path / "a.jsonl", ("PAGAR", "ESCALAR"))
    b = registro_ejemplo(tmp_path / "b.jsonl", ("PAGAR",))
    cambios = a.diff(b)
    assert [(c.file_id, c.clase) for c in cambios] == [("2026-01-02_P002.pdf", "solo_en_a")]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_verifica_y_diff(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    ruta = registro_ejemplo(tmp_path / "log.jsonl").ruta
    assert trace.main(["verifica", str(ruta)]) == 0
    assert "OK" in capsys.readouterr().out

    lineas_ok = lineas(ruta)
    linea = lineas_ok[-1]
    corte = linea.index('"ts":"') + len('"ts":"')
    roto = escribe(
        tmp_path / "roto.jsonl", lineas_ok[:-1] + [linea[:corte] + "3" + linea[corte + 1 :]]
    )
    assert trace.main(["verifica", str(roto)]) == 1
    assert "hash_roto" in capsys.readouterr().out

    otra = registro_ejemplo(tmp_path / "otra.jsonl", ("PAGAR", "PAGAR")).ruta
    assert trace.main(["diff", str(ruta), str(otra)]) == 0
    assert "ESCALAR -> PAGAR" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Vocabulario de tipos: los 4 propios + los 11 del motor Rust legado
# --------------------------------------------------------------------------- #

# Valores exactos que el Rust escribe en `eventos.tipo` (`TRASPASO.md` 3.7 y
# `docker/mongosh/02-schema-init.js`). Si alguno cambia, cambia la traza.
VOCABULARIO_RUST = (
    "ERP_RETRY_ORA_00600",
    "ERP_RETRY_SES_401",
    "ERP_RETRY_ERP_429",
    "ERP_CACHE_HIT",
    "ERP_CACHE_MISS",
    "OCR_FAIL",
    "OCR_OK",
    "EXPEDIENTE_ESTADO",
    "DECISION_EMITIDA",
    "REVISION_ABIERTA",
    "REVISION_RESUELTA",
)

TIPOS_ANTIGUOS = (trace.TIPO_LOTE, trace.TIPO_LECTURA, trace.TIPO_DECISION, trace.TIPO_FIN)

TIPOS_NUEVOS = (
    trace.TIPO_OCR_OK,
    trace.TIPO_OCR_FAIL,
    trace.TIPO_ERP_CACHE_HIT,
    trace.TIPO_ERP_CACHE_MISS,
    trace.TIPO_ERP_RETRY_ORA_00600,
    trace.TIPO_ERP_RETRY_SES_401,
    trace.TIPO_ERP_RETRY_ERP_429,
    trace.TIPO_REVISION_ABIERTA,
    trace.TIPO_REVISION_RESUELTA,
    trace.TIPO_DECISION_EMITIDA,
    trace.TIPO_EXPEDIENTE_ESTADO,
)

# Hash de un evento de tipo nuevo con `RELOJ_TS` fijo y `hash_prev` fijo (el
# del evento 0 de `registro_nuevo`). Fijarlo es lo que convierte "el hash es
# estable" en algo comprobable: si cambia el formato canonico, este test cae.
HASH_OCR_OK = "68971587642d3256ccb4a59f8d693447ebf34d87c95273d6bc7327b52723014c"


def registro_nuevo(ruta: Path) -> trace.Registro:
    """Log con un evento de cada tipo nuevo, ademas de lote/fin."""
    reg = trace.Registro(ruta, reloj=lambda: RELOJ_TS)
    reg.anota(trace.TIPO_LOTE, lote=1, version_norma="norma_v3", facturas=1)
    reg.anota(trace.TIPO_OCR_OK, "f.pdf", paginas=1, ms=42)
    reg.anota(trace.TIPO_OCR_FAIL, "ilegible.pdf", motivo="servicio OCR caido", ms=900)
    reg.anota(trace.TIPO_ERP_CACHE_HIT, "g.pdf", pedido="PO-2026-0725")
    reg.anota(trace.TIPO_ERP_CACHE_MISS, "f.pdf", pedido="PO-2026-0724")
    reg.anota(trace.TIPO_ERP_RETRY_ORA_00600, trace.GLOBAL, intento=2, backoff_ms=400)
    reg.anota(trace.TIPO_ERP_RETRY_SES_401, trace.GLOBAL, intento=1, relogin=True)
    reg.anota(trace.TIPO_ERP_RETRY_ERP_429, trace.GLOBAL, intento=3, espera_ms=1000)
    reg.anota(trace.TIPO_REVISION_ABIERTA, "f.pdf", motivo="total no legible")
    reg.anota(trace.TIPO_REVISION_RESUELTA, "f.pdf", resolucion="PAGAR", revisor="alberto")
    reg.anota(trace.TIPO_DECISION_EMITIDA, "f.pdf", result="PAGAR", version_norma="norma_v3")
    reg.anota(trace.TIPO_EXPEDIENTE_ESTADO, "f.pdf", de="ABIERTO", a="PAGADO")
    reg.anota(trace.TIPO_FIN, resultados={"PAGAR": 1}, escalones={"capa_texto": 1})
    reg.cierra()
    return reg


def test_es_tipo_valido_acepta_antiguos_y_nuevos_y_rechaza_inventados() -> None:
    for tipo in TIPOS_ANTIGUOS:
        assert trace.es_tipo_valido(tipo), tipo
    for tipo in TIPOS_NUEVOS:
        assert trace.es_tipo_valido(tipo), tipo
    assert not trace.es_tipo_valido("inventado")
    assert not trace.es_tipo_valido("ocr_ok")  # el Rust los escribe en MAYUSCULAS
    assert not trace.es_tipo_valido("")


def test_los_tipos_nuevos_copian_el_vocabulario_del_rust() -> None:
    assert len(TIPOS_NUEVOS) == len(set(TIPOS_NUEVOS)) == 11
    assert sorted(TIPOS_NUEVOS) == sorted(VOCABULARIO_RUST)
    assert trace.TIPOS[len(TIPOS_ANTIGUOS):] == TIPOS_NUEVOS  # los 11 del Rust, tal cual


def test_tipos_sin_duplicados_y_con_los_antiguos() -> None:
    assert len(trace.TIPOS) == len(set(trace.TIPOS))
    assert len(trace.TIPOS) == len(TIPOS_ANTIGUOS) + len(VOCABULARIO_RUST)
    assert set(TIPOS_ANTIGUOS) <= set(trace.TIPOS)
    assert set(trace.TIPOS) == set(TIPOS_ANTIGUOS) | set(VOCABULARIO_RUST)


def test_eventos_de_tipos_nuevos_verifican_y_se_detecta_la_manipulacion(
    tmp_path: Path,
) -> None:
    ruta = registro_nuevo(tmp_path / "nuevo.jsonl").ruta
    assert trace.verifica(ruta) == []
    tipos = [ev.tipo for ev in trace.carga(ruta)]
    assert tipos == [trace.TIPO_LOTE, *TIPOS_NUEVOS, trace.TIPO_FIN]

    original = lineas(ruta)
    linea = original[1]  # el OCR_OK, un tipo nuevo
    corte = linea.index('"ts":"') + len('"ts":"')
    assert linea[corte] == "2"
    cambiada = linea[:corte] + "3" + linea[corte + 1 :]
    roto = escribe(tmp_path / "roto.jsonl", original[:1] + [cambiada] + original[2:])
    clases = {p.clase for p in trace.verifica(roto)}
    assert "hash_roto" in clases
    assert trace.main(["verifica", str(roto)]) == 1


def test_hash_de_un_tipo_nuevo_es_estable_y_no_depende_del_vocabulario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El hash cubre seq/ts/tipo/file_id/datos/hash_prev, no el conjunto de tipos."""
    reg = trace.Registro(tmp_path / "a.jsonl", reloj=lambda: RELOJ_TS)
    reg.anota(trace.TIPO_LOTE, lote=1)
    evento = reg.anota(trace.TIPO_OCR_OK, "f.pdf", paginas=1, ms=42)
    reg.cierra()
    assert evento.hash == HASH_OCR_OK
    assert evento.hash_prev == reg[0].hash
    assert '"tipo":"OCR_OK"' in evento.canonico()
    assert trace.carga(tmp_path / "a.jsonl")[1].hash == HASH_OCR_OK  # el ida y vuelta no cambia

    # Mismo contenido, otro registro: mismo hash (determinismo).
    otro = trace.Registro(tmp_path / "b.jsonl", reloj=lambda: RELOJ_TS)
    otro.anota(trace.TIPO_LOTE, lote=1)
    assert otro.anota(trace.TIPO_OCR_OK, "f.pdf", paginas=1, ms=42).hash == HASH_OCR_OK

    # Y crecer el vocabulario no toca ni un hash: TIPOS no entra en el calculo.
    monkeypatch.setattr(trace, "TIPOS", trace.TIPOS + ("INVENTADO",))
    assert trace.es_tipo_valido("INVENTADO")
    tercera = trace.Registro(tmp_path / "c.jsonl", reloj=lambda: RELOJ_TS)
    tercera.anota(trace.TIPO_LOTE, lote=1)
    crecido = tercera.anota(trace.TIPO_OCR_OK, "f.pdf", paginas=1, ms=42)
    tercera.cierra()
    assert crecido.hash == HASH_OCR_OK
    assert trace.verifica(tmp_path / "c.jsonl") == []
