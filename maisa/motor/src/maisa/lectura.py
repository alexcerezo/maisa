"""Obtencion del texto de una factura: escalera de cuatro peldanos.

La escalera va de lo mas barato a lo mas caro y para en cuanto el texto es
creible:

1. **Capa de texto del PDF** (``pypdf``): exacta y gratis. Si su calidad supera
   el umbral, no se toca ni la red ni el disco.
2. **Cache versionada** por ``sha256`` del PDF, no por nombre: un fichero
   renombrado no vuelve a pagar OCR. Misma ruta de siempre
   (``CACHE_OCR/<sha256>.json``), pero con granularidad de **pagina** y con dos
   llaves de invalidacion: ``version`` del formato y ``motor`` (**el motor que
   produjo el texto**, local o nube, con sus modelos). Un cambio de modelos
   invalida lo cacheado en vez de servir texto de otro motor. Las entradas de
   nube se validan contra la firma de la nube y las locales contra la local:
   son motores independientes y no deben invalidarse entre si.

   Desde la ``version`` 3 la entrada guarda ademas ``geo``: la **caja** de cada
   linea reconocida y el tamano de la pagina que se renderizo. Es lo que permite
   resaltar el dato dentro del PDF en el visor. El texto sigue siendo lo unico
   obligatorio: una entrada sin ``geo`` (o sin cajas) se acepta y se lee igual,
   solo que esa factura no se podra resaltar.
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

import collections
import hashlib
import hmac
import json
import multiprocessing
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import requests
from pypdf import PdfReader

from .texto import Lectura, extrae

#: URL del servicio de vision. Se puede sobreescribir con ``MAISA_OCR_URL``;
#: la constante se mantiene por compatibilidad y refleja la variable al importar.
OCR_URL = os.environ.get("MAISA_OCR_URL") or "http://127.0.0.1:8866"
CACHE_OCR = Path(__file__).resolve().parents[2] / ".cache" / "ocr"

#: Version del formato del fichero de cache. Solo se aceptan esta y la legacy
#: (sin campo ``version``). La 3 anade ``geo`` (cajas de las lineas) y es
#: **opcional** dentro de la entrada: sin ella el texto se lee igual.
VERSION_CACHE = 3

#: Prefijo del ``motor`` de una entrada leida por la nube. El campo ``motor``
#: identifica **quien produjo el texto**, no quien lo leeria hoy: una entrada
#: local lleva la firma de los modelos locales y una de nube, la del modelo de
#: nube. Se distinguen por el prefijo porque invalidan por cosas distintas.
PREFIJO_NUBE = "nube:"

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
    """Entrada de cache utilizable: texto ya unido y sus paginas.

    ``proveedor`` y ``motor`` vienen del fichero, no de la ejecucion actual: es
    lo unico que permite decir despues con que motor se leyo de verdad. En las
    entradas legacy (sin ``proveedor``) se deduce del prefijo ``nube:`` de
    ``motor``; si tampoco hay, se asume local, que es lo que eran todas las
    anteriores a la nube.

    ``geo`` es la lista de paginas con sus cajas (``version`` 3). Vacia en las
    entradas anteriores: son validas, pero no se pueden resaltar.
    """

    texto: str
    paginas: list[str]
    escalon: str = "vision_ocr"
    proveedor: str = "local"
    motor: str = ""
    geo: list[dict] = field(default_factory=list)

    @property
    def nube(self) -> bool:
        return self.proveedor == "nube"


@dataclass
class _Intento:
    """Resultado de un peldano de vision: paginas leidas y reintentos gastados."""

    paginas: list[str]
    reintentos: int = 0
    proveedor: str = "ninguno"
    geo: list[dict] = field(default_factory=list)

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
_FIRMA_NUBE: str | None = None
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
    _asegura_firmas(timeout)
    return _FIRMA_MOTOR or ""


def firma_nube(timeout: float = 2.0) -> str:
    """Firma del modelo de nube (``"nube:PaddleOCR-VL-1.6"``), o ``""``.

    Una entrada de cache leida por la nube se invalida cuando cambia **este**
    modelo, no cuando cambian los locales: son motores independientes.
    """
    _asegura_firmas(timeout)
    return _FIRMA_NUBE or ""


def _asegura_firmas(timeout: float) -> None:
    """Rellena las dos firmas de una sola consulta a ``/health``."""
    global _FIRMA_MOTOR, _FIRMA_NUBE
    with _FIRMA_LOCK:
        if _FIRMA_MOTOR is None:
            _FIRMA_MOTOR, _FIRMA_NUBE = _consulta_firmas(timeout)


def _consulta_firmas(timeout: float) -> tuple[str, str]:
    """Firma de los modelos local y de nube, tal como los publica ``/health``.

    Devuelve ``("", "")`` si el servicio no responde: sin firmas la cache sigue
    valiendo (es lo que permite reproducir la entrega sin contenedor de OCR).
    """
    try:
        respuesta = requests.get(f"{_ocr_url()}/health", timeout=timeout)
        respuesta.raise_for_status()
        datos = respuesta.json()
        if not isinstance(datos, dict):
            return "", ""
        motores = datos.get("engines") or {}
        if not isinstance(motores, dict):
            return "", ""
        local = motores.get("local") or {}
        nube = motores.get("cloud") or {}
        modelos = (local.get("models") or {}) if isinstance(local, dict) else {}
        modelo_nube = nube.get("model") if isinstance(nube, dict) else None
    except Exception:
        return "", ""
    piezas = [modelos.get(clave) for clave in ("det", "rec", "cls")]
    firma_local = ""
    if any(piezas):
        firma_local = "local:" + "/".join(str(p) if p else "sin_cls" for p in piezas)
    firma_nube = f"{PREFIJO_NUBE}{modelo_nube}" if modelo_nube else ""
    return firma_local, firma_nube


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


def _caja_envolvente(box: object) -> list[float] | None:
    """Poligono de 4 puntos del OCR -> ``[x0, y0, x1, y1]``.

    El servicio devuelve la caja como cuatro esquinas; para pintar un resaltado
    basta su envolvente, que ademas ocupa la mitad en disco.
    """
    if not isinstance(box, list) or not box:
        return None
    xs: list[float] = []
    ys: list[float] = []
    for punto in box:
        if not isinstance(punto, (list, tuple)) or len(punto) < 2:
            return None
        try:
            xs.append(float(punto[0]))
            ys.append(float(punto[1]))
        except (TypeError, ValueError):
            return None
    if len(xs) != 4:
        return None
    return [min(xs), min(ys), max(xs), max(ys)]


def _paginas_geo(datos: dict) -> list[dict]:
    """Cajas de las lineas reconocidas, por pagina, con el tamano del render.

    Las coordenadas del OCR estan en **pixeles del bitmap** que se renderizo, no
    en puntos del PDF: sin ``escala`` no se pueden volver a poner sobre la
    pagina. Por eso se guardan juntos y por eso ``escala`` es obligatoria para
    que la entrada sirva: una caja sin su escala es una caja inutil.

    Devuelve ``[]`` cuando el servicio no mando cajas (``include_boxes=false``) o
    el documento no trae lineas: la factura se lee igual, solo que sin resaltado.
    """
    resultados = datos.get("results")
    if not isinstance(resultados, list):
        return []
    paginas: list[dict] = []
    for indice, pagina in enumerate(resultados):
        if not isinstance(pagina, dict):
            continue
        escala = pagina.get("scale")
        if not isinstance(escala, (int, float)) or float(escala) <= 0:
            continue
        tamano = pagina.get("size") if isinstance(pagina.get("size"), dict) else {}
        ancho, alto = tamano.get("width"), tamano.get("height")
        lineas: list[dict] = []
        for linea in pagina.get("lines") or []:
            if not isinstance(linea, dict):
                continue
            texto = linea.get("text")
            caja = _caja_envolvente(linea.get("box"))
            if not isinstance(texto, str) or not texto.strip() or caja is None:
                continue
            fila: dict = {"texto": texto, "caja": caja}
            score = linea.get("score")
            if isinstance(score, (int, float)):
                fila["score"] = round(float(score), 6)
            lineas.append(fila)
        if not lineas:
            continue
        pagina_geo: dict = {"pagina": indice, "escala": float(escala), "lineas": lineas}
        if isinstance(ancho, (int, float)) and isinstance(alto, (int, float)):
            pagina_geo["ancho"] = float(ancho)
            pagina_geo["alto"] = float(alto)
        paginas.append(pagina_geo)
    return paginas


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
                return _Intento(
                    paginas=paginas, reintentos=usados, proveedor=motor, geo=_paginas_geo(datos)
                )
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
#: Variable con la clave de firma de la cache. Fuera de `.cache/` a proposito:
#: una clave guardada junto a lo que firma no defiende de quien puede escribir
#: en el directorio, que es justo el atacante del que nos protegemos.
CLAVE_CACHE_ENV = "MAISA_CACHE_CLAVE"
_CLAVE_CACHE: bytes | None = None
_CLAVE_CACHE_VISTA = False
#: Contadores de la cache en esta ejecucion: lo que se leyo, lo que se rechazo.
CACHE_CONTADORES: collections.Counter = collections.Counter()


def _clave_de_fichero_env() -> str:
    """``MAISA_CACHE_CLAVE`` del `.env` del repo, si existe.

    El motor no lo lee por su cuenta (lo arranca el operador, no el compose), pero
    `maisa/.env` es el unico sitio del repo donde viven los secretos, asi que se
    acepta como origen de la clave para que la firma funcione sin exportar nada a
    mano. La variable de entorno siempre gana sobre el fichero.
    """
    ruta = Path(__file__).resolve().parents[3] / ".env"
    try:
        for linea in ruta.read_text(encoding="utf-8").splitlines():
            linea = linea.strip()
            if linea.startswith("#") or "=" not in linea:
                continue
            nombre, _, valor = linea.partition("=")
            if nombre.strip() == CLAVE_CACHE_ENV:
                return valor.strip().strip("'\"")
    except OSError:
        pass
    return ""


def _clave_cache() -> bytes | None:
    """Clave de firma, o ``None`` si no hay ninguna configurada.

    Se consulta una vez por proceso. Sin clave no se firma y no se puede
    verificar: la cache funciona igual que antes, pero `estado_cache()` lo
    declara en vez de callarlo.

    La variable de entorno definida **y vacia** significa "firma apagada a
    proposito": no se cae al `.env`. Es lo que usan los tests (que no pueden
    depender de que el repo tenga clave) y lo que querra un despliegue que
    prefiera no firmar antes que firmar con un secreto que no controla.
    """
    global _CLAVE_CACHE, _CLAVE_CACHE_VISTA
    if not _CLAVE_CACHE_VISTA:
        if CLAVE_CACHE_ENV in os.environ:
            crudo = os.environ[CLAVE_CACHE_ENV].strip()
        else:
            crudo = _clave_de_fichero_env()
        _CLAVE_CACHE = crudo.encode("utf-8") if crudo else None
        _CLAVE_CACHE_VISTA = True
    return _CLAVE_CACHE


def reinicia_clave_cache() -> None:
    """Vuelve a mirar el entorno en busca de la clave de firma.

    La clave se lee una sola vez por proceso (es un secreto, no cambia a media
    ejecucion); esto existe para que los tests puedan cambiarla entre casos.
    """
    global _CLAVE_CACHE_VISTA
    _CLAVE_CACHE_VISTA = False


def firma_entrada(datos: dict) -> str:
    """HMAC-SHA256 de la entrada de cache, sin contar el propio campo ``hmac``.

    El JSON se canonicaliza (claves ordenadas, sin espacios) para que el sello no
    dependa del orden en que se escribio el fichero ni de quien lo reescriba.
    """
    clave = _clave_cache()
    if clave is None:
        return ""
    cuerpo = {k: v for k, v in datos.items() if k != "hmac"}
    serializado = json.dumps(
        cuerpo, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hmac.new(clave, serializado, hashlib.sha256).hexdigest()


def verifica_cache(directorio: Path | None = None) -> dict:
    """Audita el directorio de cache: cuantas entradas van firmadas y cuantas no.

    Devuelve ``{"total", "firmadas_ok", "manipuladas", "sin_firma", "ilegibles",
    "sin_clave"}``. ``manipuladas`` son entradas con sello que **no** cuadra: eso
    es exactamente lo que el sello existe para detectar. ``sin_clave`` cuenta las
    firmadas que no se han podido verificar por no haber clave en el entorno.
    """
    raiz = directorio or CACHE_OCR
    informe = collections.Counter(total=0, firmadas_ok=0, manipuladas=0,
                                  sin_firma=0, ilegibles=0, sin_clave=0)
    if not raiz.is_dir():
        return dict(informe)
    for ruta in sorted(raiz.glob("*.json")):
        informe["total"] += 1
        try:
            datos = json.loads(ruta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            informe["ilegibles"] += 1
            continue
        if not isinstance(datos, dict):
            informe["ilegibles"] += 1
            continue
        sello = datos.get("hmac")
        if not isinstance(sello, str) or not sello:
            informe["sin_firma"] += 1
            continue
        esperado = firma_entrada(datos)
        if not esperado:
            informe["sin_clave"] += 1
        elif hmac.compare_digest(esperado, sello):
            informe["firmadas_ok"] += 1
        else:
            informe["manipuladas"] += 1
    return dict(informe)


def estado_cache() -> dict:
    """Como va la cache en esta ejecucion (contadores de lectura, no del disco)."""
    return {
        "clave_configurada": _clave_cache() is not None,
        "variable": CLAVE_CACHE_ENV,
        **{k: int(v) for k, v in CACHE_CONTADORES.items()},
    }


def _lee_cache(ruta_cache: Path, sha: str, firma: str, firma_nube: str = "") -> _EntradaCache | None:
    """Entrada de cache si sigue valiendo; ``None`` para releer.

    Reglas de validez, permisivas a proposito con lo ya commiteado:

    - ``version`` ausente (entradas legacy) o igual a ``VERSION_CACHE``: vale.
      Cualquier otro numero se ignora y se relee.
    - ``motor`` ausente o vacio (legacy), o firma actual no disponible: vale.
      Si los dos existen y no coinciden, la entrada es de otro motor y se
      descarta.
    - una entrada de **nube** (``proveedor`` a ``"nube"``, o ``motor`` con
      prefijo ``nube:``) se compara con la firma de la nube, no con la local:
      que cambien los modelos locales no invalida un texto que no salio de
      ellos, y al reves tampoco.
    - el ``sha256`` tiene que ser el del PDF.
    - el texto se reconstruye uniendo ``paginas`` con ``"\\n"``; si la entrada
      es legacy, el unico texto disponible es su unico campo ``texto``.
    """
    try:
        datos = json.loads(ruta_cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        CACHE_CONTADORES["ilegibles"] += 1
        return None
    if not isinstance(datos, dict) or datos.get("sha256") != sha:
        return None

    sello = datos.get("hmac")
    if isinstance(sello, str) and sello:
        esperado = firma_entrada(datos)
        if not esperado:
            # Hay sello pero no hay clave: la entrada es utilizable, pero no se
            # puede afirmar que sea la que escribio el OCR. Se cuenta para que
            # `estado_cache()` pueda decirlo.
            CACHE_CONTADORES["firmadas_sin_clave"] += 1
        elif not hmac.compare_digest(esperado, sello):
            # Contenido cambiado despues de escribirse: se relee en vez de servir
            # texto que nadie ha reconocido.
            CACHE_CONTADORES["manipuladas"] += 1
            return None
        else:
            CACHE_CONTADORES["firmadas_ok"] += 1
    else:
        CACHE_CONTADORES["sin_firma"] += 1

    version = datos.get("version")
    if isinstance(version, str) and version.strip().isdigit():
        version = int(version.strip())
    if version is not None and version != VERSION_CACHE:
        return None

    motor = datos.get("motor") or ""
    # ``proveedor`` manda cuando esta: es el campo explicito que anaden el motor
    # y el precalentado desde el arreglo. El prefijo ``nube:`` queda como
    # respaldo para entradas escritas entre medias.
    de_nube = datos.get("proveedor") == "nube" or motor.startswith(PREFIJO_NUBE)
    firma_esperada = firma_nube if de_nube else firma
    if motor and firma_esperada and motor != firma_esperada:
        return None

    escalon = datos.get("escalon") or ("vision_nube" if de_nube else "vision_ocr")
    proveedor = "nube" if de_nube else "local"
    geo = _geo_cache(datos.get("geo"))
    paginas = datos.get("paginas")
    if isinstance(paginas, list) and paginas:
        limpias = [p if isinstance(p, str) else "" for p in paginas]
        return _EntradaCache("\n".join(limpias), limpias, escalon, proveedor, motor, geo)
    texto = datos.get("texto")
    if isinstance(texto, str) and texto:
        return _EntradaCache(texto, [texto], escalon, proveedor, motor, geo)
    return None


def _geo_cache(crudo: object) -> list[dict]:
    """``geo`` del fichero, saneado.

    Una entrada con ``geo`` corrupta no puede tumbar la lectura: se descarta la
    geometria y se queda el texto, que es lo que de verdad hace falta para
    decidir. Perder el resaltado es barato; perder la factura, no.
    """
    if not isinstance(crudo, list):
        return []
    paginas: list[dict] = []
    for pagina in crudo:
        if not isinstance(pagina, dict):
            continue
        escala = pagina.get("escala")
        indice = pagina.get("pagina")
        if not isinstance(escala, (int, float)) or float(escala) <= 0:
            continue
        if not isinstance(indice, int) or indice < 0:
            continue
        lineas = [
            {"texto": linea["texto"], "caja": [float(v) for v in linea["caja"]]}
            for linea in (pagina.get("lineas") or [])
            if isinstance(linea, dict)
            and isinstance(linea.get("texto"), str)
            and isinstance(linea.get("caja"), list)
            and len(linea["caja"]) == 4
        ]
        if not lineas:
            continue
        saneada: dict = {"pagina": indice, "escala": float(escala), "lineas": lineas}
        for clave in ("ancho", "alto"):
            valor = pagina.get(clave)
            if isinstance(valor, (int, float)):
                saneada[clave] = float(valor)
        paginas.append(saneada)
    return paginas


def _escribe_cache(
    ruta_cache: Path,
    sha: str,
    paginas: list[str],
    escalon: str,
    firma: str,
    firma_nube: str = "",
    geo: list[dict] | None = None,
) -> None:
    """Escribe la entrada versionada y firmada. Un fallo de disco no aborta la lectura.

    ``motor`` es **el motor que produjo el texto**, no el local. Antes se
    estampaba siempre la firma local, asi que una lectura de la nube quedaba
    etiquetada como local: se invalidaba (o no) por el modelo equivocado y el
    corpus parecia leido por un solo motor cuando lo habian leido dos.

    ``geo`` solo se escribe cuando hay cajas: una clave vacia en 500 ficheros es
    ruido en el diff y no aporta nada.

    ``hmac`` sella el contenido entero. Sin clave configurada la entrada se
    escribe **sin sello** (es lo que eran todas las anteriores); con clave, una
    entrada manipulada se detecta al leerla y se vuelve a pedir al OCR en vez de
    servir texto cambiado.
    """
    de_nube = escalon == "vision_nube"
    datos = {
        "version": VERSION_CACHE,
        "sha256": sha,
        "motor": firma_nube if de_nube else firma,
        "proveedor": "nube" if de_nube else "local",
        "escalon": escalon,
        "paginas": paginas,
        "texto": "\n".join(paginas),
    }
    if geo:
        datos["geo"] = geo
    sello = firma_entrada(datos)
    if sello:
        datos["hmac"] = sello
    try:
        ruta_cache.parent.mkdir(parents=True, exist_ok=True)
        ruta_cache.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


# -------------------------------------------------------- presupuesto de nube
_NUBE_USADAS = 0
_NUBE_LOCK = threading.Lock()
#: Contador compartido entre procesos (``multiprocessing.Value``) cuando el lote
#: se lee con procesos. Sin el, cada proceso tendria su propio presupuesto y el
#: tope de nube se multiplicaria por el numero de trabajadores.
_NUBE_COMPARTIDO = None
#: Protege la creacion/destruccion del contador compartido (no su valor).
_NUBE_COMPARTIDO_LOCK = threading.Lock()


def reinicia_presupuesto_nube() -> None:
    """Vuelve a poner a cero el contador de llamadas a la nube de esta ejecucion."""
    global _NUBE_USADAS
    with _NUBE_LOCK:
        _NUBE_USADAS = 0
    if _NUBE_COMPARTIDO is not None:
        with _NUBE_COMPARTIDO.get_lock():
            _NUBE_COMPARTIDO.value = 0


def _toma_presupuesto_nube() -> bool:
    """Reserva una llamada a la nube; ``False`` si el presupuesto esta agotado.

    El presupuesto es **por ejecucion**, no por trabajador: con procesos el
    contador tiene que ser el mismo objeto en todos ellos o el tope de
    ``MAISA_OCR_NUBE_MAX`` se multiplicaria por el numero de hijos.
    """
    global _NUBE_USADAS
    tope = _entero_env("MAISA_OCR_NUBE_MAX", NUBE_MAX_POR_DEFECTO)
    if _NUBE_COMPARTIDO is not None:
        with _NUBE_COMPARTIDO.get_lock():
            if _NUBE_COMPARTIDO.value >= max(0, tope):
                return False
            _NUBE_COMPARTIDO.value += 1
            return True
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
    firma_n = firma_nube() if usar_cache else ""
    if usar_cache:
        entrada = _lee_cache(ruta_cache, sha, firma, firma_n)
        if entrada is not None:
            return _documento(
                extrae(entrada.texto, ruta.name, paginas, "vision_ocr", meta),
                sha, "cache_ocr", True, arranque, calidad,
                motor=entrada.motor or firma,
                proveedor=entrada.proveedor,
                paginas_ocr=len(entrada.paginas), nube=entrada.nube,
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
        _escribe_cache(ruta_cache, sha, elegido.paginas, escalon, firma, firma_n, elegido.geo)
    return _documento(
        lectura, sha, escalon, False, arranque, max(calidad, calidad_ocr),
        motor=firma_n if escalon == "vision_nube" else firma,
        proveedor=proveedor, paginas_ocr=paginas,
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
    rutas: list[Path],
    trabajadores: int = 3,
    umbral_calidad: float = 0.6,
    modo: str | None = None,
) -> list[Documento]:
    """Lee un lote en paralelo conservando el orden y sin abortar nunca.

    Devuelve exactamente un `Documento` por ruta, en el orden de entrada: una
    factura que reviente sale degradada en su sitio, porque la entrega son las
    500 decisiones y no 499 mas una excepcion.

    ``modo`` es ``"procesos"`` | ``"hilos"`` | ``None`` (lo que diga
    ``MAISA_LECTURA_MODO``, por defecto procesos en POSIX). Se admiten los dos
    porque miden cosas distintas:

    - **hilos** reparten la espera de red (el OCR es una llamada HTTP y el hilo
      se suelta), pero no reparten el trabajo de CPU: extraer la capa de texto
      de un PDF es puro Python y el GIL lo serializa. Medido: 4,76 s -> 3,69 s
      al pasar de 1 a 4 hilos, es decir ×1,29 con 4 hilos en 2 nucleos.
    - **procesos** reparten tambien la CPU, a cambio de pagar el arranque del
      interprete y de serializar cada `Documento` de vuelta al padre.

    El reparto es por `file_id` (el directorio troceado), que es lo que hace
    posible lanzar varios procesos sobre el mismo lote: el motor no tiene estado
    y la salida se concatena en el padre, en orden.

    ``lee_lote`` nunca degrada por culpa del paralelismo: si el pool de procesos
    no se puede levantar (sin ``fork``, sin permisos) cae a hilos en vez de
    fallar.
    """
    if not rutas:
        return []
    if modo is None:
        modo = os.environ.get("MAISA_LECTURA_MODO", "").strip().lower()
    if trabajadores <= 1:
        return [_lee_o_degrada(r, umbral_calidad) for r in rutas]
    if modo == "procesos" or (not modo and _puede_usar_procesos()):
        try:
            return _lee_lote_procesos(rutas, trabajadores, umbral_calidad)
        except (OSError, RuntimeError, ValueError) as exc:
            # Un pool que no arranca no puede costar una entrega: se relee con
            # hilos. El aviso va a stderr porque el numero de trabajadores es
            # una decision de capacidad y conviene enterarse de que no se aplico.
            print(f"aviso: sin procesos ({_corto(exc)}); se relee con hilos",
                  file=sys.stderr)
    with ThreadPoolExecutor(max_workers=trabajadores) as pool:
        return list(pool.map(lambda r: _lee_o_degrada(r, umbral_calidad), rutas))


def _puede_usar_procesos() -> bool:
    """Si este interprete puede trocear el lote en procesos de verdad.

    ``fork`` es lo que hace que el hijo herede el maestro ya cargado y la cache
    de firmas sin volver a pagarlos. Sin ``fork`` (Windows) un hijo arranca el
    interprete entero y vuelve a leer el Excel: sale mas caro que los hilos.
    """
    return hasattr(os, "fork") and "fork" in multiprocessing.get_all_start_methods()


def _lee_lote_procesos(
    rutas: list[Path], trabajadores: int, umbral_calidad: float
) -> list[Documento]:
    """Trocea el lote en procesos con el presupuesto de nube compartido."""
    global _NUBE_COMPARTIDO
    contexto = multiprocessing.get_context("fork")
    trozo = max(1, len(rutas) // (trabajadores * 4))
    with _NUBE_COMPARTIDO_LOCK:
        creado = _NUBE_COMPARTIDO is None
        if creado:
            _NUBE_COMPARTIDO = contexto.Value("i", _NUBE_USADAS)
    try:
        with ProcessPoolExecutor(
            max_workers=trabajadores, mp_context=contexto
        ) as pool:
            return list(pool.map(
                _lee_o_degrada_par, [(r, umbral_calidad) for r in rutas], chunksize=trozo
            ))
    finally:
        if creado:
            with _NUBE_COMPARTIDO_LOCK:
                _NUBE_COMPARTIDO = None


def _lee_o_degrada_par(par: tuple[Path, float]) -> Documento:
    """``_lee_o_degrada`` con la firma que necesita `ProcessPoolExecutor`."""
    ruta, umbral_calidad = par
    return _lee_o_degrada(ruta, umbral_calidad)
