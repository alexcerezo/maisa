"""
Cliente de la API de PaddleOCR-VL alojada en Baidu AI Studio.

Flujo de la API (todo verificado contra el servicio real, no contra la doc):

  1. POST {base}            multipart con el fichero, o JSON con `fileUrl`.
                            -> {"code":0,"msg":"Success","data":{"jobId":"..."}}
  2. GET  {base}/{jobId}    sondeo del estado.
                            -> data.state en {pending,running,done,failed}
                               data.extractProgress.{totalPages,extractedPages}
                               data.resultUrl.jsonUrl
  3. GET  jsonUrl           JSONL, una linea por documento.
                            -> result.layoutParsingResults[]  (una por pagina)
                                 .markdown.text      texto en Markdown
                                 .prunedResult.parsing_res_list[]  bloques
                                 .prunedResult.layout_det_res.boxes[]  cajas

DOS ADVERTENCIAS IMPORTANTES SOBRE LA CALIDAD, medidas sobre documentos reales:

  a) La API NO devuelve confianza de texto. El unico score disponible es el de
     DETECCION de region (`layout_det_res.boxes[].score`), que mide si ahi hay
     algo con forma de texto, no si lo leido es correcto. NO sirve para detectar
     una lectura mala: en el fax de prueba el modelo se invento una linea entera
     en chino ("晉書·齊桓公伐齊") y su region tenia un score normal.

  b) Es un modelo de lenguaje, asi que puede alucinar. El motor local (PP-OCRv5)
     no alucina nunca, pero confunde glifos degradados. En el fax degradado:
        nube  -> 460.00 + 460.00 = 920.00  (coherente con la base imponible)
        local -> 460.00 + 480.00 = 940.00  (no cuadra)
     Es decir: la nube es mejor lectora y peor testigo. Ver README.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("ocr-server.cloud")


# --------------------------------------------------------------------------- #
# Errores
# --------------------------------------------------------------------------- #


class CloudError(RuntimeError):
    """
    Fallo del motor en la nube.

    `kind` permite decidir desde fuera si merece la pena reintentar o si hay que
    caer al motor local sin perder tiempo.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "api",
        status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retryable = retryable

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "message": str(self)}
        if self.status is not None:
            out["status"] = self.status
        if self.retryable:
            out["retryable"] = True
        return out


def _classify(status: int, body: str) -> CloudError:
    snippet = (body or "").strip()[:300]
    if status in (401, 403):
        return CloudError(
            f"credenciales rechazadas por la API ({status})", kind="auth", status=status
        )
    if status == 429:
        return CloudError(
            "la API limita la frecuencia de peticiones (429)", kind="ratelimit",
            status=status, retryable=True,
        )
    if status >= 500:
        return CloudError(
            f"la API devolvio un error de servidor ({status}): {snippet}",
            kind="server", status=status, retryable=True,
        )
    if status == 404:
        return CloudError("job no encontrado en la API (404)", kind="api", status=status)
    return CloudError(
        f"la API rechazo la peticion ({status}): {snippet}", kind="api", status=status
    )


# --------------------------------------------------------------------------- #
# Configuracion
# --------------------------------------------------------------------------- #


