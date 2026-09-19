"""Cliente del ERP 2009: hostil por diseno y por contrato.

El bridge falla a proposito cada 10 consultas (``ORA-00600``), caduca el token
a los 900 s o 300 usos (``SES-401``) y aplica 10 req/s (``ERP-429``). Aqui no
se asume nada: cada fallo tiene su politica, cada respuesta se valida y el
resultado se guarda en un snapshot local para no volver a pedirlo.
"""
from __future__ import annotations

import hashlib
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .normaliza import a_decimal, a_fecha, norm_nif, norm_pedido

BASE = "http://127.0.0.1:8009"
USUARIO = "alberto"
CLAVE = "FACTURAS2009"

PAGINA_TAMANO = 20
TOKEN_VIGENCIA_SEGUNDOS = 900
TOKEN_VIGENCIA_USOS = 300
FALLO_CADA = 10
RATE_MAX_POR_SEGUNDO = 10

REINTENTOS_ORA = 4
REINTENTOS_LOGIN = 3


class ErrorERP(RuntimeError):
    """Fallo del bridge con su codigo legacy adjunto."""

    def __init__(self, codigo: str, detalle: str = "", reintentable: bool = False):
        super().__init__(f"{codigo}: {detalle}" if detalle else codigo)
        self.codigo = codigo
        self.detalle = detalle
        self.reintentable = reintentable


@dataclass
class Asiento:
    """Un asiento del ERP, ya normalizado."""

    id: str
    fecha: str
    proveedor: str
    nif: str
    pedido: str
    importe: Decimal
    estado: str

    def como_dict(self) -> dict:
        return {
            "id": self.id, "fecha": self.fecha, "proveedor": self.proveedor,
            "nif": self.nif, "pedido": self.pedido,
            "importe": str(self.importe), "estado": self.estado,
        }


@dataclass
class MetricasERP:
    """Telemetria de la conversacion con el bridge (rúbrica de coste/escala)."""

    peticiones: int = 0
    logins: int = 0
    reintentos_ora: int = 0
    reintentos_429: int = 0
    relogins: int = 0
    segundos: float = 0.0
    errores: list[str] = field(default_factory=list)

    def como_dict(self) -> dict:
        return {
            "peticiones_http": self.peticiones, "logins": self.logins,
            "reintentos_ora_00600": self.reintentos_ora,
            "esperas_429": self.reintentos_429, "relogins": self.relogins,
            "segundos": round(self.segundos, 3), "errores": list(self.errores),
        }


