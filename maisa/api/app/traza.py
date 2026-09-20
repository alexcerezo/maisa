"""Lectura de la traza del motor (`outcomes_traza.jsonl`) y de la entrega.

Decision de diseno: la traza se carga **en memoria** (500 lineas, ~1.3 MB) en
lugar de releerse en cada peticion. Se comprueba el `mtime`/tamano en cada
acceso, asi que si el motor vuelve a escribir la traza la API la recarga sola sin
reiniciar. Mongo no guarda aun `expedientes`, por lo que la traza en disco es
hoy la unica fuente de verdad de las decisiones.

Aqui no se escribe nada: el fichero de la traza es material de auditoria del
motor y la API solo lo lee.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("albertitos-api")

RESULTADOS = ("PAGAR", "NO_PAGAR", "ESCALAR")

# Firma que se publica cuando no hay traza legible: es estable a proposito, para
# que un cliente que cachea no vea cambiar el ETag sin motivo.
FIRMA_SIN_TRAZA = "sin-traza"

# Tamano del bloque con el que se calcula el sha256 del fichero.
_BLOQUE_HASH = 1 << 20

# Campos del resumen que se indexan para la busqueda de texto libre.
CAMPOS_BUSQUEDA = ("file_id", "proveedor", "pedido", "asiento")


def _num(valor: Any) -> float | None:
    """Convierte los importes de la traza (vienen como str) a numero."""
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    try:
        return float(str(valor).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _texto(valor: Any) -> str | None:
    if valor is None:
        return None
    texto = str(valor).strip()
    return texto or None


def motivo_principal(registro: dict) -> str | None:
    """Motivo que resume la decision, para la columna del visor.

    Orden: primer motivo explicito -> primer hecho incumplido (los duros primero)
    -> None cuando no hay ninguna incidencia que contar.
    """
    motivos = [m for m in (registro.get("motivos") or []) if _texto(m)]
    if motivos:
        return str(motivos[0])
    hechos = registro.get("hechos") or []
    incumplidos = [h for h in hechos if not h.get("ok")]
    incumplidos.sort(key=lambda h: (not h.get("duro"), bool(h.get("informativo"))))
    for hecho in incumplidos:
        texto = _texto(hecho.get("motivo")) or _texto(hecho.get("regla"))
        if texto:
            return texto
    return None


def resumir(registro: dict) -> dict:
    """Fila compacta para el listado del visor."""
    campos = registro.get("campos") or {}
    return {
        "file_id": registro.get("file_id"),
        "resultado": registro.get("result"),
        "motivo_principal": motivo_principal(registro),
        "proveedor": _texto(campos.get("proveedor")),
        "proveedor_id": _texto(campos.get("proveedor_id")),
        "nif": _texto(campos.get("nif_maestro")) or _texto(campos.get("nif_asiento")),
        "asiento": _texto(campos.get("asiento")),
        "pedido": _texto(campos.get("pedido")),
        "fecha": _texto(campos.get("fecha")),
        "total": _num(campos.get("total")),
        "importe_erp": _num(campos.get("importe_erp")),
        "desvio_importe": _num(campos.get("desvio_importe")),
        "estado_erp": _texto(campos.get("estado_erp")),
        "lote": registro.get("lote"),
        "metodo_lectura": registro.get("metodo_lectura"),
        "escalon_lectura": registro.get("escalon_lectura"),
        "calidad_lectura": _num(registro.get("calidad_lectura")),
        "segundos_lectura": _num(registro.get("segundos_lectura")),
        "identificacion_fiable": registro.get("identificacion_fiable"),
        "version_norma": registro.get("version_norma"),
    }


def detallar(registro: dict) -> dict:
    """Vista completa de una factura para el panel de detalle."""
    return {
        "file_id": registro.get("file_id"),
        "resultado": registro.get("result"),
        "motivo_principal": motivo_principal(registro),
        "motivos": registro.get("motivos") or [],
        "hechos": registro.get("hechos") or [],
        "campos": registro.get("campos") or {},
        "lectura": {
            "metodo_lectura": registro.get("metodo_lectura"),
            "escalon_lectura": registro.get("escalon_lectura"),
            "calidad_lectura": _num(registro.get("calidad_lectura")),
            "segundos_lectura": _num(registro.get("segundos_lectura")),
            "sospechosos": registro.get("sospechosos") or [],
            "sospechosos_meta": registro.get("sospechosos_meta") or [],
        },
        "lote": registro.get("lote"),
        "sha256": registro.get("sha256"),
        "identificacion_fiable": registro.get("identificacion_fiable"),
        "version_norma": registro.get("version_norma"),
        "nota_documento": registro.get("nota_documento"),
        "resumen": resumir(registro),
    }


@dataclass(frozen=True)
class _Firma:
    """Identidad del fichero en disco, para saber si hay que recargar."""

    mtime_ns: int
    tamano: int

    @classmethod
    def de(cls, ruta: Path) -> "_Firma | None":
        try:
            st = ruta.stat()
        except OSError:
            return None
        return cls(st.st_mtime_ns, st.st_size)


def _sha256_fichero(ruta: Path) -> str | None:
    """sha256 del contenido del fichero, o None si no se puede leer."""
    try:
        digest = hashlib.sha256()
        with ruta.open("rb") as fichero:
            for bloque in iter(lambda: fichero.read(_BLOQUE_HASH), b""):
                digest.update(bloque)
    except OSError:
        return None
    return digest.hexdigest()


class TrazaStore:
    """Traza del motor en memoria, con recarga automatica si cambia el fichero."""

    def __init__(self, ruta: Path) -> None:
        self.ruta = ruta
        self._lock = threading.Lock()
        self._firma: _Firma | None = None
        self._hash: str | None = None
        self._registros: list[dict] = []
        self._resumenes: list[dict] = []
        self._indice: dict[str, int] = {}
        self._lineas_invalidas = 0

    # ------------------------------------------------------------------ #
    # Carga
    # ------------------------------------------------------------------ #
    def cargar(self, forzar: bool = False) -> None:
        """Recarga la traza si el fichero cambio (o siempre, con `forzar`).

        Solo se lee el fichero cuando su identidad en disco (`mtime`/tamano)
        cambia; el sha256 del contenido se calcula en esa misma recarga, no en
        cada consulta.
        """
        firma = _Firma.de(self.ruta)
        with self._lock:
            if not forzar and firma is not None and firma == self._firma:
                return
            registros: list[dict] = []
            resumenes: list[dict] = []
            indice: dict[str, int] = {}
            invalidas = 0
            hash_contenido: str | None = None
            if firma is not None:
                hash_contenido = _sha256_fichero(self.ruta)
                with self.ruta.open("r", encoding="utf-8") as fichero:
                    for numero, linea in enumerate(fichero, start=1):
                        linea = linea.strip()
                        if not linea:
                            continue
                        try:
                            registro = json.loads(linea)
                        except json.JSONDecodeError:
                            invalidas += 1
                            logger.warning("Traza: linea %s ilegible, se ignora.", numero)
                            continue
                        file_id = _texto(registro.get("file_id"))
                        if not file_id:
                            invalidas += 1
                            logger.warning("Traza: linea %s sin file_id, se ignora.", numero)
                            continue
                        indice.setdefault(file_id, len(registros))
                        registros.append(registro)
                        resumenes.append(resumir(registro))
            self._firma = firma
            self._hash = hash_contenido
            self._registros = registros
            self._resumenes = resumenes
            self._indice = indice
            self._lineas_invalidas = invalidas
            logger.info(
                "Traza cargada: %s facturas%s (%s)",
                len(registros),
                f", {invalidas} lineas ignoradas" if invalidas else "",
                self.ruta,
            )

    def _asegurar(self) -> None:
        self.cargar()

    # ------------------------------------------------------------------ #
    # Consultas
    # ------------------------------------------------------------------ #
    @property
    def disponible(self) -> bool:
        self._asegurar()
        return bool(self._registros)

    def total(self) -> int:
        self._asegurar()
        return len(self._registros)

    def lineas_invalidas(self) -> int:
        self._asegurar()
        return self._lineas_invalidas

    def firma(self) -> str:
        """Firma estable del contenido cargado (sha256 del fichero de traza).

        Es la identidad de la traza para quien cachea: dos lecturas con la misma
        firma describen exactamente el mismo contenido, y la firma cambia cuando
        el motor reescribe el fichero. Sin traza legible devuelve
        `FIRMA_SIN_TRAZA`, que tambien es estable.
        """
        self._asegurar()
        return self._hash or FIRMA_SIN_TRAZA

    def obtener(self, file_id: str) -> dict | None:
        self._asegurar()
        posicion = self._indice.get(file_id)
        return self._registros[posicion] if posicion is not None else None

    def contiene(self, file_id: str) -> bool:
        self._asegurar()
        return file_id in self._indice

    def buscar(
        self,
        *,
        resultado: str | None = None,
        lote: int | None = None,
        proveedor: str | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
        max_query_len: int = 64,
    ) -> tuple[list[dict], int]:
        """Listado filtrado y paginado. Devuelve (filas, total_de_coincidencias)."""
        self._asegurar()
        patron_q = compilar_busqueda(q, max_query_len)
        patron_proveedor = compilar_busqueda(proveedor, max_query_len)

        coincidencias = []
        for fila in self._resumenes:
            if resultado is not None and fila["resultado"] != resultado:
                continue
            if lote is not None and fila["lote"] != lote:
                continue
            if patron_proveedor is not None and not patron_proveedor.search(fila.get("proveedor") or ""):
                continue
            if patron_q is not None and not _coincide(fila, patron_q):
                continue
            coincidencias.append(fila)

        coincidencias.sort(key=lambda fila: (fila.get("file_id") or ""))
        return coincidencias[offset : offset + limit], len(coincidencias)

    def estadisticas(self) -> dict:
        self._asegurar()
        por_resultado = {nombre: 0 for nombre in RESULTADOS}
        por_lote: dict[str, int] = {}
        por_metodo: dict[str, int] = {}
        otros = 0
        for fila in self._resumenes:
            resultado = fila.get("resultado")
            if resultado in por_resultado:
                por_resultado[resultado] += 1
            else:
                otros += 1
            lote = fila.get("lote")
            clave = str(lote) if lote is not None else "desconocido"
            por_lote[clave] = por_lote.get(clave, 0) + 1
            metodo = fila.get("metodo_lectura") or "desconocido"
            por_metodo[metodo] = por_metodo.get(metodo, 0) + 1
        return {
            "total": len(self._resumenes),
            "por_resultado": por_resultado,
            "resultados_desconocidos": otros,
            "por_lote": dict(sorted(por_lote.items())),
            "por_metodo_lectura": dict(sorted(por_metodo.items())),
        }

    def file_ids(self) -> list[str]:
        self._asegurar()
        return [fila["file_id"] for fila in self._resumenes if fila.get("file_id")]

    def file_ids_por_resultado(self, resultado: str) -> list[str]:
        self._asegurar()
        return [fila["file_id"] for fila in self._resumenes if fila.get("resultado") == resultado]

    def versiones_norma(self) -> dict[str, int]:
        self._asegurar()
        versiones: dict[str, int] = {}
        for fila in self._resumenes:
            clave = fila.get("version_norma") or "desconocida"
            versiones[clave] = versiones.get(clave, 0) + 1
        return dict(sorted(versiones.items()))


def _coincide(fila: dict, patron: re.Pattern[str]) -> bool:
    return any(patron.search(str(fila.get(campo) or "")) for campo in CAMPOS_BUSQUEDA)


def compilar_busqueda(texto: str | None, max_len: int) -> re.Pattern[str] | None:
    """Compila una busqueda literal y segura.

    `re.escape` neutraliza metacaracteres (nada de ReDoS por `(a+)+` ni de
    inyeccion de patrones) y el texto se recorta a `max_len` caracteres para
    acotar el coste. La longitud excesiva se rechaza antes de llegar aqui.
    """
    if texto is None:
        return None
    limpio = texto.strip()
    if not limpio:
        return None
    return re.compile(re.escape(limpio[:max_len]), re.IGNORECASE)


class EntregaStore:
    """`outcomes.jsonl` (la entrega): solo se usa para contrastar totales."""

    def __init__(self, ruta: Path) -> None:
        self.ruta = ruta
        self._lock = threading.Lock()
        self._firma: _Firma | None = None
        self._resultados: dict[str, str] = {}
        self._invalidas = 0

    def cargar(self) -> None:
        firma = _Firma.de(self.ruta)
        with self._lock:
            if firma == self._firma:
                return
            resultados: dict[str, str] = {}
            invalidas = 0
            if firma is not None:
                with self.ruta.open("r", encoding="utf-8") as fichero:
                    for linea in fichero:
                        linea = linea.strip()
                        if not linea:
                            continue
                        try:
                            dato = json.loads(linea)
                        except json.JSONDecodeError:
                            invalidas += 1
                            continue
                        file_id = _texto(dato.get("file_id"))
                        if file_id:
                            resultados[file_id] = str(dato.get("result"))
                        else:
                            invalidas += 1
            self._firma = firma
            self._resultados = resultados
            self._invalidas = invalidas

    def resultados(self) -> dict[str, str]:
        self.cargar()
        return dict(self._resultados)

    def total(self) -> int:
        self.cargar()
        return len(self._resultados)

    def lineas_invalidas(self) -> int:
        self.cargar()
        return self._invalidas


def resumir_muchos(registros: Iterable[dict]) -> list[dict]:
    return [resumir(registro) for registro in registros]