def _flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _num(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class CloudConfig:
    base_url: str
    token: str
    model: str
    doc_orientation: bool
    doc_unwarping: bool
    chart_recognition: bool
    connect_timeout: float
    read_timeout: float
    probe_timeout: float
    job_timeout: float
    poll_interval: float
    max_failures: int
    cooldown: float
    max_concurrency: int
    submit_attempts: int

    @property
    def host_port(self) -> tuple[str, int]:
        """(host, puerto) del endpoint, para la sonda TCP."""
        parsed = urlsplit(self.base_url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if (parsed.scheme or "https") == "https" else 80)
        return host, port

    @classmethod
    def from_env(cls) -> "CloudConfig":
        return cls(
            base_url=os.getenv(
                "OCR_CLOUD_BASE_URL", "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
            ).rstrip("/"),
            token=os.getenv("OCR_CLOUD_TOKEN", "").strip(),
            model=os.getenv("OCR_CLOUD_MODEL", "PaddleOCR-VL-1.6").strip(),
            doc_orientation=_flag("OCR_CLOUD_DOC_ORIENTATION"),
            doc_unwarping=_flag("OCR_CLOUD_DOC_UNWARPING"),
            chart_recognition=_flag("OCR_CLOUD_CHART_RECOGNITION"),
            # OJO, esto no es solo el timeout de conexion: en `requests` este
            # valor se queda en el socket y termina gobernando TAMBIEN la
            # escritura del cuerpo. Medido contra esta API: con 10 s subian
            # 1 de cada 3 ficheros de 249 KB, con 60 s suben 3 de 3
            # (17-23 s cada uno, ~12 KB/s hasta el servidor de Baidu). El error
            # que se ve es 'The write operation timed out', que despista: no es
            # un fallo de red, es esta propio limite de tiempo.
            connect_timeout=_num("OCR_CLOUD_CONNECT_TIMEOUT", 10),
            # Timeout del socket al leer respuestas y descargar resultados.
            read_timeout=_num("OCR_CLOUD_READ_TIMEOUT", 120),
            # Sonda TCP previa. Evita pagar el timeout largo cuando el host esta
            # simplemente inalcanzable: en ese caso se falla en `probe_timeout`
            # segundos y se cae al motor local, sin acumular 60 s por intento.
            probe_timeout=_num("OCR_CLOUD_PROBE_TIMEOUT", 5),
            job_timeout=_num("OCR_CLOUD_JOB_TIMEOUT", 600),
            poll_interval=max(1.0, _num("OCR_CLOUD_POLL_INTERVAL", 3)),
            max_failures=max(1, int(_num("OCR_CLOUD_MAX_FAILURES", 3))),
            cooldown=max(1.0, _num("OCR_CLOUD_COOLDOWN", 60)),
            max_concurrency=max(1, int(_num("OCR_CLOUD_MAX_CONCURRENCY", 3))),
            submit_attempts=max(1, int(_num("OCR_CLOUD_SUBMIT_ATTEMPTS", 3))),
        )

    @property
    def configured(self) -> bool:
        return bool(self.token) and bool(self.base_url)

    def snapshot(self) -> dict[str, Any]:
        """Configuracion para /health. Nunca expone el token."""
        return {
            "enabled": self.configured,
            "model": self.model,
            "base_url": self.base_url,
            "job_timeout": self.job_timeout,
            "poll_interval": self.poll_interval,
            "max_concurrency": self.max_concurrency,
            "max_failures": self.max_failures,
            "cooldown": self.cooldown,
            "token": "configurado" if self.token else "ausente",
        }


# --------------------------------------------------------------------------- #
# Markdown -> texto plano
# --------------------------------------------------------------------------- #

_TAG_RE = re.compile(r"<[^>]+>")
_ROW_END_RE = re.compile(r"</t[dh]>\s*</tr>", re.I)
_CELL_RE = re.compile(r"</t[dh]>", re.I)
_OPEN_RE = re.compile(r"<t[dh][^>]*>|<tr[^>]*>|<table[^>]*>|</table>|<thead>|</thead>|<tbody>|</tbody>", re.I)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", re.S)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)


def markdown_to_text(markdown: str) -> str:
    """
    Aplana el Markdown de la API a texto legible.

    La API mete las facturas enteras dentro de un `<table>` HTML, asi que sin
    esto el `text` de una factura escaneada seria una sola linea con etiquetas.
    Las celdas se separan con tabulador y las filas con salto de linea.
    """
    if not markdown:
        return ""
    s = markdown
    s = _ROW_END_RE.sub("\n", s)
    s = _CELL_RE.sub("\t", s)
    s = _OPEN_RE.sub("", s)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s).replace("\xa0", " ")
    s = _BOLD_RE.sub(r"\1", s)
    s = _ITALIC_RE.sub(r"\1", s)
    s = _HEADING_RE.sub("", s)

    out: list[str] = []
    for raw_line in s.split("\n"):
        line = " ".join(raw_line.replace("\t", " \t ").split())
        line = line.replace(" \t ", "\t").strip("\t ")
        if line or (out and out[-1]):
            out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Guardas de calidad