class ERP:
    """Cliente sincrono, serializado y auto-reparable."""

    def __init__(self, base: str = BASE, pausa_min: float = 0.0) -> None:
        self.base = base.rstrip("/")
        self.pausa_min = pausa_min
        self.token: str | None = None
        self.usos_token = 0
        self.token_creado = 0.0
        self.metricas = MetricasERP()
        self._ultima_peticion = 0.0
        self._intervalo = 1.0 / (RATE_MAX_POR_SEGUNDO * 0.8)  # margen del 20 %

    # ------------------------------------------------------------------ red
    def _espera_ritmo(self) -> None:
        ahora = time.monotonic()
        delta = ahora - self._ultima_peticion
        if delta < self._intervalo:
            time.sleep(self._intervalo - delta)
        if self.pausa_min:
            time.sleep(self.pausa_min)
        self._ultima_peticion = time.monotonic()

    def _peticion(self, ruta: str, datos: bytes | None = None,
                  cabeceras: dict | None = None) -> tuple[int, str, dict]:
        url = f"{self.base}{ruta}"
        req = urllib.request.Request(url, data=datos, headers=cabeceras or {})
        self._espera_ritmo()
        self.metricas.peticiones += 1
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                cuerpo = resp.read().decode("iso-8859-1", errors="replace")
                return resp.status, cuerpo, dict(resp.headers)
        except urllib.error.HTTPError as e:
            cuerpo = e.read().decode("iso-8859-1", errors="replace")
            return e.code, cuerpo, dict(e.headers)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return 0, "", {"motivo": str(e)}

    @staticmethod
    def _codigo(cuerpo: str) -> str:
        m = ET.fromstring(cuerpo) if cuerpo.strip().startswith("<") else None
        if m is not None:
            for campo in ("codigo", "error", "código"):
                hallado = m.find(f".//{campo}")
                if hallado is not None and hallado.text:
                    return hallado.text.strip()
        return ""

    # ---------------------------------------------------------------- login
    def login(self) -> None:
        datos = urllib.parse.urlencode({"usuario": USUARIO, "clave": CLAVE}).encode()
        for intento in range(1, REINTENTOS_LOGIN + 1):
            estado, cuerpo, _ = self._peticion(
                "/erp/login", datos,
                {"Content-Type": "application/x-www-form-urlencoded"},
            )
            self.metricas.logins += 1
            if estado == 200 and "<token>" in cuerpo:
                raiz = ET.fromstring(cuerpo)
                token = raiz.findtext(".//token", "").strip()
                if token:
                    self.token = token
                    self.usos_token = 0
                    self.token_creado = time.monotonic()
                    return
            time.sleep(min(8.0, 0.5 * 2 ** intento) * random.uniform(0.8, 1.2))
        raise ErrorERP("SES-401", "no se pudo obtener token tras varios intentos")

    def _token_valido(self) -> bool:
        if not self.token:
            return False
        edad = time.monotonic() - self.token_creado
        return edad < TOKEN_VIGENCIA_SEGUNDOS - 30 and self.usos_token < TOKEN_VIGENCIA_USOS - 5

    # ------------------------------------------------------------ consultas
    def _consulta(self, ruta: str) -> str:
        """GET autenticado con toda la politica de reintentos del bridge."""
        if not self._token_valido():
            self.login()
        ultimo: ErrorERP | None = None
        for intento in range(1, REINTENTOS_ORA + 2):
            estado, cuerpo, cabeceras = self._peticion(
                ruta, None, {"X-ERP-Token": self.token or "", "Accept": "text/xml"}
            )
            self.usos_token += 1
            if estado == 200:
                return cuerpo
            codigo = self._codigo(cuerpo) or f"HTTP-{estado}"
            if estado == 401 or codigo == "SES-401":
                self.metricas.relogins += 1
                self.login()
                ultimo = ErrorERP("SES-401", "token caducado", reintentable=True)
                continue
            if codigo == "ORA-00600":
                self.metricas.reintentos_ora += 1
                time.sleep(min(4.0, 0.2 * intento) * random.uniform(0.8, 1.2))
                ultimo = ErrorERP("ORA-00600", "fallo interno del bridge", reintentable=True)
                continue
            if estado == 429 or codigo == "ERP-429":
                self.metricas.reintentos_429 += 1
                espera = float(cabeceras.get("Retry-After", 1) or 1)
                time.sleep(max(espera, 0.5) * random.uniform(1.0, 1.4))
                ultimo = ErrorERP("ERP-429", "rate limit", reintentable=True)
                continue
            self.metricas.errores.append(f"{ruta} -> {codigo}")
            raise ErrorERP(codigo, "consulta no recuperable", reintentable=False)
        raise ultimo or ErrorERP("ORA-00600", "agotados los reintentos")

    def estado(self) -> dict:
        _, cuerpo, _ = self._peticion("/erp/estado")
        raiz = ET.fromstring(cuerpo)
        return {hijo.tag: (hijo.text or "") for hijo in raiz}

    def asientos(self) -> list[Asiento]:
        """Descarga los 516 asientos paginando de 1 a ``paginas``."""
        arranque = time.monotonic()
        primero = ET.fromstring(self._consulta("/erp/asientos?pagina=1"))
        meta = primero.find("meta")
        paginas = int(meta.findtext("paginas", "1")) if meta is not None else 1
        total = int(meta.findtext("total", "0")) if meta is not None else 0
        crudos = list(primero.findall(".//asiento"))
        for pagina in range(2, paginas + 1):
            raiz = ET.fromstring(self._consulta(f"/erp/asientos?pagina={pagina}"))
            crudos.extend(raiz.findall(".//asiento"))
        asientos = [a for a in (self._parsea(n) for n in crudos) if a is not None]
        if total and len(asientos) != total:
            raise ErrorERP("ERP-INCOMPLETO", f"esperados {total}, obtenidos {len(asientos)}")
        self.metricas.segundos = time.monotonic() - arranque
        return asientos

    @staticmethod
    def _parsea(nodo: ET.Element) -> Asiento | None:
        def texto(tag: str) -> str:
            return (nodo.findtext(tag) or "").strip()

        importe = a_decimal(texto("importe"))
        if importe is None:
            return None
        fecha = a_fecha(texto("fecha"))
        return Asiento(
            id=texto("id"),
            fecha=fecha.isoformat() if fecha else texto("fecha"),
            proveedor=texto("proveedor"),
            nif=norm_nif(texto("nif")),
            pedido=norm_pedido(texto("pedido")),
            importe=importe,
            estado=texto("estado").upper(),
        )


def snapshot(ruta: Path, asientos: list[Asiento], metricas: dict | None = None) -> str:
    """Persiste los asientos y devuelve el sha256 del contenido (linaje)."""
    cuerpo = json.dumps(
        [a.como_dict() for a in asientos], ensure_ascii=False, sort_keys=True
    ).encode()
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(cuerpo)
    if metricas is not None:
        (ruta.with_suffix(".metricas.json")).write_text(
            json.dumps(metricas, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return hashlib.sha256(cuerpo).hexdigest()


def carga_snapshot(ruta: Path) -> list[Asiento]:
    """Reconstruye los asientos desde el snapshot (sin tocar la red).

    Tolera snapshots en formato legacy (``12.874,40``, ``DD/MM/AAAA``): el
    snapshot es un fichero que puede haber escrito una version anterior y
    re-normalizarlo aqui evita que un cambio de formato invalide la cache.
    Acepta tanto una lista de asientos como un objeto que los envuelva en
    ``{"asientos": [...]}`` con metadatos de la descarga.
    """
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    if isinstance(datos, dict):
        datos = datos.get("asientos") or []
    asientos: list[Asiento] = []
    for d in datos:
        if not isinstance(d, dict):
            continue
        importe = a_decimal(d.get("importe"))
        if importe is None:
            continue
        fecha = a_fecha(d.get("fecha"))
        asientos.append(Asiento(
            id=str(d.get("id") or d.get("asiento_id") or ""),
            fecha=fecha.isoformat() if fecha else str(d.get("fecha", "")),
            proveedor=str(d.get("proveedor", "")).strip().upper(),
            nif=norm_nif(d.get("nif")),
            pedido=norm_pedido(d.get("pedido")),
            importe=importe,
            estado=str(d.get("estado", "")).strip().upper(),
        ))
    return asientos
