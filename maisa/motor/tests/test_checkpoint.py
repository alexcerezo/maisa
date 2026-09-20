"""Tests de `maisa.checkpoint`: reanudar un lote cortado decide lo mismo.

Lo que se comprueba aqui no es que el fichero se escriba, sino que **reanudar
da la misma entrega que una pasada entera**: el checkpoint guarda la lectura
completa (texto incluido) y la regla de pedidos repetidos vuelve a ver el lote
entero, no solo lo que faltaba. Si eso se rompe, la entrega de un lote
reanudado no seria la de un lote completo y no valdria.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from maisa import checkpoint, emit, lectura
from maisa.texto import Candidato, Lectura


def _documento(file_id: str, texto: str = "FACTURA", pedido: str = "PO-2026-0001") -> lectura.Documento:
    return lectura.Documento(
        lectura=Lectura(
            file_id=file_id,
            paginas=1,
            metodo="texto_determinista",
            pedido=[Candidato(pedido, "Pedido: PO-2026-0001", 0.9)],
            sospechosos=["algo"],
            ordenes=["PO-2026-0001"],
            nota="nota",
            texto=texto,
        ),
        sha256="a" * 64,
        escalon="capa_texto",
        cache=True,
        segundos=0.0123,
        calidad=0.87,
        motor="texto",
        proveedor="ninguno",
    )


def test_la_ida_y_vuelta_conserva_la_lectura_entera(carpeta: Path) -> None:
    """`como_dict`/`desde_dict` no pueden perder nada que la decision mire."""
    original = _documento("f.pdf", texto="TOTAL 2.637,80 EUR")

    vuelta = lectura.Documento.desde_dict(
        json.loads(json.dumps(original.como_dict(), ensure_ascii=False))
    )

    assert vuelta == original
    # El texto es justo el campo que `Lectura.como_dict` no lleva: si se
    # perdiera, `divisas_declaradas` e `extrae_iva_pct` decidirian otra cosa.
    assert vuelta.lectura.texto == original.lectura.texto
    assert vuelta.lectura.pedido[0].valor == "PO-2026-0001"


def test_desde_dict_tolera_documentos_vacios() -> None:
    vuelta = lectura.Documento.desde_dict({})

    assert vuelta.lectura.file_id == ""
    assert vuelta.lectura.paginas == 0
    assert vuelta.lectura.nif == []
    assert vuelta.escalon == ""


def test_anota_y_recarga_en_orden(carpeta: Path) -> None:
    ruta = carpeta / "lote_lecturas.jsonl"
    with checkpoint.Checkpoint(ruta) as marca:
        marca.anota(_documento("b.pdf"))
        marca.anota(_documento("a.pdf"))

    recargado = checkpoint.Checkpoint(ruta)

    assert set(recargado.leidos) == {emit.normaliza_file_id("a.pdf"),
                                     emit.normaliza_file_id("b.pdf")}
    hechos, faltan = recargado.reparte([carpeta / "a.pdf", carpeta / "c.pdf", carpeta / "b.pdf"])
    assert [d.lectura.file_id for d in hechos] == ["a.pdf", "b.pdf"]
    assert [p.name for p in faltan] == ["c.pdf"]


def test_una_linea_a_medias_se_descarta_y_no_tumba_la_reanudacion(carpeta: Path) -> None:
    """El proceso que muere escribiendo deja media linea: es el caso normal."""
    ruta = carpeta / "lote_lecturas.jsonl"
    with checkpoint.Checkpoint(ruta) as marca:
        marca.anota(_documento("a.pdf"))
    with ruta.open("a", encoding="utf-8") as fh:
        fh.write('{"lectura": {"file_id": "b.pd')

    recargado = checkpoint.Checkpoint(ruta)

    assert recargado.lineas_rotas == 1
    assert list(recargado.leidos) == [emit.normaliza_file_id("a.pdf")]


def test_borra_retira_el_checkpoint(carpeta: Path) -> None:
    ruta = carpeta / "lote_lecturas.jsonl"
    marca = checkpoint.Checkpoint(ruta)
    marca.anota(_documento("a.pdf"))

    marca.borra()

    assert not ruta.exists()


def test_aviso_sin_lote_no_avisa_de_nada(carpeta: Path) -> None:
    """`lee_lote` avisa documento a documento, en el orden del lote."""
    avisados: list[str] = []

    docs = lectura.lee_lote([], trabajadores=4, aviso=lambda d: avisados.append(d.lectura.file_id))

    assert docs == []
    assert avisados == []


def test_lee_lote_avisa_en_orden(carpeta: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lectura.requests, "post", lambda *a, **k: (_ for _ in ()).throw(
        ConnectionError("sin servicio")
    ))
    rutas = [carpeta / f"scan_{i}.pdf" for i in range(4)]
    for ruta in rutas:
        ruta.write_bytes(b"%PDF-1.4 no es un pdf de verdad")
    avisados: list[str] = []

    docs = lectura.lee_lote(rutas, trabajadores=2,
                            aviso=lambda d: avisados.append(d.lectura.file_id))

    assert avisados == [d.lectura.file_id for d in docs] == [r.name for r in rutas]


# ------------------------------------------------------------- de punta a punta
def _facturas_con_capa(carpeta: Path, cuantas: int = 6) -> list[Path]:
    """Copia unas cuantas facturas del corpus que no necesiten OCR.

    Se copian (y no se leen del sitio) para que el lote del test sea pequeno y
    para que `emit.valida_jsonl` compare contra exactamente estos ficheros.
    """
    from conftest import CORPUS

    destino = carpeta / "facturas"
    destino.mkdir()
    elegidas: list[Path] = []
    for pdf in sorted(CORPUS.glob("*.pdf")):
        if len(elegidas) >= cuantas:
            break
        crudo, paginas = lectura.capa_texto(pdf)
        if lectura.calidad_texto(crudo, paginas) < 0.6:
            continue
        copia = destino / pdf.name
        copia.write_bytes(pdf.read_bytes())
        elegidas.append(copia)
    return elegidas


@pytest.mark.lento
def test_reanudar_da_la_misma_entrega_que_una_pasada_entera(
    carpeta: Path, maestro, asientos
) -> None:
    """La promesa del checkpoint: cortar y reanudar no cambia la entrega.

    Es el unico test que lo comprueba de punta a punta (lectura + decision +
    entrega), que es donde puede fallar: la regla de pedidos repetidos mira el
    lote entero, asi que si `procesa` decidiera solo con lo que quedo por leer,
    la entrega reanudada saldria distinta y nadie lo notaria.
    """
    from conftest import LIBRO, REGLAS, SNAPSHOT

    from maisa import procesa

    facturas = _facturas_con_capa(carpeta)
    if len(facturas) < 4:
        pytest.skip("hacen falta al menos 4 facturas con capa de texto en el corpus")
    entera = carpeta / "entera" / "outcomes.jsonl"
    reanudada = carpeta / "reanudada" / "outcomes.jsonl"
    comun = dict(xlsx=LIBRO, config=REGLAS, snapshot=SNAPSHOT, erp_url=None, lote=1,
                 trabajadores=2)

    filas = procesa.procesa(facturas[0].parent, salida=entera, **comun)

    # Checkpoint a medias, como si la pasada anterior hubiera muerto: se leen
    # solo las dos primeras y se deja el fichero sin borrar.
    marca = checkpoint.Checkpoint(reanudada.with_name("outcomes_lecturas.jsonl"))
    with marca:
        lectura.lee_lote(facturas[:2], trabajadores=2, aviso=marca.anota)
    reanudadas = procesa.procesa(facturas[0].parent, salida=reanudada,
                                continuar=True, **comun)

    assert len(filas) == len(reanudadas) == len(facturas)
    assert reanudada.read_bytes() == entera.read_bytes()
    # El checkpoint se retira solo cuando la entrega sale validada: si siguiera
    # ahi, la proxima pasada dudaria de si el lote termino.
    assert not reanudada.with_name("outcomes_lecturas.jsonl").exists()