#
# La API no publica confianza de texto, asi que la unica forma de detectar una
# alucinacion es mirar la ESCRITURA. En un documento en espanol, un bloque en
# chino es casi con seguridad un invento del modelo: es exactamente el caso que
# se midio en el fax ("晉書·齊桓公伐齊"). Se marca, no se borra: descartar texto
# en silencio es peor que entregarlo con una advertencia.
# --------------------------------------------------------------------------- #

_SCRIPT_RANGES: tuple[tuple[int, int, str], ...] = (
    (0x0041, 0x024F, "latin"),
    (0x0370, 0x03FF, "greek"),
    (0x0400, 0x052F, "cyrillic"),
    (0x0590, 0x05FF, "hebrew"),
    (0x0600, 0x06FF, "arabic"),
    (0x0750, 0x077F, "arabic"),
    (0x0900, 0x097F, "devanagari"),
    (0x0E00, 0x0E7F, "thai"),
    (0x3040, 0x30FF, "kana"),
    (0x3400, 0x4DBF, "han"),
    (0x4E00, 0x9FFF, "han"),
    (0xF900, 0xFAFF, "han"),
    (0xAC00, 0xD7AF, "hangul"),
)


def char_script(ch: str) -> str:
    """Clasifica un caracter por escritura. Solo importan las letras."""
    cp = ord(ch)
    if cp < 0x0041:
        return "ascii"
    for low, high, name in _SCRIPT_RANGES:
        if low <= cp <= high:
            return name
    return "other"


def dominant_script(text: str) -> str | None:
    """Escritura mayoritaria del texto contando solo letras. None si no hay."""
    tally: dict[str, int] = {}
    for ch in text:
        if not ch.isalpha():
            continue
        name = char_script(ch)
        if name in ("ascii",):
            name = "latin"
        tally[name] = tally.get(name, 0) + 1
    if not tally:
        return None
    return max(tally, key=lambda k: (tally[k], k))


def _overlap_ratio(inner: list[int], outer: list[int]) -> float:
    """
    Fraccion del rectangulo `inner` cubierta por `outer`.

    Se usa cobertura y no IoU porque las cajas de linea (43 en el fax) son
    minusculas frente al bloque fusionado que las contiene: su IoU es ~0 aunque
    esten claramente dentro.
    """
    ix1, iy1 = max(inner[0], outer[0]), max(inner[1], outer[1])
    ix2, iy2 = min(inner[2], outer[2]), min(inner[3], outer[3])
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    area = (inner[2] - inner[0]) * (inner[3] - inner[1])
    if area <= 0:
        return 0.0
    return ((ix2 - ix1) * (iy2 - iy1)) / area


# --------------------------------------------------------------------------- #
# Resultados
# --------------------------------------------------------------------------- #


@dataclass
class LayoutBox:
    """
    Region de linea detectada por el pipeline de layout.

    Es la unica granularidad con confianza que publica la API: una caja por
    linea, con score de DETECCION (no de acierto del texto).
    """

    label: str
    score: float | None
    coordinate: list[int] | None
    order: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "score": None if self.score is None else round(self.score, 6),
            "box": self.coordinate,
            "order": self.order,
            "score_kind": "layout_detection",
        }


@dataclass
class CloudBlock:
    """Bloque de contenido fusionado. `score` es el minimo de sus regiones."""

    label: str
    content: str
    box: list[int] | None
    order: int | None
    score: float | None
    regions: int = 0
    script: str | None = None
    suspect: bool = False
    suspect_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "label": self.label,
            "text": self.content,
            "box": self.box,
            "order": self.order,
            "score": None if self.score is None else round(self.score, 6),
            # El score NO mide si el texto es correcto, solo si ahi habia algo
            # con forma de linea. Se etiqueta para que nadie lo confunda.
            "score_kind": "layout_detection_min",
            "regions": self.regions,
        }
        if self.script:
            out["script"] = self.script
        if self.suspect:
            out["suspect"] = True
            out["suspect_reason"] = self.suspect_reason
        return out


