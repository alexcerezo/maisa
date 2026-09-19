"""Obtencion del texto de una factura: escalera de cuatro peldanos.

La escalera va de lo mas barato a lo mas caro y para en cuanto el texto es
creible:

1. **Capa de texto del PDF** (``pypdf``): exacta y gratis. Si su calidad supera
   el umbral, no se toca ni la red ni el disco.
2. **Cache versionada** por ``sha256`` del PDF, no por nombre: un fichero
   renombrado no vuelve a pagar OCR. Misma ruta de siempre
   (``CACHE_OCR/<sha256>.json``), pero con granularidad de **pagina** y con dos
   llaves de invalidacion: ``version`` del formato y ``motor`` (los modelos del
   servicio de vision). Un cambio de modelos invalida lo cacheado en vez de
   servir texto de otro motor.
3. **Vision local** (``POST {OCR_URL}/ocr?engine=local``): con timeout de
   conexion y de lectura separados y reintentos con backoff exponencial.
4. **Vision en la nube** (``POST {OCR_URL}/ocr?engine=cloud``), **apagada por
   defecto**: solo se intenta si el peldano local es poco concluyente, solo se
   adopta si mejora la calidad y esta limitada por un presupuesto por ejecucion
   (``MAISA_OCR_NUBE_MAX``).

**El lote nunca aborta.** Si no hay ningun motor de vision disponible, la
factura sale degradada (``escalon="degradado"``, ``degradado=True``) con el
texto de la capa nativa: la extraccion lo vera sin texto legible y la norma
escalara la factura en vez de pagarla. ``lee()`` y ``lee_lote()`` no propagan
nunca ``OcrNoDisponible``, y ``lee_lote()`` devuelve exactamente un
``Documento`` por ruta de entrada, en el mismo orden.

Las entradas de cache **sin** ``version`` (las ya commiteadas en el repo) y las
que no traen ``motor`` (o cuando la firma del motor no se puede consultar) se
aceptan a proposito: la entrega tiene que poder reproducirse sin red y sin
contenedor de OCR.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import requests
from pypdf import PdfReader

from .texto import Lectura, extrae

#: URL del servicio de vision. Se puede sobreescribir con ``MAISA_OCR_URL``;
#: la constante se mantiene por compatibilidad y refleja la variable al importar.
OCR_URL = os.environ.get("MAISA_OCR_URL") or "http://127.0.0.1:8866"
CACHE_OCR = Path(__file__).resolve().parents[2] / ".cache" / "ocr"

#: Version del formato del fichero de cache. Solo se aceptan esta y la legacy
#: (sin campo ``version``).
VERSION_CACHE = 2

#: Reintentos del motor local antes de degradar (``MAISA_OCR_REINTENTOS``).
REINTENTOS_POR_DEFECTO = 2
#: Timeout de lectura del OCR en segundos (``MAISA_OCR_TIMEOUT``).
TIMEOUT_POR_DEFECTO = 300.0
#: Timeout de conexion al OCR en segundos (``MAISA_OCR_CONEXION_TIMEOUT``).
CONEXION_TIMEOUT_POR_DEFECTO = 10.0
#: Llamadas maximas a la nube por ejecucion (``MAISA_OCR_NUBE_MAX``).
NUBE_MAX_POR_DEFECTO = 50

#: Backoff exponencial entre reintentos: 0.5, 1, 2, ... con tope de 8 s.
_BACKOFF_BASE = 0.5
_BACKOFF_TOPE = 8.0

# Caracteres que indican que la capa de texto no es texto real.
_RE_BASURA = re.compile(r"[\ufffd\u0000-\u0008\u000b\u000c\u000e-\u001f]")


class OcrNoDisponible(Exception):
    """El motor de vision no entrego texto (timeout, conexion, 5xx o 4xx).

    Es la excepcion del **camino interno**: la lanza la peticion HTTP y la
    recoge la escalera. ``ocr_contenedor`` devuelve ``""`` y ``lee()`` /
    ``lee_lote()`` degradan el documento; nadie fuera de este modulo deberia
    verla nunca.
    """

    def __init__(self, mensaje: str, reintentable: bool = True, reintentos: int = 0) -> None:
        super().__init__(mensaje)
        #: ``False`` cuando reintentar no puede arreglarlo (4xx, respuesta sin texto).
        self.reintentable = reintentable
        #: Reintentos ya gastados cuando se agoto la peticion.
        self.reintentos = reintentos


@dataclass
class Documento:
    """Resultado de leer un PDF: su lectura y como se obtuvo.

    ``escalon`` es ``capa_texto`` | ``cache_ocr`` | ``vision_ocr`` |
    ``vision_nube`` | ``degradado``. ``proveedor`` es ``local`` | ``nube`` |
    ``ninguno``. ``paginas_ocr`` cuenta las paginas que hubo que mandar al OCR
    (0 si la capa de texto basto) y ``reintentos`` los reintentos gastados.
    """

    lectura: Lectura
    sha256: str
    escalon: str
    cache: bool
    segundos: float
    calidad: float
    motor: str = ""
    proveedor: str = ""
    paginas_ocr: int = 0
    reintentos: int = 0
    degradado: bool = False
    error: str = ""
    nube: bool = False


@dataclass
class _EntradaCache:
    """Entrada de cache utilizable: texto ya unido y sus paginas."""

    texto: str
    paginas: list[str]
    escalon: str = "vision_ocr"


@dataclass
class _Intento:
    """Resultado de un peldano de vision: paginas leidas y reintentos gastados."""

    paginas: list[str]
    reintentos: int = 0
    proveedor: str = "ninguno"

    @property
    def texto(self) -> str:
        return "\n".join(self.paginas)


# ------------------------------------------------------------------- entorno
def _entero_env(nombre: str, defecto: int) -> int:
    try:
        return int(os.environ.get(nombre) or defecto)
    except (TypeError, ValueError):
        return defecto


def _flotante_env(nombre: str, defecto: float) -> float:
    try:
        return float(os.environ.get(nombre) or defecto)
    except (TypeError, ValueError):
        return defecto


def _bandera_env(nombre: str, defecto: bool) -> bool:
    crudo = (os.environ.get(nombre) or "").strip().lower()
    if not crudo:
        return defecto
    return crudo in ("1", "true", "si", "yes", "on")


def _ocr_url() -> str:
    """URL del servicio de vision: la variable de entorno manda sobre la constante."""
    return os.environ.get("MAISA_OCR_URL") or OCR_URL


def _espera(segundos: float) -> None:
    """Espera entre reintentos.

    Vive a nivel de modulo para que los tests puedan parchearla y no dormir de
    verdad, y para que el backoff sea inyectable desde `_intenta(dormir=...)`.
    """
    time.sleep(segundos)


def _corto(error: object, limite: int = 200) -> str:
    """Mensaje de error de una linea y corto: viaja al `Documento` y a la traza."""
    return " ".join(str(error).split())[:limite]


def _nube_activa() -> bool:
    """Peldano de nube: apagado salvo ``MAISA_OCR_NUBE=1``."""
    return _bandera_env("MAISA_OCR_NUBE", False)


# -------------------------------------------------------------------- lectura
def sha256_pdf(ruta: Path) -> str:
    return hashlib.sha256(ruta.read_bytes()).hexdigest()


def capa_texto(ruta: Path) -> tuple[str, int]:
    """Texto nativo del PDF (todas las paginas) y su numero de paginas."""
    lector = PdfReader(str(ruta))
    trozos: list[str] = []
    for pagina in lector.pages:
        try:
            trozos.append(pagina.extract_text() or "")
        except Exception:
            trozos.append("")
    return "\n".join(trozos), len(lector.pages)


def metadatos_texto(ruta: Path) -> str:
    """Metadatos del PDF como texto plano.

    Un emisor puede esconder una instruccion en ``/Keywords`` o ``/Subject``
    (hay una en La Caja que pide pagar dos veces). Se leen para inspeccionarlos,
    nunca para extraer campos.
    """
    try:
        md = PdfReader(str(ruta)).metadata or {}
    except Exception:
        return ""
    return "\n".join(str(v) for v in md.values() if isinstance(v, str))


def calidad_texto(texto: str, paginas: int) -> float:
    """0..1: cuanto se parece esto a una factura de verdad.

    Un PDF escaneado puede devolver 3 caracteres por pagina y parecer "con
    capa de texto"; aqui eso se penaliza para forzar el escalon de vision.
    """
    if not texto.strip():
        return 0.0
    por_pagina = len(texto) / max(1, paginas)
    if por_pagina < 120:
        return min(0.5, por_pagina / 240)
    util = len(re.sub(r"\s", "", texto))
    if util == 0:
        return 0.0
    ratio_basura = len(_RE_BASURA.findall(texto)) / len(texto)
    ratio_alfa = sum(c.isalnum() for c in texto) / len(texto)
    calidad = 1.0 - min(1.0, ratio_basura * 20)
    calidad *= min(1.0, ratio_alfa / 0.55)
    # Una factura tiene importes y una palabra clave; sin eso, sospechamos.
    if not re.search(r"\d{1,3}(?:[.,]\d{3})*[.,]\d{2}", texto):
        calidad *= 0.7
    if not re.search(r"(?i)factura|invoice|total", texto):
        calidad *= 0.8
    return max(0.0, min(1.0, calidad))


# ----------------------------------------------------------- firma del motor
_FIRMA_MOTOR: str | None = None
_FIRMA_LOCK = threading.Lock()


def firma_motor(timeout: float = 2.0) -> str:
    """Firma estable de los modelos del motor de vision local.

    Es lo que invalida la cache cuando cambian los modelos: si el servicio
    responde ``"local:PP-OCRv5_mobile/PP-OCRv5_mobile/PP-OCRv4_mobile"``, ese
    texto viaja al fichero de cache y cualquier otro motor lo descarta.

    Se consulta **una sola vez por proceso** (el fallo tambien se cachea: no
    tiene sentido pagar el timeout en cada factura) y devuelve ``""`` si el
    servicio no responde o no publica sus modelos.
    """
    global _FIRMA_MOTOR
    with _FIRMA_LOCK:
        if _FIRMA_MOTOR is None:
            _FIRMA_MOTOR = _consulta_firma(timeout)
        return _FIRMA_MOTOR


def _consulta_firma(timeout: float) -> str:
    try:
        respuesta = requests.get(f"{_ocr_url()}/health", timeout=timeout)
        respuesta.raise_for_status()
        datos = respuesta.json()
        if not isinstance(datos, dict):
            return ""
        motores = datos.get("engines") or {}
        local = (motores.get("local") or {}) if isinstance(motores, dict) else {}
        modelos = (local.get("models") or {}) if isinstance(local, dict) else {}
    except Exception:
        return ""
    piezas = [modelos.get(clave) for clave in ("det", "rec", "cls")]
    if not any(piezas):
        return ""
    return "local:" + "/".join(str(p) if p else "sin_cls" for p in piezas)


# ------------------------------------------------------------------ peldanos
def _peticion_ocr(ruta: Path, motor: str, timeout: float, conexion: float) -> dict:
    """Una sola llamada al servicio de vision.

    ``timeout`` es el de lectura y ``conexion`` el de conexion, como pide
    ``requests``: un servicio vivo pero atascado no debe consumir el margen de
    un servicio que no esta. Lanza ``OcrNoDisponible``; un 4xx se marca como no
    reintentable porque reintentar un documento rechazado solo gasta tiempo.
    """
    try:
        with ruta.open("rb") as fh:
            respuesta = requests.post(
                f"{_ocr_url()}/ocr",
                params={"engine": motor},
                files={"file": (ruta.name, fh, "application/pdf")},
                timeout=(conexion, timeout),
            )
    except Exception as exc:
        # Conexion, timeout, DNS... o el parche de un test: cualquiera de los
        # tres es "no hay motor de vision", no un fallo del lote.
        raise OcrNoDisponible(f"{type(exc).__name__}: {exc}") from exc
    if respuesta.status_code >= 500:
        raise OcrNoDisponible(f"el servicio devolvio {respuesta.status_code}")
    if respuesta.status_code >= 400:
        raise OcrNoDisponible(
            f"el servicio rechazo el documento ({respuesta.status_code})", reintentable=False
        )
    try:
        datos = respuesta.json()
    except Exception as exc:
        raise OcrNoDisponible(f"respuesta ilegible: {type(exc).__name__}") from exc
    if not isinstance(datos, dict):
        raise OcrNoDisponible("respuesta con forma inesperada", reintentable=False)
    return datos


def _paginas_respuesta(datos: dict) -> list[str]:
    """Paginas de texto del JSON del servicio, una entrada por pagina del PDF.

    El motor local solo publica ``text`` cuando el documento tiene una pagina;
    ``results`` viene siempre y trae el texto de cada una, que es lo que
    necesita la cache para no perder la granularidad de pagina.
    """
    resultados = datos.get("results")
    if isinstance(resultados, list):
        paginas = [(r.get("text") or "") if isinstance(r, dict) else "" for r in resultados]
        if any(p.strip() for p in paginas):
            return paginas
    texto = datos.get("text")
    if isinstance(texto, str) and texto:
        return [texto]
    return []


def _intenta(
    ruta: Path,
    motor: str = "local",
    timeout: float | None = None,
    conexion_timeout: float | None = None,
    reintentos: int | None = None,
    dormir: Callable[[float], None] | None = None,
) -> _Intento:
    """Un peldano de vision completo: peticion, reintentos con backoff y paginas.

    Lanza ``OcrNoDisponible`` (con los reintentos gastados dentro) cuando se
    agotan los intentos. El backoff es inyectable con ``dormir=`` para que los
    tests no duerman de verdad.
    """
    lee_s = _flotante_env("MAISA_OCR_TIMEOUT", TIMEOUT_POR_DEFECTO)
    con_s = _flotante_env("MAISA_OCR_CONEXION_TIMEOUT", CONEXION_TIMEOUT_POR_DEFECTO)
    tope = _entero_env("MAISA_OCR_REINTENTOS", REINTENTOS_POR_DEFECTO)
    lee_s = lee_s if timeout is None else float(timeout)
    con_s = con_s if conexion_timeout is None else float(conexion_timeout)
    tope = tope if reintentos is None else int(reintentos)
    espera = _espera if dormir is None else dormir

    error = "sin respuesta del servicio de vision"
    reintentable = True
    usados = 0
    for intento in range(max(0, tope) + 1):
        if intento:
            usados = intento
            espera(min(_BACKOFF_BASE * 2 ** (intento - 1), _BACKOFF_TOPE))
        try:
            datos = _peticion_ocr(ruta, motor, lee_s, con_s)
        except OcrNoDisponible as exc:
            error, reintentable = str(exc), exc.reintentable
        else:
            paginas = _paginas_respuesta(datos)
            if paginas:
                return _Intento(paginas=paginas, reintentos=usados, proveedor=motor)
            # Un 200 sin texto no mejora reintentando: el documento no da mas.
            error, reintentable = "el motor de vision no reconocio texto", False
        if not reintentable:
            break
    raise OcrNoDisponible(error, reintentable=reintentable, reintentos=usados)


def ocr_contenedor(
    ruta: Path,
    timeout: float | None = None,
    motor: str = "local",
    reintentos: int | None = None,
    dormir: Callable[[float], None] | None = None,
) -> str:
    """Escalon de vision: manda el PDF al servicio y devuelve el texto reconocido.

    Devuelve ``""`` (nunca lanza) cuando se agotan los reintentos: quien decide
    que hacer sin texto es la escalera, no el cliente HTTP.
    """
    try:
        return _intenta(
            ruta, motor=motor, timeout=timeout, reintentos=reintentos, dormir=dormir
        ).texto
    except OcrNoDisponible:
        return ""


# ---------------------------------------------------------------------- cache
def _lee_cache(ruta_cache: Path, sha: str, firma: str) -> _EntradaCache | None:
    """Entrada de cache si sigue valiendo; ``None`` para releer.

    Reglas de validez, permisivas a proposito con lo ya commiteado:

    - ``version`` ausente (entradas legacy) o igual a ``VERSION_CACHE``: vale.
      Cualquier otro numero se ignora y se relee.
    - ``motor`` ausente o vacio (legacy), o firma actual no disponible: vale.
      Si los dos existen y no coinciden, la entrada es de otro motor y se
      descarta.
    - el ``sha256`` tiene que ser el del PDF.
    - el texto se reconstruye uniendo ``paginas`` con ``"\\n"``; si la entrada
      es legacy, el unico texto disponible es su unico campo ``texto``.
    """
    try:
        datos = json.loads(ruta_cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(datos, dict) or datos.get("sha256") != sha:
        return None

    version = datos.get("version")
    if isinstance(version, str) and version.strip().isdigit():
        version = int(version.strip())
    if version is not None and version != VERSION_CACHE:
        return None

    motor = datos.get("motor") or ""
    if motor and firma and motor != firma:
        return None

    escalon = datos.get("escalon") or "vision_ocr"
    paginas = datos.get("paginas")
    if isinstance(paginas, list) and paginas:
        limpias = [p if isinstance(p, str) else "" for p in paginas]
        return _EntradaCache("\n".join(limpias), limpias, escalon)
    texto = datos.get("texto")
    if isinstance(texto, str) and texto:
        return _EntradaCache(texto, [texto], escalon)
    return None


def _escribe_cache(
    ruta_cache: Path, sha: str, paginas: list[str], escalon: str, firma: str
) -> None:
    """Escribe la entrada versionada. Un fallo de disco no aborta la lectura."""
    datos = {
        "version": VERSION_CACHE,
        "sha256": sha,
        "motor": firma,
        "escalon": escalon,
        "paginas": paginas,
        "texto": "\n".join(paginas),
    }
    try:
        ruta_cache.parent.mkdir(parents=True, exist_ok=True)
        ruta_cache.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


# -------------------------------------------------------- presupuesto de nube
_NUBE_USADAS = 0
_NUBE_LOCK = threading.Lock()


def reinicia_presupuesto_nube() -> None:
    """Vuelve a poner a cero el contador de llamadas a la nube de esta ejecucion."""
    global _NUBE_USADAS
    with _NUBE_LOCK:
        _NUBE_USADAS = 0


def _toma_presupuesto_nube() -> bool:
    """Reserva una llamada a la nube; ``False`` si el presupuesto esta agotado."""
    global _NUBE_USADAS
    tope = _entero_env("MAISA_OCR_NUBE_MAX", NUBE_MAX_POR_DEFECTO)
    with _NUBE_LOCK:
        if _NUBE_USADAS >= max(0, tope):
            return False
        _NUBE_USADAS += 1
        return True


# ------------------------------------------------------------------ escalera
def _documento(
    lectura: Lectura,
    sha: str,
    escalon: str,
    cache: bool,
    arranque: float,
    calidad: float,
    motor: str = "",
    proveedor: str = "ninguno",
    paginas_ocr: int = 0,
    reintentos: int = 0,
    degradado: bool = False,
    error: str = "",
    nube: bool = False,
) -> Documento:
    """Cierra un `Documento` con el tiempo ya medido y el peldano alcanzado."""
    return Documento(
        lectura=lectura, sha256=sha, escalon=escalon, cache=cache,
        segundos=time.monotonic() - arranque, calidad=calidad, motor=motor,
        proveedor=proveedor, paginas_ocr=paginas_ocr, reintentos=reintentos,
        degradado=degradado, error=error, nube=nube,
    )


def lee(
    ruta: Path,
    umbral_calidad: float = 0.6,
    usar_cache: bool = True,
    nube: bool | None = None,
) -> Documento:
    """Lee una factura aplicando la escalera completa, con cache y degradacion.

    ``nube`` a ``None`` significa "lo que diga ``MAISA_OCR_NUBE``". Nunca lanza
    ``OcrNoDisponible``: sin motor de vision devuelve el documento degradado.
    """
    arranque = time.monotonic()
    sha = sha256_pdf(ruta)
    ruta_cache = CACHE_OCR / f"{sha}.json"

    meta = metadatos_texto(ruta)
    texto, paginas = capa_texto(ruta)
    calidad = calidad_texto(texto, paginas)
    if calidad >= umbral_calidad:
        return _documento(
            extrae(texto, ruta.name, paginas, "texto_determinista", meta),
            sha, "capa_texto", False, arranque, calidad,
        )

    # La firma se consulta aqui (una vez por proceso): es la llave que decide
    # si lo cacheado vale. Si no se puede obtener, la cache legacy sigue valiendo.
    firma = firma_motor() if usar_cache else ""
    if usar_cache:
        entrada = _lee_cache(ruta_cache, sha, firma)
        if entrada is not None:
            de_nube = entrada.escalon == "vision_nube"
            return _documento(
                extrae(entrada.texto, ruta.name, paginas, "vision_ocr", meta),
                sha, "cache_ocr", True, arranque, calidad, motor=firma,
                proveedor="nube" if de_nube else "local",
                paginas_ocr=len(entrada.paginas), nube=de_nube,
            )

    if nube is None:
        nube = _nube_activa()

    try:
        elegido = _intenta(ruta, "local")
    except OcrNoDisponible as exc:
        elegido = _Intento(paginas=[], reintentos=exc.reintentos)
        error = _corto(exc)
    else:
        error = ""

    lectura = extrae(elegido.texto, ruta.name, paginas, "vision_ocr", meta)
    calidad_ocr = calidad_texto(elegido.texto, paginas)
    escalon = "vision_ocr" if elegido.paginas else "degradado"
    proveedor = elegido.proveedor if elegido.paginas else "ninguno"

    # Peldano 4: solo si el local es poco concluyente y hay presupuesto. Nunca
    # se pierde lo local: la nube tiene que MEJORAR la calidad para sustituirlo.
    poco_concluyente = calidad_ocr < umbral_calidad or lectura.texto_ilegible
    if nube and poco_concluyente:
        if not _toma_presupuesto_nube():
            error = "presupuesto de nube agotado (MAISA_OCR_NUBE_MAX)"
        else:
            try:
                intento_nube = _intenta(ruta, "cloud")
            except OcrNoDisponible as exc:
                error = _corto(f"la nube no respondio: {exc}")
            else:
                calidad_nube = calidad_texto(intento_nube.texto, paginas)
                if calidad_nube > calidad_ocr:
                    elegido = intento_nube
                    lectura = extrae(intento_nube.texto, ruta.name, paginas, "vision_ocr", meta)
                    calidad_ocr = calidad_nube
                    escalon, proveedor, error = "vision_nube", "nube", ""
                else:
                    error = "la nube no mejoro la lectura local"

    if not elegido.paginas:
        return _documento(
            extrae("", ruta.name, paginas, "vision_ocr", meta),
            sha, "degradado", False, arranque, calidad, motor=firma,
            paginas_ocr=paginas, reintentos=elegido.reintentos, degradado=True,
            error=error,
        )

    if usar_cache:
        _escribe_cache(ruta_cache, sha, elegido.paginas, escalon, firma)
    return _documento(
        lectura, sha, escalon, False, arranque, max(calidad, calidad_ocr),
        motor=firma, proveedor=proveedor, paginas_ocr=paginas,
        reintentos=elegido.reintentos, error=error, nube=escalon == "vision_nube",
    )


def _lee_o_degrada(ruta: Path, umbral_calidad: float) -> Documento:
    """`lee` con red de seguridad: ni un fallo inesperado tumba el lote."""
    try:
        return lee(ruta, umbral_calidad)
    except Exception as exc:
        return Documento(
            lectura=extrae("", ruta.name, 0, "vision_ocr", ""),
            sha256="", escalon="degradado", cache=False, segundos=0.0, calidad=0.0,
            proveedor="ninguno", degradado=True, error=_corto(exc),
        )


def lee_lote(
    rutas: list[Path], trabajadores: int = 3, umbral_calidad: float = 0.6
) -> list[Documento]:
    """Lee un lote en paralelo conservando el orden y sin abortar nunca.

    Devuelve exactamente un `Documento` por ruta, en el orden de entrada: una
    factura que reviente sale degradada en su sitio, porque la entrega son las
    500 decisiones y no 499 mas una excepcion.
    """
    if not rutas:
        return []
    with ThreadPoolExecutor(max_workers=trabajadores) as pool:
        return list(pool.map(lambda r: _lee_o_degrada(r, umbral_calidad), rutas))
