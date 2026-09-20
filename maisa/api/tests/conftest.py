"""Fixtures comunes.

Los tests NO necesitan Mongo ni el OCR: la traza vive en un fichero temporal y
las dependencias externas se sustituyen por dobles. Mongo se apunta a un puerto
cerrado (127.0.0.1:1) con timeout minimo, de modo que el caso "dependencia
caida" se prueba de verdad, sin simularlo.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.almacen import (
    ESTADO_INICIAL,
    ESTADO_TRAS_OCR,
    ESQUEMA_VERSION,
    FacturaDuplicada,
    Subida,
    lineas_desde_ocr,
    motor_desde_engine,
    normalizar_file_id,
)
from app.config import Settings
from app.deps import get_almacen, get_mongo, get_ocr
from app.errors import ApiError
from app.main import create_app
from app.mongo_repo import MongoNoDisponible

API_KEY = "clave-de-prueba-1234"

# Traza de juguete: incluye a proposito metacaracteres de regex en el proveedor
# ("Talleres Rios (S.A.)") y en el nombre ("P001+ Logistica") para poder probar
# el escapado de la busqueda.
REGISTROS = [
    {
        "file_id": "2026-01-08_P001.pdf",
        "result": "PAGAR",
        "motivos": [],
        "campos": {
            "file_id": "2026-01-08_P001.pdf",
            "metodo_lectura": "texto_determinista",
            "nota_documento": "Servicio mensual ....... 2.489,99",
            "pedido": "PO-2026-0096",
            "asiento": "AS-00096",
            "proveedor_id": "P001",
            "estado_erp": "PENDIENTE",
            "importe_erp": "3012.89",
            "proveedor": "Suministros Levante S.L.",
            "nif_maestro": "B46102331",
            "base": "2489.99",
            "iva": "522.90",
            "total": "3012.89",
            "iva_pct": "21",
            "desvio_importe": "0.00",
            "fecha": "2026-01-08",
        },
        "identificacion_fiable": True,
        "version_norma": "norma_v3.1",
        "metodo_lectura": "texto_determinista",
        "hechos": [
            {
                "regla": "R1_identidad",
                "ok": True,
                "motivo": "NIF del emisor coincide con el maestro",
                "datos": {"nif": "B46102331"},
                "duro": False,
                "nombre": "",
                "informativo": False,
            }
        ],
        "lote": 1,
        "sha256": "a" * 64,
        "escalon_lectura": "capa_texto",
        "calidad_lectura": 1.0,
        "segundos_lectura": 0.042,
        "sospechosos": [],
        "sospechosos_meta": [],
        "nota_documento": "Servicio mensual ....... 2.489,99",
    },
    {
        "file_id": "2026-02-01_P002.pdf",
        "result": "ESCALAR",
        "motivos": ["fecha invalida: 31/02/2026"],
        "campos": {
            "file_id": "2026-02-01_P002.pdf",
            "metodo_lectura": "vision_ocr",
            "pedido": "PO-2026-0200",
            "asiento": "AS-00200",
            "proveedor_id": "P002",
            "estado_erp": "PENDIENTE",
            "importe_erp": "1512.5",
            "proveedor": "Talleres Rios (S.A.)",
            "nif_maestro": "B46102332",
            "total": "1512.50",
            "desvio_importe": "0.00",
            "fecha": "2026-02-31",
        },
        "identificacion_fiable": True,
        "version_norma": "norma_v3.1",
        "metodo_lectura": "vision_ocr",
        "hechos": [
            {
                "regla": "R4_fecha",
                "ok": False,
                "motivo": "fecha invalida",
                "datos": {"fecha": "2026-02-31"},
                "duro": True,
                "nombre": "",
                "informativo": False,
            }
        ],
        "lote": 1,
        "sha256": "b" * 64,
        "escalon_lectura": "vision",
        "calidad_lectura": 0.87,
        "segundos_lectura": 1.5,
        "sospechosos": ["2026-02-31"],
        "sospechosos_meta": [],
        "nota_documento": "",
    },
    {
        "file_id": "2026-02-02_P003.pdf",
        "result": "NO_PAGAR",
        "motivos": ["PO-2026-0476 ya esta PAGADA en el ERP: no se paga dos veces"],
        "campos": {
            "file_id": "2026-02-02_P003.pdf",
            "metodo_lectura": "texto_determinista",
            "pedido": "PO-2026-0476",
            "asiento": "AS-00476",
            "proveedor_id": "P003",
            "estado_erp": "PAGADA",
            "importe_erp": "1000.00",
            "proveedor": "P001+ Logistica",
            "nif_maestro": "B46102333",
            "total": "1010.00",
            "desvio_importe": "10.00",
            "fecha": "2026-02-02",
        },
        "identificacion_fiable": False,
        "version_norma": "norma_v3.0",
        "metodo_lectura": "texto_determinista",
        "hechos": [],
        "lote": 2,
        "sha256": "c" * 64,
        "escalon_lectura": "capa_texto",
        "calidad_lectura": 0.95,
        "segundos_lectura": 0.1,
        "sospechosos": [],
        "sospechosos_meta": [],
        "nota_documento": "",
    },
    {
        "file_id": "2026-02-03_P004.pdf",
        "result": "PAGAR",
        "motivos": [],
        "campos": {
            "file_id": "2026-02-03_P004.pdf",
            "metodo_lectura": "texto_determinista",
            "pedido": "PO-2026-0477",
            "asiento": "AS-00477",
            "proveedor_id": "P001",
            "estado_erp": "PENDIENTE",
            "importe_erp": "200.00",
            "proveedor": "Suministros Levante S.L.",
            "nif_maestro": "B46102331",
            "total": "200.00",
            "desvio_importe": "0.00",
            "fecha": "2026-02-03",
        },
        "identificacion_fiable": True,
        "version_norma": "norma_v3.1",
        "metodo_lectura": "texto_determinista",
        "hechos": [
            {
                "regla": "R5_estado",
                "ok": False,
                "motivo": "el pedido esta PENDIENTE en el ERP",
                "datos": {"estado": "PENDIENTE"},
                "duro": False,
                "nombre": "",
                "informativo": True,
            }
        ],
        "lote": 2,
        "sha256": "d" * 64,
        "escalon_lectura": "capa_texto",
        "calidad_lectura": 1.0,
        "segundos_lectura": 0.05,
        "sospechosos": [],
        "sospechosos_meta": [],
        "nota_documento": "",
    },
]

PDF_VALIDO = "2026-01-08_P001.pdf"
PDF_BYTES = b"%PDF-1.4\n% factura de prueba\n%%EOF\n"


def escribir_traza(ruta: Path, registros: list[dict] | None = None) -> Path:
    registros = REGISTROS if registros is None else registros
    with ruta.open("w", encoding="utf-8") as fichero:
        for registro in registros:
            fichero.write(json.dumps(registro, ensure_ascii=False) + "\n")
    return ruta


@pytest.fixture
def outputs_dir(tmp_path: Path) -> Path:
    directorio = tmp_path / "outputs"
    directorio.mkdir()
    escribir_traza(directorio / "outcomes_traza.jsonl")
    with (directorio / "outcomes.jsonl").open("w", encoding="utf-8") as fichero:
        for registro in REGISTROS:
            fichero.write(json.dumps({"file_id": registro["file_id"], "result": registro["result"]}) + "\n")
    return directorio


@pytest.fixture
def facturas_dir(tmp_path: Path) -> Path:
    directorio = tmp_path / "facturas"
    directorio.mkdir()
    (directorio / PDF_VALIDO).write_bytes(PDF_BYTES)
    return directorio


@pytest.fixture
def ui_dir(tmp_path: Path) -> Path:
    directorio = tmp_path / "ui"
    directorio.mkdir()
    (directorio / ".gitkeep").write_text("")
    return directorio


def construir_settings(
    outputs_dir: Path,
    facturas_dir: Path,
    ui_dir: Path,
    *,
    api_key: str | None = None,
    mongo_uri: str = "mongodb://127.0.0.1:1/albertitos",
) -> Settings:
    return Settings(
        mongo_uri=mongo_uri,
        mongo_db="albertitos",
        mongo_timeout_ms=150,
        ocr_url="http://127.0.0.1:1",
        outputs_dir=outputs_dir,
        facturas_dir=facturas_dir,
        ui_dir=ui_dir,
        api_key=api_key,
        health_timeout_s=0.5,
        max_upload_mb=1.0,
        default_limit=50,
        max_limit=500,
    )


@pytest.fixture
def settings(outputs_dir: Path, facturas_dir: Path, ui_dir: Path) -> Settings:
    return construir_settings(outputs_dir, facturas_dir, ui_dir)


@pytest.fixture
def settings_con_api_key(outputs_dir: Path, facturas_dir: Path, ui_dir: Path) -> Settings:
    return construir_settings(outputs_dir, facturas_dir, ui_dir, api_key=API_KEY)


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as cliente:
        yield cliente


@pytest.fixture
def client_con_api_key(settings_con_api_key: Settings):
    with TestClient(create_app(settings_con_api_key)) as cliente:
        yield cliente


# --------------------------------------------------------------------------- #
# Dobles de las dependencias externas
# --------------------------------------------------------------------------- #
class FakeMongo:
    """Doble de MongoRepo con la misma superficie asincrona que el real."""

    def __init__(
        self,
        asientos: list[dict] | None = None,
        snapshots: list[dict] | None = None,
        revisiones: dict[str, dict] | None = None,
        *,
        error: str | None = None,
    ) -> None:
        self.db_nombre = "albertitos"
        self._error = error
        self.asientos = asientos if asientos is not None else ASIENTOS
        self.snapshots = snapshots if snapshots is not None else SNAPSHOTS
        self.revisiones: dict[str, dict] = revisiones if revisiones is not None else {}

    def _comprobar(self) -> None:
        if self._error:
            raise MongoNoDisponible(self._error)

    async def ping_async(self) -> None:
        self._comprobar()

    def indices_faltantes(self) -> dict:
        self._comprobar()
        return {}

    async def listar_asientos(self, *, vigente=None, q=None, limit=50, offset=0):
        self._comprobar()
        filas = list(self.asientos)
        if vigente is not None:
            filas = [fila for fila in filas if fila.get("vigente") is vigente]
        if q:
            texto = q.lower()
            filas = [
                fila
                for fila in filas
                if any(texto in str(fila.get(campo, "")).lower() for campo in ("asiento_id", "nif", "pedido"))
            ]
        # Mismo orden que el repositorio real: asiento ascendente, snapshot reciente primero.
        filas.sort(key=lambda fila: fila["snapshot_id"], reverse=True)
        filas.sort(key=lambda fila: fila["asiento_id"])
        return filas[offset : offset + limit], len(filas)

    async def obtener_asiento(self, asiento_id: str):
        self._comprobar()
        for fila in self.asientos:
            if fila["asiento_id"] == asiento_id:
                return fila
        return None

    async def listar_snapshots(self, *, limit=50, offset=0):
        self._comprobar()
        filas = sorted(self.snapshots, key=lambda fila: fila["descargado_en"], reverse=True)
        return filas[offset : offset + limit], len(filas)

    async def contar_asientos_vigentes(self) -> int:
        self._comprobar()
        return sum(1 for fila in self.asientos if fila.get("vigente"))

    async def listar_revisiones(self, file_ids: list[str]) -> dict[str, dict]:
        self._comprobar()
        return {file_id: self.revisiones[file_id] for file_id in file_ids if file_id in self.revisiones}

    async def obtener_revision(self, file_id: str) -> dict | None:
        self._comprobar()
        return self.revisiones.get(file_id)

    async def marcar_revision(
        self, file_id: str, *, estado: str, revisor: str | None = None, comentario: str | None = None
    ) -> dict:
        self._comprobar()
        doc = {"_id": file_id, "estado": estado, "revisor": revisor, "comentario": comentario,
               "actualizado_en": "2026-01-01T00:00:00"}
        self.revisiones[file_id] = doc
        return doc


class FakeOcr:
    """Doble de OcrClient."""

    def __init__(self, *, error: Exception | None = None, texto: str = "FACTURA 123\nTOTAL 100,00") -> None:
        self.base_url = "http://fake-ocr:8866"
        self._error = error
        self._texto = texto
        self.llamadas: list[dict] = []

    async def salud(self, timeout_s: float) -> dict:
        if self._error:
            raise self._error
        return {"status": "ok", "engine": "local"}

    async def procesar(self, *, nombre, contenido, content_type, engine=None, detalle=False) -> dict:
        self.llamadas.append(
            {
                "nombre": nombre,
                "bytes": len(contenido),
                "content_type": content_type,
                "engine": engine,
                "detalle": detalle,
            }
        )
        if self._error:
            raise self._error
        return {
            "texto": self._texto,
            "score": 0.93,
            "motor": engine or "local",
            "paginas": 1,
            "lineas": 4,
            "segundos_ocr": 0.8,
            "segundos_proxy": 0.81,
            "bytes_enviados": len(contenido),
            "stats": {"mean_score": 0.93},
            "bruto": {"text": self._texto, "engine": engine or "local"},
        }


class FakeAlmacen:
    """Doble de `AlmacenFacturas` con memoria en vez de Mongo y GridFS.

    Reutiliza `normalizar_file_id`, `lineas_desde_ocr` y `motor_desde_engine`
    del modulo real: asi los tests comprueban el comportamiento de verdad y no
    una reimplementacion paralela que se puede desincronizar.
    """

    def __init__(self, *, error: Exception | None = None) -> None:
        self.db_nombre = "albertitos"
        self._error = error
        self.expedientes: dict[str, dict] = {}
        self.pdfs: dict[str, bytes] = {}
        self.eventos: list[dict] = []

    def _comprobar(self) -> None:
        if self._error:
            raise self._error

    async def guardar(
        self,
        *,
        nombre_original: str,
        contenido: bytes,
        content_type: str | None = None,
        lote_id: str = "lote1",
        ocr: dict | None = None,
        run_id: str = "api",
    ) -> Subida:
        self._comprobar()
        file_id = normalizar_file_id(nombre_original)
        sha256 = hashlib.sha256(contenido).hexdigest()
        previo = self.expedientes.get(file_id)
        if previo is not None:
            if previo["documento"]["sha256"] == sha256:
                return Subida(
                    file_id=file_id,
                    sha256=sha256,
                    tamano_bytes=len(contenido),
                    lote_id=previo["lote_id"],
                    estado_proceso=previo["estado_proceso"],
                    creado_en=datetime.now(timezone.utc),
                    duplicado=True,
                    expediente=previo,
                )
            raise FacturaDuplicada(f"Ya existe una factura '{file_id}' con otro contenido.")

        lineas = lineas_desde_ocr((ocr or {}).get("bruto") or {})
        motor = motor_desde_engine((ocr or {}).get("motor"), bool(lineas))
        estado = ESTADO_TRAS_OCR if ocr is not None else ESTADO_INICIAL
        ahora = datetime.now(timezone.utc)
        documento: dict = {
            "_id": file_id,
            "lote_id": lote_id,
            "estado_proceso": estado,
            "esquema_version": ESQUEMA_VERSION,
            "creado_en": ahora.isoformat(),
            "actualizado_en": ahora.isoformat(),
            "documento": {
                "nombre_original": nombre_original,
                "sha256": sha256,
                "tamano_bytes": len(contenido),
                "gridfs_id": f"gridfs-{file_id}",
            },
            "ocr": {"motor": motor, "lineas": lineas, "disponible": ocr is not None},
            "decision": None,
        }
        self.expedientes[file_id] = documento
        self.pdfs[file_id] = contenido

        eventos = ["EXPEDIENTE_ESTADO"]
        self.eventos.append({"tipo": "EXPEDIENTE_ESTADO", "file_id": file_id, "run_id": run_id})
        if ocr is not None:
            tipo = "OCR_OK" if lineas or ocr.get("texto") else "OCR_FAIL"
            self.eventos.append({"tipo": tipo, "file_id": file_id, "run_id": run_id})
            eventos.append(tipo)
        return Subida(
            file_id=file_id,
            sha256=sha256,
            tamano_bytes=len(contenido),
            lote_id=lote_id,
            estado_proceso=estado,
            creado_en=ahora,
            duplicado=False,
            expediente=documento,
            eventos=tuple(eventos),
        )

    async def evento(self, **kwargs) -> None:
        self._comprobar()
        self.eventos.append(dict(kwargs))

    async def obtener(self, file_id: str) -> dict | None:
        self._comprobar()
        return self.expedientes.get(file_id)

    async def listar(self, *, lote_id=None, estado=None, limit=50, offset=0):
        self._comprobar()
        filas = [
            documento
            for documento in self.expedientes.values()
            if (lote_id is None or documento["lote_id"] == lote_id)
            and (estado is None or documento["estado_proceso"] == estado)
        ]
        return filas[offset : offset + limit], len(filas)

    async def abrir_pdf(self, file_id: str) -> bytes | None:
        self._comprobar()
        return self.pdfs.get(file_id)

    async def estado(self) -> dict:
        self._comprobar()
        return {
            "bucket": "pdfs",
            "expedientes": len(self.expedientes),
            "pdfs": len(self.pdfs),
            "eventos": len(self.eventos),
        }


ASIENTOS = [
    {
        "_id": "snap-2026-01-01#AS-00096",
        "asiento_id": "AS-00096",
        "snapshot_id": "snap-2026-01-01",
        "fecha": "2026-01-08T00:00:00",
        "proveedor": "Suministros Levante S.L.",
        "nif": "B46102331",
        "pedido": "PO-2026-0096",
        "importe": 3012.89,
        "estado": "PENDIENTE",
        "vigente": True,
        "esquema_version": 1,
    },
    {
        "_id": "snap-2025-12-01#AS-00096",
        "asiento_id": "AS-00096",
        "snapshot_id": "snap-2025-12-01",
        "fecha": "2026-01-08T00:00:00",
        "proveedor": "Suministros Levante S.L.",
        "nif": "B46102331",
        "pedido": "PO-2026-0096",
        "importe": 3012.89,
        "estado": "PENDIENTE",
        "vigente": False,
        "esquema_version": 1,
    },
    {
        "_id": "snap-2026-01-01#AS-00476",
        "asiento_id": "AS-00476",
        "snapshot_id": "snap-2026-01-01",
        "fecha": "2026-02-02T00:00:00",
        "proveedor": "P001+ Logistica",
        "nif": "B46102333",
        "pedido": "PO-2026-0476",
        "importe": 1000.0,
        "estado": "PAGADA",
        "vigente": True,
        "esquema_version": 1,
    },
]

SNAPSHOTS = [
    {
        "_id": "snap-2026-01-01",
        "descargado_en": "2026-01-01T10:00:00",
        "total_asientos": 516,
        "paginas": 26,
        "vigente": True,
        "estado": "COMPLETO",
        "reintentos": {"ora_00600": 3, "ses_401": 1, "erp_429": 0},
        "duracion_ms": 42000,
        "esquema_version": 1,
    },
    {
        "_id": "snap-2025-12-01",
        "descargado_en": "2025-12-01T10:00:00",
        "total_asientos": 400,
        "paginas": 20,
        "vigente": False,
        "estado": "COMPLETO",
        "reintentos": {"ora_00600": 0, "ses_401": 0, "erp_429": 0},
        "duracion_ms": 30000,
        "esquema_version": 1,
    },
]


@pytest.fixture
def fake_mongo() -> FakeMongo:
    return FakeMongo()


@pytest.fixture
def fake_ocr() -> FakeOcr:
    return FakeOcr()


@pytest.fixture
def fake_almacen() -> FakeAlmacen:
    return FakeAlmacen()


@pytest.fixture
def cliente_con_fakes(
    settings: Settings, fake_mongo: FakeMongo, fake_ocr: FakeOcr, fake_almacen: FakeAlmacen
):
    """App con Mongo, OCR y el almacen sustituidos por dobles."""
    app = create_app(settings)
    app.dependency_overrides[get_mongo] = lambda: fake_mongo
    app.dependency_overrides[get_ocr] = lambda: fake_ocr
    app.dependency_overrides[get_almacen] = lambda: fake_almacen
    with TestClient(app) as cliente:
        yield cliente
    app.dependency_overrides.clear()


__all__ = [
    "API_KEY",
    "PDF_BYTES",
    "PDF_VALIDO",
    "ApiError",
    "FakeAlmacen",
    "FakeMongo",
    "FakeOcr",
    "construir_settings",
    "escribir_traza",
]