@dataclass
class CloudPage:
    index: int
    text: str
    markdown: str
    blocks: list[CloudBlock]
    width: int | None
    height: int | None
    images: dict[str, str] = field(default_factory=dict)
    layout_image: str | None = None
    input_image: str | None = None
    layout_boxes: list[LayoutBox] = field(default_factory=list)
    script: str | None = None

    @property
    def alpha_ratio(self) -> float:
        chars = [c for c in self.text if not c.isspace()]
        if not chars:
            return 0.0
        return sum(c.isalnum() for c in chars) / len(chars)

    @property
    def suspect_blocks(self) -> list[CloudBlock]:
        return [b for b in self.blocks if b.suspect]

    @property
    def clean_text(self) -> str:
        """
        Texto sin los bloques marcados como sospechosos.

        Se ofrece aparte y NO se aplica por defecto: descartar texto en silencio
        es peor que entregarlo con una advertencia. Quien sepa que su documento
        es monolingue puede usar esto para quitarse de encima las alucinaciones.
        """
        if not self.suspect_blocks:
            return self.text
        kept = " ".join(b.content for b in self.blocks if not b.suspect)
        return markdown_to_text(kept) or self.text

    def as_dict(self, include_blocks: bool = True, include_images: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "page": f"page_{self.index + 1}",
            "size": {"width": self.width, "height": self.height},
            "text": self.text,
            "markdown": self.markdown,
            "alpha_ratio": round(self.alpha_ratio, 4),
            "blocks": len(self.blocks),
            "regions": len(self.layout_boxes),
        }
        if self.script:
            out["script"] = self.script
        if self.suspect_blocks:
            out["suspect"] = len(self.suspect_blocks)
            out["suspect_reason"] = self.suspect_blocks[0].suspect_reason
            out["text_clean"] = self.clean_text
        if include_blocks:
            out["lines"] = [b.as_dict() for b in self.blocks]
            out["layout_boxes"] = [b.as_dict() for b in self.layout_boxes]
        if include_images:
            out["images"] = self.images
            if self.layout_image:
                out["layout_image"] = self.layout_image
        return out


@dataclass
class CloudDocument:
    job_id: str
    pages: list[CloudPage]
    elapsed: float
    submit_s: float
    wait_s: float
    download_s: float
    num_pages: int
    server_span: str | None = None
    result_url: str | None = None
    file_type: str | None = None

    @property
    def text(self) -> str:
        return "\n\n".join(p.text for p in self.pages if p.text).strip()

    @property
    def markdown(self) -> str:
        return "\n\n".join(p.markdown for p in self.pages if p.markdown).strip()

    def as_dict(self, include_blocks: bool = True, include_images: bool = False) -> dict[str, Any]:
        return {
            "engine": "cloud",
            "job_id": self.job_id,
            "elapsed": round(self.elapsed, 4),
            "timing": {
                "submit": round(self.submit_s, 4),
                "wait": round(self.wait_s, 4),
                "download": round(self.download_s, 4),
            },
            "pages": len(self.pages),
            "reported_pages": self.num_pages,
            "server_span": self.server_span,
            "file_type": self.file_type,
            "text": self.text,
            "markdown": self.markdown,
            "results": [
                p.as_dict(include_blocks, include_images) for p in self.pages
            ],
        }


# --------------------------------------------------------------------------- #
# Cliente
# --------------------------------------------------------------------------- #


