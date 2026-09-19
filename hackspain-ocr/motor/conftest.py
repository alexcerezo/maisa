"""Fixtures y utilidades compartidas por la suite del motor Maisa.

La suite **no** modifica los modulos del motor: los importa tal cual. Todo lo
que depende del corpus (los 500 PDFs, el Excel del maestro o el snapshot del
ERP) se salta solo cuando el fichero no esta, para que `pytest` sea verde en
cualquier maquina.

`PYTHONPATH=src` no hace falta: lo pone `pytest.ini`.
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import sys
import tempfile
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent
if str(RAIZ / "src") not in sys.path:
    sys.path.insert(0, str(RAIZ / "src"))

from maisa import erp, excel, lectura, norma, procesa, texto  # noqa: E402

# Las rutas del corpus salen del propio motor, que ya sabe resolver los dos
# arboles posibles (el repositorio de entrega, con `data/`, y el de desarrollo,
# con `corpus/maisa/`).
CORPUS = procesa.FACTURAS_POR_DEFECTO
LIBRO = procesa.XLSX_POR_DEFECTO
SNAPSHOT = Path(os.environ.get("MAISA_SNAPSHOT", str(procesa.SNAPSHOT_POR_DEFECTO)))
CACHE_OCR = lectura.CACHE_OCR
REGLAS = RAIZ / "config" / "reglas.toml"

# Centinela: "usa lo que dice el ERP/maestro para este pedido".
AUTO = object()


# --------------------------------------------------------------------- utils
def importe_es(valor: Decimal | str) -> str:
    """``3012.89`` -> ``'3.012,89'`` (formato de las facturas espanolas)."""
    crudo = f"{Decimal(str(valor)):,.2f}"
    return crudo.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def iban_agrupado(iban: str) -> str:
    """``'ES2100491500051234567890'`` -> ``'ES21 0049 1500 ...'``."""
    return " ".join(iban[i:i + 4] for i in range(0, len(iban), 4))


def texto_factura(
    *,
    pedido: str | None,
    total: Decimal | str,
    fecha: str = "05/01/2026",
    nif: str | None = None,
    iban: str | None = None,
    razon_social: str | None = "Suministros Levante S.L.",
    nota: str = "",
    base: Decimal | str | None = None,
    iva: Decimal | str | None = None,
    cliente: bool = True,
) -> str:
    """Factura sintetica con la maqueta que el motor reconoce.

    Un campo a ``None`` simplemente no aparece impreso (asi se prueba la
    herencia de identidad y los pedidos ilegibles). El IVA se calcula al 21%
    para que la aritmetica del documento cuadre.
    """
    total_d = Decimal(str(total))
    if base is None:
        base_d = (total_d / Decimal("1.21")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    else:
        base_d = Decimal(str(base))
    iva_d = total_d - base_d if iva is None else Decimal(str(iva))

    lineas = ["FACTURA", f"Factura: 2026/11604    Fecha: {fecha}"]
    if pedido:
        lineas.append(f"Pedido: {pedido}")
    if razon_social:
        lineas.append(razon_social)
    if nif:
        lineas.append(f"NIF: {nif}")
    if iban:
        lineas.append(f"IBAN: {iban_agrupado(iban)}")
    lineas += [
        f"Base: {importe_es(base_d)}",
        f"IVA (21%): {importe_es(iva_d)}",
        f"TOTAL: {importe_es(total_d)}",
    ]
    if cliente:
        # El CIF del destinatario nunca es el del emisor: no debe colarse.
        lineas.append("Cliente: Banco Miralmar S.A. CIF: A58231074")
    if nota:
        lineas.append(f"Condiciones de pago: transferencia a 30 dias. {nota}")
    lineas += [
        "Documento emitido conforme al RD 1619/2012.",
        "Documento generado por el sistema de facturacion del proveedor.",
    ]
    return "\n".join(lineas) + "\n"


def lee_texto(cuerpo: str, file_id: str = "sintetica.pdf", paginas: int = 1,
              metodo: str = "texto_determinista", meta: str = "") -> texto.Lectura:
    return texto.extrae(cuerpo, file_id, paginas, metodo, meta)


# ------------------------------------------------------------------ corpus
def ruta_pdf(nombre: str) -> Path:
    return CORPUS / nombre


def exige_pdf(nombre: str) -> Path:
    """Ruta de un PDF del corpus; salta el test si el corpus no esta."""
    ruta = CORPUS / nombre
    if not ruta.exists():
        pytest.skip(f"no esta en el corpus: {nombre}")
    return ruta


def tiene_cache_ocr(ruta: Path) -> bool:
    return (CACHE_OCR / f"{lectura.sha256_pdf(ruta)}.json").exists()


def exige_lectura(nombre: str, umbral: float = 0.6) -> lectura.Documento:
    """Lee un PDF del corpus sin depender del servicio de OCR.

    Si el PDF no tiene capa de texto utilizable se exige que su OCR ya este en
    la cache (`maisa/.cache/ocr/`); si no, el test se salta en vez de intentar
    levantar el contenedor de vision.
    """
    ruta = exige_pdf(nombre)
    if not tiene_cache_ocr(ruta):
        crudo, paginas = lectura.capa_texto(ruta)
        if lectura.calidad_texto(crudo, paginas) < umbral:
            pytest.skip(f"{nombre} necesita OCR y no esta en la cache")
    return lectura.lee(ruta)


# ----------------------------------------------------------- mundo sintetico
@dataclasses.dataclass(frozen=True)
class Mundo:
    """Maestro + ERP en memoria: la suite no depende del Excel ni del bridge."""

    maestro: excel.Maestro
    asientos: dict[str, erp.Asiento]
    decisor: norma.Decisor
    importes: dict[str, Decimal]
    pedido_base: str = "PO-2026-0096"

    def proveedor(self, pedido: str) -> excel.Proveedor:
        proveedor = self.maestro.proveedor_de_pedido(pedido)
        assert proveedor is not None, f"{pedido} no tiene proveedor en el mundo de prueba"
        return proveedor

    def nif(self, pedido: str) -> str:
        return self.proveedor(pedido).nif

    def iban(self, pedido: str) -> str:
        return self.proveedor(pedido).iban

    def importe(self, pedido: str) -> Decimal:
        return self.importes[pedido]

    def estado(self, pedido: str) -> str:
        return self.asientos[pedido].estado


#: Proveedores y pedidos del mundo de prueba. Los importes imitan el enunciado
#: (incluido el 1234,50 contra 1200,00 del caso descuadrado).
_PROVEEDORES = (
    ("P001", "Suministros Levante S.L.", "B46102331", "ES2100491500051234567890"),
    ("P002", "Catering Hermanos Pico S.L.", "B96233419", "ES1800815290070001234567"),
    ("P003", "Electricidad Montcada S.A.", "A46990201", "ES3520385778983000765410"),
)
_PEDIDOS = (
    ("PO-2026-0096", "P001", "3012.89", "PENDIENTE"),
    ("PO-2026-0492", "P002", "1512.50", "PENDIENTE"),
    ("PO-2026-0803", "P003", "1076.90", "PAGADA"),
    ("PO-2026-0814", "P003", "1200.00", "PENDIENTE"),
)


def mundo_sintetico(politica: norma.Politica) -> Mundo:
    proveedores = {
        pid: excel.Proveedor(pid, razon, nif, iban, "Mislata", "30 dias")
        for pid, razon, nif, iban in _PROVEEDORES
    }
    pedidos: dict[str, excel.Pedido] = {}
    asientos: dict[str, erp.Asiento] = {}
    importes: dict[str, Decimal] = {}
    for ped, pid, importe, estado in _PEDIDOS:
        prov = proveedores[pid]
        importe_d = Decimal(importe)
        pedidos[ped] = excel.Pedido(ped, pid, prov.nif, importe_d, estado, "2026-01-05")
        asientos[ped] = erp.Asiento(
            id=f"ASI-{ped}", fecha="2026-01-05", proveedor=pid, nif=prov.nif,
            pedido=ped, importe=importe_d, estado=estado,
        )
        importes[ped] = importe_d
    por_nif: dict[str, list[excel.Proveedor]] = {}
    por_iban: dict[str, list[excel.Proveedor]] = {}
    for prov in proveedores.values():
        por_nif.setdefault(prov.nif, []).append(prov)
        por_iban.setdefault(prov.iban, []).append(prov)
    maestro = excel.Maestro(
        proveedores=proveedores, por_nif=por_nif, por_iban=por_iban, pedidos=pedidos,
        norma=[], pendientes_revisar=[], duplicados=[], sha256="0" * 64, hoja_estado={},
    )
    return Mundo(maestro, asientos, norma.Decisor(maestro, asientos, politica), importes)


class Sintetica:
    """Fabrica de facturas sinteticas ya conciliadas contra el mundo de prueba."""

    def __init__(self, mundo: Mundo) -> None:
        self.mundo = mundo

    def texto(self, *, pedido: str | None = AUTO, total: Decimal | str | None = AUTO,
              fecha: str = "05/01/2026", nif: str | None = AUTO,
              iban: str | None = AUTO, nota: str = "",
              razon_social: str | None = AUTO, **extra) -> str:
        pedido_real = self.mundo.pedido_base if pedido is AUTO else pedido

        def deduce(valor, campo, calcula, por_defecto=None):
            if valor is not AUTO:
                return valor
            if pedido_real and pedido_real in self.mundo.asientos:
                return calcula(pedido_real)
            if por_defecto is not None:
                return por_defecto
            raise AssertionError(f"sin pedido conocido hay que dar {campo} explicito")

        total_real = deduce(total, "el importe", self.mundo.importe)
        nif_real = deduce(nif, "el NIF", self.mundo.nif)
        iban_real = deduce(iban, "el IBAN", self.mundo.iban)
        razon = deduce(razon_social, "la razon social",
                       lambda p: self.mundo.proveedor(p).razon_social,
                       por_defecto="Proveedor Sintetico S.L.")
        return texto_factura(pedido=pedido_real, total=total_real, fecha=fecha,
                             nif=nif_real, iban=iban_real, nota=nota,
                             razon_social=razon, **extra)

    def lectura(self, *, metodo: str = "texto_determinista", file_id: str = "sintetica.pdf",
                paginas: int = 1, meta: str = "", **kw) -> texto.Lectura:
        return lee_texto(self.texto(**kw), file_id=file_id, paginas=paginas,
                         metodo=metodo, meta=meta)

    def decide(self, **kw) -> norma.Decision:
        return self.mundo.decisor.decide(self.lectura(**kw))


# ------------------------------------------------------------------ fixtures
@pytest.fixture(scope="session")
def politica() -> norma.Politica:
    return norma.Politica.carga(REGLAS)


@pytest.fixture(scope="session")
def maestro() -> excel.Maestro:
    if not LIBRO.exists():
        pytest.skip(f"falta el libro del maestro: {LIBRO}")
    return excel.carga(LIBRO)


@pytest.fixture(scope="session")
def asientos() -> dict[str, erp.Asiento]:
    if not SNAPSHOT.exists():
        pytest.skip(f"falta el snapshot del ERP: {SNAPSHOT}")
    return {a.pedido: a for a in erp.carga_snapshot(SNAPSHOT)}


@pytest.fixture(scope="session")
def decisor(maestro: excel.Maestro, asientos: dict[str, erp.Asiento],
            politica: norma.Politica) -> norma.Decisor:
    return norma.Decisor(maestro, asientos, politica)


@pytest.fixture(scope="session")
def mundo(politica: norma.Politica) -> Mundo:
    return mundo_sintetico(politica)


@pytest.fixture
def sintetica(mundo: Mundo) -> Sintetica:
    return Sintetica(mundo)


@pytest.fixture
def carpeta():
    """Carpeta temporal dentro del proyecto (nunca `/tmp`), borrada al final."""
    base = RAIZ / ".cache" / "pytest_tmp"
    base.mkdir(parents=True, exist_ok=True)
    temporal = Path(tempfile.mkdtemp(dir=base))
    yield temporal
    shutil.rmtree(temporal, ignore_errors=True)
