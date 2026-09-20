"""Los dos lotes se sirven como uno solo.

El motor escribe **una traza por lote** (`outcomes_traza.jsonl` y
`outcomes_lote2_traza.jsonl`) y los 40 PDF del lote 2 viven en su propio arbol
(`data/facturas_lote2/facturas_primin`). El visor, en cambio, tiene que ver las
540 facturas como un unico listado.

Estas pruebas fijan las dos piezas que lo hacen posible: que `TrazaStore` lea
varias trazas como una (con la recarga automatica intacta) y que `resolver_pdf`
busque el PDF en los dos directorios sin perder el blindaje contra traversal.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.routers.facturas import resolver_pdf
from app.traza import FIRMA_SIN_TRAZA, TrazaStore

from .conftest import PDF_BYTES, PDF_VALIDO, construir_settings, escribir_traza

LOTE2 = "2026-08-05_P005.pdf"


def registro(file_id: str, lote: int, resultado: str = "PAGAR") -> dict:
    """Registro minimo con la forma que deja `procesa.py` en la traza plana."""
    return {
        "file_id": file_id,
        "result": resultado,
        "motivos": [],
        "campos": {"file_id": file_id},
        "lote": lote,
        "metodo_lectura": "texto_determinista",
        "escalon_lectura": "capa_texto",
        "calidad_lectura": 1.0,
        "segundos_lectura": 0.01,
    }


# --------------------------------------------------------------------------- #
# TrazaStore con varias trazas
# --------------------------------------------------------------------------- #
def test_dos_trazas_se_leen_como_una_sola(tmp_path: Path):
    lote1 = escribir_traza(tmp_path / "outcomes_traza.jsonl", [registro(PDF_VALIDO, 1)])
    lote2 = escribir_traza(tmp_path / "outcomes_lote2_traza.jsonl", [registro(LOTE2, 2)])

    traza = TrazaStore((lote1, lote2))

    assert traza.total() == 2
    assert traza.obtener(PDF_VALIDO) is not None
    assert traza.obtener(LOTE2) is not None
    assert traza.estadisticas()["por_lote"] == {"1": 1, "2": 1}


def test_una_traza_ausente_no_impide_servir_la_otra(tmp_path: Path):
    """Antes del sabado solo existe el lote 1: la API tiene que arrancar igual."""
    lote1 = escribir_traza(tmp_path / "outcomes_traza.jsonl", [registro(PDF_VALIDO, 1)])
    traza = TrazaStore((lote1, tmp_path / "outcomes_lote2_traza.jsonl"))

    assert traza.disponible is True
    assert traza.total() == 1
    assert traza.firma() != FIRMA_SIN_TRAZA


def test_la_traza_del_lote2_se_recarga_sola_al_aparecer(tmp_path: Path):
    """Sin reiniciar la API: el motor reescribe y la API se entera por `mtime`."""
    lote1 = escribir_traza(tmp_path / "outcomes_traza.jsonl", [registro(PDF_VALIDO, 1)])
    ruta_lote2 = tmp_path / "outcomes_lote2_traza.jsonl"
    traza = TrazaStore((lote1, ruta_lote2))

    assert traza.total() == 1
    firma_antes = traza.firma()

    escribir_traza(ruta_lote2, [registro(LOTE2, 2)])

    assert traza.total() == 2
    assert traza.obtener(LOTE2) is not None
    assert traza.firma() != firma_antes


def test_sin_ninguna_traza_la_firma_es_estable_y_no_se_relee(tmp_path: Path):
    """Dos ficheros ausentes son un estado estable, no una relectura por consulta."""
    traza = TrazaStore((tmp_path / "a.jsonl", tmp_path / "b.jsonl"))

    assert traza.disponible is False
    assert traza.firma() == FIRMA_SIN_TRAZA
    assert traza.firma() == FIRMA_SIN_TRAZA


def test_una_sola_traza_sigue_funcionando(tmp_path: Path):
    """Compatibilidad: quien pase una ruta suelta (no una secuencia) no cambia."""
    ruta = escribir_traza(tmp_path / "outcomes_traza.jsonl", [registro(PDF_VALIDO, 1)])
    traza = TrazaStore(ruta)

    assert traza.ruta == ruta
    assert traza.rutas == (ruta,)
    assert traza.total() == 1


# --------------------------------------------------------------------------- #
# `resolver_pdf` con dos directorios
# --------------------------------------------------------------------------- #
def test_resolver_pdf_busca_en_los_dos_directorios(tmp_path: Path):
    lote1 = tmp_path / "facturas"
    lote1.mkdir()
    lote2 = tmp_path / "facturas_lote2" / "facturas_primin"
    lote2.mkdir(parents=True)
    (lote1 / PDF_VALIDO).write_bytes(PDF_BYTES)
    (lote2 / LOTE2).write_bytes(PDF_BYTES)

    directorios = (lote1, lote2)
    assert resolver_pdf(directorios, PDF_VALIDO) == lote1 / PDF_VALIDO
    assert resolver_pdf(directorios, LOTE2) == lote2 / LOTE2


def test_resolver_pdf_sigue_siendo_estricto_con_dos_directorios(tmp_path: Path):
    """El segundo directorio no puede abrir una puerta que el primero cierra."""
    lote1 = tmp_path / "facturas"
    lote1.mkdir()
    lote2 = tmp_path / "facturas_lote2" / "facturas_primin"
    lote2.mkdir(parents=True)
    (lote2 / PDF_VALIDO).write_bytes(PDF_BYTES)

    directorios = (lote1, lote2)
    assert resolver_pdf(directorios, "no-existe.pdf") is None
    assert resolver_pdf(directorios, "../outputs/outcomes.jsonl") is None
    assert resolver_pdf(directorios, "/etc/passwd") is None
    assert resolver_pdf(directorios, "..") is None
    assert resolver_pdf(directorios, "") is None


# --------------------------------------------------------------------------- #
# Extremo a extremo: listado y PDF de los dos lotes
# --------------------------------------------------------------------------- #
def test_el_listado_y_el_pdf_cubren_los_dos_lotes(tmp_path: Path, ui_dir: Path):
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    escribir_traza(outputs / "outcomes_traza.jsonl", [registro(PDF_VALIDO, 1)])
    escribir_traza(outputs / "outcomes_lote2_traza.jsonl", [registro(LOTE2, 2, "ESCALAR")])

    lote1 = tmp_path / "facturas"
    lote1.mkdir()
    (lote1 / PDF_VALIDO).write_bytes(PDF_BYTES)
    lote2 = tmp_path / "facturas_lote2" / "facturas_primin"
    lote2.mkdir(parents=True)
    (lote2 / LOTE2).write_bytes(PDF_BYTES)

    settings = construir_settings(outputs, lote1, ui_dir, facturas_lote2_dir=lote2)
    with TestClient(create_app(settings)) as cliente:
        listado = cliente.get("/api/facturas?limit=500").json()
        assert listado["total"] == 2
        assert {fila["file_id"] for fila in listado["items"]} == {PDF_VALIDO, LOTE2}
        assert listado["items"][1]["lote"] == 2

        # El PDF del lote 2 se sirve desde su propio arbol, no desde el del lote 1.
        respuesta = cliente.get(f"/api/facturas/{LOTE2}/pdf")
        assert respuesta.status_code == 200
        assert respuesta.content == PDF_BYTES

        # Y el filtro por lote sigue discriminando.
        assert cliente.get("/api/facturas?lote=2").json()["total"] == 1
        assert cliente.get("/api/facturas?lote=1").json()["total"] == 1