class CloudOCR:
    """Cliente con reintentos, cortacircuitos y control de concurrencia."""

    def __init__(self, config: CloudConfig | None = None) -> None:
        self.config = config or CloudConfig.from_env()
        self._session = requests.Session()
        # Reintentos solo en GET (idempotentes). El POST de subida NO se deja a
        # urllib3: reintentarlo a ciegas desde ahi duplicaria jobs sin control;
        # se hace a mano en `_submit` para poder espaciar y para poder contarlos.
        retry = Retry(
            total=3,
            connect=3,
            read=2,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=self.config.max_concurrency,
            pool_maxsize=self.config.max_concurrency * 2,
        )
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

        self._semaphore = threading.BoundedSemaphore(self.config.max_concurrency)
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at = 0.0
        self._last_error: str | None = None

    # -- cortacircuitos ------------------------------------------------------ #

    def available(self) -> tuple[bool, str]:
        """
        Si el cortacircuitos esta abierto no se intenta la nube: se va directo al
        motor local. Sin esto, con la API caida cada peticion pagaria entero el
        timeout antes de degradar.
        """
        if not self.config.configured:
            return False, "nube no configurada (falta OCR_CLOUD_TOKEN)"
        with self._lock:
            if self._failures >= self.config.max_failures:
                elapsed = time.monotonic() - self._opened_at
                if elapsed < self.config.cooldown:
                    return False, (
                        f"cortacircuitos abierto tras {self._failures} fallos "
                        f"({self.config.cooldown - elapsed:.0f}s restantes)"
                    )
                # Semiabierto: se cuela un intento de prueba.
                logger.info("Cortacircuitos de la nube en semiabierto: probando.")
            return True, ""

    def note_success(self) -> None:
        with self._lock:
            if self._failures:
                logger.info("Nube recuperada tras %d fallos.", self._failures)
            self._failures = 0
            self._last_error = None

    def note_failure(self, exc: BaseException) -> None:
        with self._lock:
            # Se satura el contador: solo importa si esta por debajo o por encima
            # del umbral, y asi `state()` no reporta numeros absurdos.
            if self._failures < self.config.max_failures:
                self._failures += 1
            self._opened_at = time.monotonic()
            self._last_error = f"{type(exc).__name__}: {exc}"
            if self._failures == self.config.max_failures:
                logger.warning(
                    "Cortacircuitos de la nube ABIERTO tras %d fallos: %s. "
                    "Se usara el motor local durante %.0fs.",
                    self._failures, exc, self.config.cooldown,
                )
            else:
                logger.warning("Fallo de la nube (%d): %s", self._failures, exc)

    def state(self) -> dict[str, Any]:
        with self._lock:
            open_ = self._failures >= self.config.max_failures
            remaining = 0.0
            if open_:
                remaining = max(0.0, self.config.cooldown - (time.monotonic() - self._opened_at))
            return {
                "failures": self._failures,
                "circuit": "open" if open_ and remaining > 0 else ("half-open" if open_ else "closed"),
                "cooldown_remaining": round(remaining, 1),
                "last_error": self._last_error,
            }

    # -- HTTP ---------------------------------------------------------------- #

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        try:
            return self._session.request(method, url, **kwargs)
        except requests.exceptions.SSLError as exc:
            raise CloudError(f"fallo TLS: {exc}", kind="network", retryable=False) from exc
        except requests.exceptions.Timeout as exc:
            raise CloudError(
                f"la API no respondio a tiempo: {exc}", kind="network", retryable=True
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise CloudError(
                f"fallo de red hablando con la API: {exc}", kind="network", retryable=True
            ) from exc

    def _post(self, url: str, **kwargs: Any) -> requests.Response:
        return self._request("POST", url, **kwargs)

    def _get(self, url: str, **kwargs: Any) -> requests.Response:
        return self._request("GET", url, **kwargs)

    def _auth(self, json_content: bool = False) -> dict[str, str]:
        headers = {"Authorization": f"bearer {self.config.token}"}
        if json_content:
            headers["Content-Type"] = "application/json"
        return headers

    def _payload(self) -> dict[str, Any]:
        return {
            "useDocOrientationClassify": self.config.doc_orientation,
            "useDocUnwarping": self.config.doc_unwarping,
            "useChartRecognition": self.config.chart_recognition,
        }

    def _reachable(self) -> None:
        """
        Sonda TCP barata antes de subir nada.

        El timeout largo de subida es necesario (el enlace a Baidu es lento),
        pero pagarlo cuando el host esta caido convertiria cada reintento en 60 s
        muertos. Un connect() de `probe_timeout` segundos distingue "lento" de
        "inalcanzable" y falla rapido en el segundo caso.

        Un `ConnectionRefusedError` NO se marca como reintentable: significa que
        hay alguien escuchando y no quiere hablar, o que no hay nadie. En ambos
        casos insistir 2s + 4s despues no cambia nada (medido: 9.5s -> 3.5s).
        Un timeout, en cambio, si puede ser transitorio.
        """
        host, port = self.config.host_port
        if not host:
            return
        try:
            with socket.create_connection((host, port), timeout=self.config.probe_timeout):
                return
        except ConnectionRefusedError as exc:
            raise CloudError(
                f"conexion rechazada en {host}:{port} ({exc})",
                kind="network",
                retryable=False,
            ) from exc
        except socket.timeout as exc:
            raise CloudError(
                f"la sonda TCP a {host}:{port} agoto {self.config.probe_timeout}s",
                kind="timeout",
                retryable=True,
            ) from exc
        except OSError as exc:
            raise CloudError(
                f"no hay ruta TCP a {host}:{port} ({exc})",
                kind="network",
                retryable=True,
            ) from exc

    def _submit_once(self, path: str | None, file_url: str | None) -> str:
        self._reachable()
        timeout = (self.config.connect_timeout, self.config.read_timeout)
        if file_url:
            resp = self._post(
                self.config.base_url,
                headers=self._auth(json_content=True),
                json={
                    "fileUrl": file_url,
                    "model": self.config.model,
                    "optionalPayload": self._payload(),
                },
                timeout=timeout,
            )
        else:
            assert path is not None
            with open(path, "rb") as fh:
                resp = self._post(
                    self.config.base_url,
                    headers=self._auth(),
                    data={
                        "model": self.config.model,
                        "optionalPayload": json.dumps(self._payload()),
                    },
                    files={"file": (os.path.basename(path), fh)},
                    timeout=timeout,
                )

        if resp.status_code != 200:
            raise _classify(resp.status_code, resp.text)
        try:
            body = resp.json()
        except ValueError as exc:
            raise CloudError(
                f"la API no devolvio JSON en la subida: {resp.text[:200]}",
                kind="parse", status=resp.status_code,
            ) from exc
        if body.get("code") not in (0, None):
            raise CloudError(
                f"la API rechazo el trabajo: {body.get('msg') or body}",
                kind="api", status=resp.status_code,
            )
        job_id = (body.get("data") or {}).get("jobId")
        if not job_id:
            raise CloudError("la API no devolvio jobId", kind="parse", status=resp.status_code)
        return str(job_id)

    def _submit(self, path: str | None, file_url: str | None) -> tuple[str, float]:
        """
        Sube el documento, con reintentos.

        Los reintentos no son un lujo, y lo medido contra esta API lo demuestra:
        con un timeout de socket de 10 s subia 1 fichero de 249 KB de cada 3
        (el enlace real va a ~12 KB/s, o sea 20 s de subida). Con 60 s suben los
        3. Es decir: el "fallo de red" era sobre todo un limite de tiempo
        demasiado corto. Los reintentos siguen siendo la red de seguridad para
        el caso de enlace realmente caido.
        """
        started = time.perf_counter()
        last: CloudError | None = None
        for attempt in range(1, self.config.submit_attempts + 1):
            try:
                job_id = self._submit_once(path, file_url)
                elapsed = time.perf_counter() - started
                if path:
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        size = 0
                    if size and elapsed > 0:
                        logger.info(
                            "Subida de %.1f KB en %.2fs (%.1f KB/s)",
                            size / 1024, elapsed, size / 1024 / elapsed,
                        )
                return job_id, elapsed
            except CloudError as exc:
                last = exc
                if not exc.retryable or attempt == self.config.submit_attempts:
                    raise
                delay = min(2.0 ** attempt, 20.0)
                logger.warning(
                    "Subida a la nube fallida (intento %d/%d): %s. Reintento en %.0fs.",
                    attempt, self.config.submit_attempts, exc, delay,
                )
                time.sleep(delay)
        assert last is not None
        raise last

    def _wait(self, job_id: str) -> tuple[dict[str, Any], float]:
        started = time.perf_counter()
        deadline = started + self.config.job_timeout
        interval = self.config.poll_interval
        last_state: str | None = None
        while True:
            if time.perf_counter() > deadline:
                raise CloudError(
                    f"el job {job_id} no termino en {self.config.job_timeout:.0f}s",
                    kind="timeout",
                )
            resp = self._get(
                f"{self.config.base_url}/{job_id}",
                headers=self._auth(),
                timeout=(self.config.connect_timeout, self.config.read_timeout),
            )
            if resp.status_code != 200:
                raise _classify(resp.status_code, resp.text)
            try:
                data = resp.json().get("data") or {}
            except ValueError as exc:
                raise CloudError(
                    f"respuesta de sondeo ilegible: {resp.text[:200]}", kind="parse"
                ) from exc

            state = data.get("state")
            if state != last_state:
                progress = data.get("extractProgress") or {}
                logger.info(
                    "Job %s: %s%s", job_id, state,
                    f" ({progress.get('extractedPages')}/{progress.get('totalPages')} paginas)"
                    if progress.get("totalPages") else "",
                )
                last_state = state

            if state == "done":
                return data, time.perf_counter() - started
            if state == "failed":
                raise CloudError(
                    f"la API fallo el job: {data.get('errorMsg') or 'sin detalle'}", kind="api"
                )
            if state not in (None, "pending", "running"):
                logger.warning("Estado de job desconocido: %r", state)
            time.sleep(interval)
            # Sondeo adaptativo: al principio cada 3 s, luego mas espaciado para
            # no castigar la API en documentos largos.
            interval = min(interval * 1.3, 15.0)

    def _iter_jsonl(self, json_url: str) -> Iterator[dict[str, Any]]:
        """
        Descarga el JSONL en flujo, linea a linea.

        No se hace `resp.text` completo: en un documento de cientos de paginas el
        cuerpo entero puede ser muy grande y no hay razon para tenerlo en memoria.
        """
        with self._get(
            json_url,
            timeout=(self.config.connect_timeout, max(self.config.read_timeout, 180.0)),
            stream=True,
        ) as resp:
            if resp.status_code != 200:
                raise _classify(resp.status_code, "descarga del resultado")
            for raw in resp.iter_lines(decode_unicode=False):
                if not raw or not raw.strip():
                    continue
                try:
                    yield json.loads(raw)
                except ValueError as exc:
                    raise CloudError(
                        f"linea de JSONL ilegible: {raw[:200]!r}", kind="parse"
                    ) from exc

    @staticmethod
    def _box_of(block: dict[str, Any]) -> list[int] | None:
        bbox = block.get("block_bbox")
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            try:
                return [int(round(float(v))) for v in bbox]
            except (TypeError, ValueError):
                return None
        return None

    def _parse(self, lines: Iterator[dict[str, Any]]) -> tuple[list[CloudPage], int, str | None]:
        pages: list[CloudPage] = []
        reported = 0
        file_type: str | None = None

        for entry in lines:
            error_code = entry.get("errorCode")
            if error_code not in (0, None):
                raise CloudError(
                    f"la API devolvio errorCode={error_code}: {entry.get('errorMsg')}",
                    kind="api",
                )
            result = entry.get("result") or {}
            info = result.get("dataInfo") or {}
            if info.get("numPages"):
                reported += int(info["numPages"])
            file_type = info.get("type") or file_type

            for item in result.get("layoutParsingResults") or []:
                pruned = item.get("prunedResult") or {}
                markdown = (item.get("markdown") or {}).get("text") or ""

                # Cajas de layout: una por linea, con score de deteccion. Es la
                # unica confianza que publica la API y la unica granularidad
                # fina; se conserva entera porque el bloque fusionado la pierde.
                layout: list[LayoutBox] = []
                for raw in ((pruned.get("layout_det_res") or {}).get("boxes")) or []:
                    coord = raw.get("coordinate")
                    layout.append(
                        LayoutBox(
                            label=str(raw.get("label") or ""),
                            score=_as_float(raw.get("score")),
                            coordinate=(
                                [int(round(float(v))) for v in coord]
                                if isinstance(coord, (list, tuple)) and len(coord) == 4
                                else None
                            ),
                            order=raw.get("order"),
                        )
                    )

                blocks = [
                    CloudBlock(
                        label=str(b.get("block_label") or ""),
                        content=str(b.get("block_content") or ""),
                        box=self._box_of(b),
                        order=b.get("block_order"),
                        score=None,
                    )
                    for b in (pruned.get("parsing_res_list") or [])
                ]

                # Score por bloque = el MINIMO de las cajas que caen dentro.
                # El minimo, y no la media: una tabla con 43 lineas buenas y una
                # mala debe delatarse, no esconderse en el promedio.
                for block in blocks:
                    if not block.box:
                        continue
                    inside = [
                        b.score
                        for b in layout
                        if b.coordinate
                        and b.score is not None
                        and _overlap_ratio(b.coordinate, block.box) >= 0.5
                    ]
                    block.regions = len(inside)
                    block.score = min(inside) if inside else None

                page_text = markdown_to_text(markdown)
                page_script = dominant_script(page_text)

                # Guarda de alucinacion: se marca un bloque cuya escritura no es
                # la del documento solo si esa escritura es una minoria clara
                # (menos del 25% de las letras), para no marcar documentos
                # realmente bilingues.
                weights: dict[str, int] = {}
                for block in blocks:
                    block.script = dominant_script(block.content)
                    if block.script:
                        weights[block.script] = weights.get(block.script, 0) + sum(
                            1 for c in block.content if c.isalpha()
                        )
                total_letters = sum(weights.values())
                for block in blocks:
                    if (
                        page_script
                        and block.script
                        and block.script != page_script
                        and total_letters
                        and weights.get(page_script, 0) / total_letters >= 0.75
                    ):
                        block.suspect = True
                        block.suspect_reason = (
                            f"escritura '{block.script}' en un documento '{page_script}'"
                        )

                output_images = item.get("outputImages") or {}
                pages.append(
                    CloudPage(
                        index=len(pages),
                        text=page_text,
                        markdown=markdown,
                        blocks=blocks,
                        width=pruned.get("width"),
                        height=pruned.get("height"),
                        images={
                            str(k): str(v)
                            for k, v in ((item.get("markdown") or {}).get("images") or {}).items()
                        },
                        layout_image=output_images.get("layout_det_res"),
                        input_image=item.get("inputImage"),
                        layout_boxes=layout,
                        script=page_script,
                    )
                )
        return pages, (reported or len(pages)), file_type

    # -- API publica --------------------------------------------------------- #

    def run(self, path: str | None = None, file_url: str | None = None) -> CloudDocument:
        """Ejecuta el OCR completo en la nube y devuelve las paginas."""
        if not self.config.configured:
            raise CloudError("nube no configurada (falta OCR_CLOUD_TOKEN)", kind="config")
        if not path and not file_url:
            raise CloudError("hay que indicar un fichero o una URL", kind="config")

        started = time.perf_counter()
        acquired = self._semaphore.acquire(timeout=self.config.job_timeout)
        if not acquired:
            raise CloudError(
                "demasiados trabajos en curso contra la API",
                kind="busy", retryable=False,
            )
        try:
            job_id, submit_s = self._submit(path, file_url)
            logger.info("Job %s enviado en %.2fs (%s)", job_id, submit_s, path or file_url)
            data, wait_s = self._wait(job_id)

            progress = data.get("extractProgress") or {}
            span = None
            if progress.get("startTime") and progress.get("endTime"):
                span = f"{progress['startTime']} -> {progress['endTime']}"

            json_url = ((data.get("resultUrl") or {}).get("jsonUrl")) or ""
            if not json_url:
                raise CloudError("el job termino sin resultUrl.jsonUrl", kind="parse")

            download_started = time.perf_counter()
            pages, reported, file_type = self._parse(self._iter_jsonl(json_url))
            download_s = time.perf_counter() - download_started

            if not pages:
                raise CloudError("la API no devolvio ninguna pagina", kind="empty")

            doc = CloudDocument(
                job_id=job_id,
                pages=pages,
                elapsed=time.perf_counter() - started,
                submit_s=submit_s,
                wait_s=wait_s,
                download_s=download_s,
                num_pages=reported,
                server_span=span,
                result_url=json_url,
                file_type=file_type,
            )
            logger.info(
                "Job %s listo: %d pagina(s), %.2fs (subida %.2f, espera %.2f, bajada %.2f)",
                job_id, len(pages), doc.elapsed, submit_s, wait_s, download_s,
            )
            self.note_success()
            return doc
        except CloudError as exc:
            self.note_failure(exc)
            raise
        except Exception as exc:  # red de seguridad: nunca tumbar la peticion
            self.note_failure(exc)
            raise CloudError(f"fallo inesperado de la nube: {exc}", kind="unexpected") from exc
        finally:
            self._semaphore.release()

    def close(self) -> None:
        self._session.close()
