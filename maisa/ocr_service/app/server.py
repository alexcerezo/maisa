"""
Servidor OCR REST - CPU ARM64.

Dos motores, en este orden de preferencia:

  1. Nube (PaddleOCR-VL, API de Baidu AI Studio): mejor lectora, sobre todo en
     documentos degradados. Ver `app/cloud.py` para las advertencias de calidad.
  2. Local (RapidOCR / ONNX Runtime sobre PP-OCRv5): nunca necesita red y nunca
     alucina, pero confunde glifos en escaneos malos.

El motor se elige con `OCR_ENGINE=auto|cloud|local`. En `auto` se intenta la
nube y, si falla o no esta configurada, se continua con el local sin que la
peticion se pierda.

Devuelve JSON con el texto reconocido, su score de confianza y el recuadro
(bounding box) de cada linea detectada.

Modelos configurables por variables de entorno (ver .env / README.md).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Iterator

import numpy as np
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from PIL import Image
from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR

try:  # ejecutado como paquete (`uvicorn app.server:app`)
    from .cloud import CloudConfig, CloudError, CloudOCR
    from .importes import a_convencion_es
except ImportError:  # ejecutado como script (`python app/server.py`)
    from cloud import CloudConfig, CloudError, CloudOCR  # type: ignore[no-redef]
    from importes import a_convencion_es  # type: ignore[no-redef]

# --------------------------------------------------------------------------- #
# Configuracion (variables de entorno)
# --------------------------------------------------------------------------- #

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ocr-server")

# Mapa aceptado -> enum, para poder pasar strings por env / query params.
_MODEL_TYPES = {
    "tiny": ModelType.TINY,
    "small": ModelType.SMALL,
    "medium": ModelType.MEDIUM,
    "mobile": ModelType.MOBILE,
    "server": ModelType.SERVER,
}
_OCR_VERSIONS = {
    "PP-OCRv4": OCRVersion.PPOCRV4,
    "PP-OCRv5": OCRVersion.PPOCRV5,
    "PP-OCRv6": OCRVersion.PPOCRV6,
}

#: Cuota real de CPU del contenedor, que **no** es `os.cpu_count()`: en Docker
#: sin `--cpuset-cpus` el proceso ve los nucleos del anfitrion aunque su cuota
#: sea de dos. Dimensionar el pool por `cpu_count()` seria prometer paralelismo
#: que la cuota no da: los motores se pelearian por el mismo tiempo de CPU y
#: cada inferencia iria mas lenta, no mas rapido.
def _cuota_cpu() -> float | None:
    """Cuota de CPU en nucleos segun cgroup v2 (`cpu.max`) o v1, o `None`."""
    for ruta, v1 in (("/sys/fs/cgroup/cpu.max", False), ("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", True)):
        try:
            crudo = Path(ruta).read_text(encoding="utf-8").split()
        except OSError:
            continue
        if v1:
            if crudo and crudo[0] != "-1":
                periodo = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text().split()
                if periodo:
                    return int(crudo[0]) / int(periodo[0])
            continue
        if len(crudo) >= 2 and crudo[0] != "max":
            return int(crudo[0]) / int(crudo[1])
    return None


def nucleos_efectivos() -> int:
    """Nucleos utilizables: la cuota del cgroup si la hay, si no los del sistema."""
    cuota = _cuota_cpu()
    if cuota:
        return max(1, int(cuota))
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except AttributeError:  # pragma: no cover - solo en sistemas sin afinidad
        return max(1, os.cpu_count() or 1)


DEFAULTS = {
    # Deteccion: mobile = 58 ms en CPU vs 383 ms del server (por 4.8 pts de Hmean).
    # En 2 nucleos la balanza se inclina claramente hacia mobile.
    "det_version": os.getenv("OCR_DET_VERSION", "PP-OCRv5"),
    "det_model_type": os.getenv("OCR_DET_MODEL_TYPE", "mobile"),
    # Reconocimiento: mobile. Medido sobre 4 PDFs reales a escala 4.0, mobile
    # empata en calidad con server (score 0.9219 vs 0.9177, alfanumericos
    # 91.78% vs 91.70%) siendo 2.80x mas rapido y con un modelo de 16 MB en vez
    # de 85 MB. La ventaja de server solo aparecia a escala 2.0, es decir,
    # cuando el renderizado ya habia destruido el texto. Ver README.
    "rec_version": os.getenv("OCR_REC_VERSION", "PP-OCRv5"),
    "rec_model_type": os.getenv("OCR_REC_MODEL_TYPE", "mobile"),
    # Clasificador de orientacion (lineas rotadas 180). El modelo es minusculo.
    "use_cls": os.getenv("OCR_USE_CLS", "true").lower() in ("1", "true", "yes"),
    # Resolucion de entrada de la deteccion (multiplo de 32).
    "det_limit_side_len": int(os.getenv("OCR_DET_LIMIT_SIDE_LEN", "960")),
    # Descarta lineas por debajo de este score.
    "text_score": float(os.getenv("OCR_TEXT_SCORE", "0.5")),
    # Escala de renderizado para PDFs. 4.0 = 288 dpi. El reconocedor reescala
    # cada recorte a 48 px de alto: a 144 dpi las lineas de un fax degradado
    # miden ~20 px y se amplian 2.4x por interpolacion, destruyendo el texto
    # (el modelo devuelve puntuacion suelta con scores altos). Medido: 0% de
    # alfanumericos a escala 2.0 frente a 92% a 4.0, y ademas mas rapido porque
    # el detector deja de emitir decenas de cajas basura. Los escaneos limpios
    # son insensibles a la escala (~+20% de tiempo). Ver README.
    "pdf_scale": float(os.getenv("OCR_PDF_SCALE", "4.0")),
    # Escalada automatica: si una pagina devuelve basura (poca proporcion de
    # caracteres alfanumericos) se reintenta a mas resolucion. Ver README.
    "auto_scale": os.getenv("OCR_AUTO_SCALE", "true").lower() in ("1", "true", "yes"),
    # Escala a la que se reintenta cuando la primera pasada no da texto real.
    "auto_scale_max": float(os.getenv("OCR_AUTO_SCALE_MAX", "5.0")),
    # Umbral de "texto creible": % de caracteres alfanumericos sobre el total.
    "min_alpha_ratio": float(os.getenv("OCR_MIN_ALPHA_RATIO", "0.5")),
    # Tope de pixeles por pagina renderizada, para no agotar memoria en A3 a
    # escalas altas (una A3 a escala 5 son ~35 Mpx = 105 MB en RGB).
    "max_pixels": float(os.getenv("OCR_MAX_PIXELS", "2.4e7")),
    # Tamano del pool de motores locales. Por defecto, la cuota de CPU del
    # contenedor: mas motores que nucleos solo reparten el mismo tiempo de CPU
    # entre mas bocas. 0 o vacio = automatico.
    "workers": max(1, int(os.getenv("OCR_WORKERS") or 0) or nucleos_efectivos()),
    # --- Seleccion de motor -------------------------------------------------
    # auto  = nube primero, local como red de seguridad (recomendado)
    # cloud = solo nube: un fallo devuelve 503, sin gastar CPU
    # local = solo local: ni se toca la red
    "engine": os.getenv("OCR_ENGINE", "auto").strip().lower(),
    # --- Ficheros grandes ---------------------------------------------------
    # Tope del cuerpo subido. Se comprueba MIENTRAS se escribe en disco, asi que
    # un fichero de 10 GB se rechaza sin llegar a ocupar 10 GB.
    "max_upload_mb": float(os.getenv("OCR_MAX_UPLOAD_MB", "300")),
    # Tope de paginas procesadas. 0 = sin tope.
    "max_pages": int(os.getenv("OCR_MAX_PAGES", "0")),
    # Los temporales van a disco, no a memoria: un PDF de 300 MB no debe
    # depender del tamano del heap.
    "tmp_dir": os.getenv("OCR_TMP_DIR", "/tmp/ocr-work"),
}

if DEFAULTS["engine"] not in ("auto", "cloud", "local"):
    logger.warning(
        "OCR_ENGINE=%r no reconocido; se usara 'auto'.", DEFAULTS["engine"]
    )
    DEFAULTS["engine"] = "auto"

# Con mas de un motor, cada inferencia compite por los mismos nucleos. Si
# ademas OpenCV abre su propio pool por llamada (por defecto, uno por nucleo),
# dos motores piden cuatro hilos a dos nucleos: el sistema los alterna y cada
# operacion va mas lenta. El paralelismo lo dan los motores, no los hilos de
# dentro. Con un solo motor se deja como estaba: ahi si interesa que una
# inferencia use todo lo que hay.
if DEFAULTS["workers"] > 1:
    try:
        import cv2

        cv2.setNumThreads(1)
        logger.info(
            "OpenCV limitado a 1 hilo: el paralelismo lo dan los %d motores.",
            DEFAULTS["workers"],
        )
    except ImportError:  # pragma: no cover - rapidocr siempre trae cv2
        pass

# --------------------------------------------------------------------------- #
# Motor OCR
# --------------------------------------------------------------------------- #

#: Motores locales vivos. Uno por worker y **cada uno usado por un solo hilo a
#: la vez**: RapidOCR no es thread-safe (muta estado interno entre llamadas).
#: Antes habia un unico motor detras de un `Lock` global, que serializaba todas
#: las inferencias del servicio: cuatro peticiones simultaneas tardaban lo mismo
#: que cuatro en fila (medido: 4.48 s una, 20.25 s las cuatro), y el lote de
#: escaneos no bajaba de ahi. El cuello de botella no era el modelo, era el
#: candado.
#:
#: `OMP_NUM_THREADS=1` es parte del trato: el paralelismo se pide por motores
#: (uno por nucleo), no dentro de una inferencia. Medido con 2 nucleos, una
#: inferencia tarda ~3.0 s con un hilo de OpenMP y ~3.9 s con dos, asi que
#: repartir el trabajo es estrictamente mejor que ensancharlo.
_engines: "queue.Queue[RapidOCR]" | None = None
_engines_lock = threading.Lock()


def _build_params() -> dict[str, Any]:
    """Parametros de RapidOCR, con los hilos de ONNX ajustados al pool.

    Con mas de un motor, cada inferencia se queda con **un** hilo de ONNX
    Runtime. `OMP_NUM_THREADS=1` ya deja OpenMP en un hilo, asi que el pool
    interno de ONNX (por defecto, uno por nucleo) no reparte trabajo: se pelea
    consigo mismo. Medido sobre una factura escaneada real (escala 4, tres
    pasadas intercaladas de cada variante): reparto automatico 5.03 s de
    mediana (minimo 4.26 s), un hilo 3.78 s (minimo 3.69 s). Con un solo motor
    se deja el automatico, que ahi si interesa que una inferencia use todo.
    """
    params: dict[str, Any] = {
        "Det.engine_type": EngineType.ONNXRUNTIME,
        "Det.ocr_version": _OCR_VERSIONS[DEFAULTS["det_version"]],
        "Det.model_type": _MODEL_TYPES[DEFAULTS["det_model_type"]],
        "Det.limit_side_len": DEFAULTS["det_limit_side_len"],
        "Det.limit_type": "min",
        "Rec.engine_type": EngineType.ONNXRUNTIME,
        "Rec.ocr_version": _OCR_VERSIONS[DEFAULTS["rec_version"]],
        "Rec.model_type": _MODEL_TYPES[DEFAULTS["rec_model_type"]],
        "Cls.engine_type": EngineType.ONNXRUNTIME,
        "Cls.ocr_version": OCRVersion.PPOCRV4,
        "Cls.model_type": ModelType.MOBILE,
        "Global.use_cls": DEFAULTS["use_cls"],
        "Global.text_score": DEFAULTS["text_score"],
    }
    if DEFAULTS["workers"] > 1:
        params["EngineConfig.onnxruntime.intra_op_num_threads"] = 1
        params["EngineConfig.onnxruntime.inter_op_num_threads"] = 1
    return params


def get_engine() -> RapidOCR:
    """Motor **nuevo**: construir un pool de motores, no compartir uno solo.

    Cada motor cuesta RAM (medido: ~1.4 GB residentes con el motor cargado y
    ~1.9 GB con dos), asi que se construyen de uno en uno y solo los que pide
    `OCR_WORKERS`.
    """
    params = _build_params()
    logger.info("Cargando modelos: %s", params)
    return RapidOCR(params=params)


def motor_pool() -> "queue.Queue[RapidOCR]":
    """Cola con los motores locales ya cargados (se crea la primera vez).

    El tamano lo fija `OCR_WORKERS` (por defecto, la cuota de CPU del
    contenedor). `_run_page` saca un motor, infiere y lo devuelve: con la cola
    vacia la peticion **espera**, que es lo que queremos (mejor en fila que
    peleando por la CPU), pero sin bloquear a las demas si hay motor libre.
    """
    global _engines
    if _engines is None:
        with _engines_lock:
            if _engines is None:
                cola: "queue.Queue[RapidOCR]" = queue.Queue()
                for _ in range(DEFAULTS["workers"]):
                    cola.put(get_engine())
                _engines = cola
                logger.info("Pool de OCR listo: %d motor(es).", DEFAULTS["workers"])
    return _engines


def wants_local_engine() -> bool:
    """Si el motor local debe estar cargado para atender peticiones."""
    return DEFAULTS["engine"] in ("auto", "local")


def warmup() -> None:
    """
    Fuerza la descarga/carga de modelos para que /health sea fiable.

    Con `OCR_ENGINE=cloud` se omite a proposito: cargar RapidOCR cuesta ~2 GB de
    RAM residente que en ese modo no se usarian jamas.

    Se calientan **todos** los motores del pool, cada uno con su inferencia: un
    motor recien construido paga en su primera llamada la inicializacion de las
    sesiones de ONNX Runtime (~1 s), y sin calentarlo esa penalizacion se la
    llevaria la primera peticion de verdad que le tocara.
    """
    if not wants_local_engine():
        logger.info(
            "OCR_ENGINE=%s: no se carga el motor local (se ahorran ~2 GB de RAM).",
            DEFAULTS["engine"],
        )
        return

    cola = motor_pool()
    # Imagen sintetica con texto -> ejercita det + cls + rec.
    img = np.full((160, 640, 3), 255, dtype=np.uint8)
    img[60:100, 40:600] = 0
    for _ in range(cola.qsize()):
        motor = cola.get()
        try:
            motor(img)
        except Exception as exc:  # pragma: no cover - solo informativo
            logger.warning("Warmup devolvio un aviso (no bloqueante): %s", exc)
        finally:
            cola.put(motor)
    logger.info("Modelos locales listos (%d motor/es).", cola.qsize())


# --------------------------------------------------------------------------- #
# Motor en la nube
# --------------------------------------------------------------------------- #

_cloud: CloudOCR | None = None
_cloud_lock = threading.Lock()


def get_cloud() -> CloudOCR:
    """Cliente de nube unico: reutiliza conexiones y el estado del cortacircuitos."""
    global _cloud
    if _cloud is None:
        with _cloud_lock:
            if _cloud is None:
                _cloud = CloudOCR(CloudConfig.from_env())
    return _cloud


def cloud_config_snapshot() -> dict[str, Any]:
    """Config de nube sin el token (jamas se expone por HTTP)."""
    try:
        return CloudConfig.from_env().snapshot()
    except Exception as exc:  # pragma: no cover - defensivo
        return {"error": str(exc)}


# --------------------------------------------------------------------------- #
# Carga de imagenes (imagen suelta o PDF multipagina)
# --------------------------------------------------------------------------- #


class _Source:
    """
    Documento en disco, capaz de re-renderizar sus paginas a distintas escalas.

    Trabaja sobre una RUTA, no sobre `bytes`: pypdfium2 mapea el fichero en
    memoria, asi que un PDF de 300 MB no necesita 300 MB de heap ni una copia
    extra al pasarlo por los endpoints.

    La escala importa mucho mas de lo que parece: el reconocedor reescala cada
    recorte a 48 px de alto. En un fax degradado las lineas miden ~20 px a
    144 dpi, asi que se amplian 2.4x por interpolacion y el texto se destruye.
    Renderizar a mas resolucion le da al modelo pixeles reales que leer.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._pdf = None
        self._image: Image.Image | None = None

        with path.open("rb") as handle:
            head = handle.read(5)

        if head == b"%PDF-":
            try:
                import pypdfium2 as pdfium
            except ImportError as exc:  # pragma: no cover
                raise HTTPException(
                    status_code=415,
                    detail="El soporte de PDF no esta instalado en este contenedor.",
                ) from exc
            self._pdf = pdfium.PdfDocument(str(path))
            total = len(self._pdf)
            if total == 0:
                raise HTTPException(status_code=422, detail="El PDF no contiene paginas.")
        else:
            try:
                self._image = Image.open(path)
                self._image.load()
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=422, detail=f"Imagen ilegible: {exc}") from exc
            total = 1

        # Tope de paginas: se recorta aqui para que TODOS los caminos (nube,
        # local y streaming) vean el mismo `count`.
        cap = DEFAULTS["max_pages"]
        if cap > 0 and total > cap:
            logger.warning("El documento tiene %d paginas; se procesaran %d.", total, cap)
            total = cap
        self.count = total

    @property
    def is_pdf(self) -> bool:
        return self._pdf is not None

    def base_scale(self) -> float:
        """Escala de partida: la configurada para PDFs, 1.0 para imagenes."""
        return DEFAULTS["pdf_scale"] if self.is_pdf else 1.0

    def scales(self, requested: float | None, auto: bool | None = None) -> list[float]:
        """
        Escalas a probar, en orden.

        Si `requested` viene del usuario, se respeta y NO se escala
        automaticamente (control explicito).
        """
        if requested is not None:
            return [requested]

        enabled = DEFAULTS["auto_scale"] if auto is None else auto
        base = self.base_scale()
        if not enabled:
            return [base]

        ladder = [base]
        top = max(DEFAULTS["auto_scale_max"], base) if self.is_pdf else 2.0
        if top > base:
            ladder.append(top)
        return ladder

    def _clamp(self, width: int, height: int, scale: float) -> float:
        """Reduce la escala si la pagina renderizada superaria OCR_MAX_PIXELS."""
        total = width * height * scale * scale
        limit = DEFAULTS["max_pixels"]
        if total <= limit:
            return scale
        return (limit / (width * height)) ** 0.5

    def render(self, index: int, scale: float) -> np.ndarray:
        if self._pdf is not None:
            page = self._pdf[index]
            scale = self._clamp(round(page.get_width()), round(page.get_height()), scale)
            bitmap = page.render(scale=scale)
            return np.asarray(bitmap.to_pil().convert("RGB"))

        assert self._image is not None
        img = self._image.convert("RGB")
        scale = self._clamp(img.width, img.height, scale)
        if scale != 1.0:
            size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
            img = img.resize(size, Image.LANCZOS)
        return np.asarray(img)

    def page_name(self, index: int) -> str:
        return f"page_{index + 1}" if self.is_pdf else "image"

    def close(self) -> None:
        """Suelta el descriptor del PDF (obligatorio en ficheros grandes)."""
        if self._pdf is not None:
            self._pdf.close()
            self._pdf = None


# --------------------------------------------------------------------------- #
# Inferencia
# --------------------------------------------------------------------------- #


def _run_page(img: np.ndarray) -> tuple[list[dict[str, Any]], float]:
    """Infiere una pagina con el motor que este libre (y lo devuelve al acabar)."""
    cola = motor_pool()
    motor = cola.get()
    started = time.perf_counter()
    try:
        result = motor(img)
    finally:
        cola.put(motor)
    elapsed = time.perf_counter() - started

    boxes = getattr(result, "boxes", None)
    if boxes is None or getattr(result, "txts", None) is None:
        return [], elapsed

    lines: list[dict[str, Any]] = []
    for box, text, score in zip(boxes, result.txts, result.scores):
        clean = (text or "").strip()
        if not clean:
            continue
        lines.append(
            {
                "text": clean,
                "score": round(float(score), 6),
                "box": np.asarray(box).astype(int).tolist(),
            }
        )
    return lines, elapsed


def _summarize(all_lines: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [line["score"] for line in all_lines]
    if not scores:
        return {"lines": 0, "mean_score": 0.0, "min_score": 0.0}
    return {
        "lines": len(scores),
        "mean_score": round(sum(scores) / len(scores), 6),
        "min_score": round(min(scores), 6),
    }


def _alpha_ratio(lines: list[dict[str, Any]]) -> float:
    """% de caracteres alfanumericos sobre el total reconocido."""
    chars = [c for line in lines for c in line["text"]]
    if not chars:
        return 0.0
    return sum(c.isalnum() or c.isspace() for c in chars) / len(chars)


def _looks_like_text(lines: list[dict[str, Any]]) -> bool:
    """
    Distingue texto real de ruido.

    Un fax mal escaneado hace que el detector devuelva puntuacion suelta
    ("...", "…", ":") con scores altos: el score NO sirve como señal. La
    proporcion de alfanumericos si (0% en ruido, 85-90% en texto real).
    """
    if not lines:
        return False
    return _alpha_ratio(lines) >= DEFAULTS["min_alpha_ratio"]


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("Arrancando OCR server (threads=%s)", os.getenv("OMP_NUM_THREADS"))
    try:
        warmup()
    except Exception:
        logger.exception("Fallo durante el warmup. Revisa la configuracion de modelos.")
        raise
    yield
    logger.info("Parando OCR server.")


app = FastAPI(
    title="OCR API (RapidOCR / ONNX Runtime)",
    description="OCR CPU sobre PP-OCRv5: devuelve texto + score de confianza.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, Any]:
    engine_mode = DEFAULTS["engine"]
    local_ready = _engines is not None
    cloud = cloud_config_snapshot()

    # Un unico campo `status` para que el healthcheck del contenedor siga
    # funcionando igual que antes.
    if engine_mode == "cloud":
        status = "ok"
    elif engine_mode == "local":
        status = "ok" if local_ready else "loading"
    else:  # auto: sirve si CUALQUIERA de los dos motores esta disponible
        status = "ok" if (local_ready or cloud.get("enabled")) else "loading"

    return {
        "status": status,
        "engine": engine_mode,
        "engines": {
            "local": {
                "enabled": wants_local_engine(),
                "loaded": local_ready,
                "runtime": "onnxruntime",
                "models": {
                    "det": f'{DEFAULTS["det_version"]}_{DEFAULTS["det_model_type"]}',
                    "rec": f'{DEFAULTS["rec_version"]}_{DEFAULTS["rec_model_type"]}',
                    "cls": "PP-OCRv4_mobile" if DEFAULTS["use_cls"] else None,
                },
                "det_limit_side_len": DEFAULTS["det_limit_side_len"],
                "text_score_threshold": DEFAULTS["text_score"],
                # Motores locales en paralelo: cada uno atiende una inferencia a
                # la vez, y `libres` dice cuantos hay sin usar en este momento.
                "workers": DEFAULTS["workers"],
                "idle": _engines.qsize() if _engines is not None else 0,
            },
            "cloud": {
                **cloud,
                # Estado del cortacircuitos: si esta en `open`, las peticiones
                # van directas al local sin pagar el timeout. Se consulta
                # siempre (crear el cliente es solo abrir una `requests.Session`)
                # porque si no, `/health` devolvia `null` hasta la primera OCR.
                "circuit": get_cloud().state(),
            },
        },
        "threads": int(os.getenv("OMP_NUM_THREADS", "0")) or None,
        "pdf_scale": DEFAULTS["pdf_scale"],
        "auto_scale": DEFAULTS["auto_scale"],
        "auto_scale_max": DEFAULTS["auto_scale_max"],
        "limits": {
            "max_upload_mb": DEFAULTS["max_upload_mb"],
            "max_pages": DEFAULTS["max_pages"] or None,
            "max_pixels": DEFAULTS["max_pixels"],
            "tmp_dir": DEFAULTS["tmp_dir"],
        },
    }


@app.get("/cloud")
def cloud_status() -> dict[str, Any]:
    """Estado del motor en la nube, sin exponer el token."""
    client = get_cloud()
    return {
        "config": client.config.snapshot(),
        "circuit": client.state(),
        "capabilities": {
            "text_confidence": False,
            "note": "la API solo publica score de deteccion de region, no de texto",
            "hallucination_guard": True,
        },
    }



def _text_alpha_ratio(text: str) -> float:
    """% de caracteres alfanumericos (o espacios) sobre el total."""
    chars = list(text)
    if not chars:
        return 0.0
    return sum(c.isalnum() or c.isspace() for c in chars) / len(chars)


def _alpha_ratio(lines: list[dict[str, Any]]) -> float:
    """% de caracteres alfanumericos sobre el total reconocido."""
    return _text_alpha_ratio("".join(line["text"] for line in lines))


def _looks_like_text(lines: list[dict[str, Any]]) -> bool:
    """
    Distingue texto real de ruido.

    Un fax mal escaneado hace que el detector devuelva puntuacion suelta
    ("...", "…", ":") con scores altos: el score NO sirve como señal. La
    proporcion de alfanumericos si (0% en ruido, 85-90% en texto real).

    Ojo: esto NO detecta alucinaciones de la nube (el chino inventado da 91%).
    Para eso esta el guardia de escritura de `cloud.py`.
    """
    if not lines:
        return False
    return _alpha_ratio(lines) >= DEFAULTS["min_alpha_ratio"]


# --------------------------------------------------------------------------- #
# Motor local
# --------------------------------------------------------------------------- #


def _run_local_page(
    source: _Source, index: int, scales: list[float], include_boxes: bool
) -> dict[str, Any]:
    """
    OCR local de una pagina, probando la escalera de escalas.

    Devuelve la entrada de resultado, lista para el JSON o para NDJSON.
    """
    best: dict[str, Any] | None = None
    attempts: list[dict[str, Any]] = []

    for rank, current in enumerate(scales):
        img = source.render(index, current)
        lines, elapsed = _run_page(img)
        # El bitmap NO se guarda en `attempts`: una pagina A3 a escala 5 son
        # ~105 MB y la escalera llegaba a retener varias vivas a la vez.
        attempt = {
            "scale": current,
            "size": {"width": int(img.shape[1]), "height": int(img.shape[0])},
            "lines": lines,
            "elapsed": elapsed,
            "alpha_ratio": _alpha_ratio(lines),
        }
        del img
        attempts.append(attempt)

        if best is None or _looks_like_text(lines):
            best = attempt
        # Primera escala que produzca texto creible: nos quedamos con ella.
        if _looks_like_text(lines):
            break
        # Escalas agotadas sin texto creible: conservar la menos mala.
        if rank == len(scales) - 1:
            best = max(attempts, key=lambda a: (a["alpha_ratio"], len(a["lines"])))

    assert best is not None
    lines = best["lines"]
    if not include_boxes:
        for line in lines:
            line.pop("box", None)

    entry: dict[str, Any] = {
        "page": source.page_name(index),
        "size": best["size"],
        "elapsed": round(best["elapsed"], 4),
        "scale": best["scale"],
        "alpha_ratio": round(best["alpha_ratio"], 4),
        "lines": lines,
        "text": "\n".join(line["text"] for line in lines),
    }
    if len(attempts) > 1:
        entry["attempts"] = [
            {
                "scale": a["scale"],
                "lines": len(a["lines"]),
                "alpha_ratio": round(a["alpha_ratio"], 4),
                "elapsed": round(a["elapsed"], 4),
            }
            for a in attempts
        ]
    entry["_lines"] = lines  # uso interno; se retira antes de serializar
    return entry


def _public(entry: dict[str, Any]) -> dict[str, Any]:
    """Quita las claves de uso interno antes de entregar el resultado."""
    entry.pop("_lines", None)
    return entry


def _normaliza_importes(payload: dict[str, Any]) -> dict[str, Any]:
    """Reescribe los importes del payload a convencion espanola, in situ.

    Es el unico punto donde se toca el texto de los DOS motores, para que salgan
    del servicio con el mismo formato numerico. `a_convencion_es` es idempotente,
    asi que da igual que un numero se visite dos veces (el texto de pagina y el
    de su lista de lineas son el mismo objeto).
    """
    for clave in ("text", "text_clean"):
        if isinstance(payload.get(clave), str):
            payload[clave] = a_convencion_es(payload[clave])
    for clave in ("lines", "results"):
        for entrada in payload.get(clave) or []:
            if not isinstance(entrada, dict):
                continue
            if isinstance(entrada.get("text"), str):
                entrada["text"] = a_convencion_es(entrada["text"])
            for linea in entrada.get("lines") or []:
                if isinstance(linea, dict) and isinstance(linea.get("text"), str):
                    linea["text"] = a_convencion_es(linea["text"])
    return payload


def _iter_local(
    source: _Source,
    include_boxes: bool,
    scale: float | None,
    auto_scale: bool | None,
) -> Iterator[dict[str, Any]]:
    """Genera los resultados pagina a pagina (permite NDJSON incremental)."""
    scales = source.scales(scale, auto_scale)
    for index in range(source.count):
        yield _run_local_page(source, index, scales, include_boxes)


def _local_stats(pages: list[dict[str, Any]]) -> dict[str, Any]:
    all_lines = [line for page in pages for line in page["_lines"]]
    return _summarize(all_lines)


def _local_document(
    source: _Source,
    filename: str | None,
    include_boxes: bool,
    scale: float | None,
    auto_scale: bool | None,
) -> dict[str, Any]:
    started = time.perf_counter()
    pages = list(_iter_local(source, include_boxes, scale, auto_scale))
    elapsed = time.perf_counter() - started
    stats = _local_stats(pages)

    # Se copia la referencia ANTES de que _public retire las claves internas.
    single_lines = pages[0]["_lines"] if len(pages) == 1 else None

    payload: dict[str, Any] = {
        "file": filename,
        "engine": "local",
        "pages": len(pages),
        "elapsed": round(elapsed, 4),
        "stats": stats,
        "results": [_public(page) for page in pages],
    }
    if single_lines is not None:
        payload["lines"] = single_lines
        payload["text"] = pages[0]["text"]
    return _normaliza_importes(payload)


# --------------------------------------------------------------------------- #
# Motor en la nube
# --------------------------------------------------------------------------- #


def _cloud_document(
    path: Path,
    filename: str | None,
    include_boxes: bool,
) -> dict[str, Any]:
    """OCR en la nube, ya normalizado al mismo sobre que el motor local."""
    document = get_cloud().run(path=str(path))
    payload = document.as_dict(include_blocks=include_boxes)
    payload["file"] = filename
    payload["pages"] = len(document.pages)
    payload["stats"] = _cloud_stats(document)
    if any(p.suspect_blocks for p in document.pages):
        # Se entrega aparte, sin tocar `text`: quitar texto por cuenta propia
        # seria peor que avisar de que ahi hay algo raro.
        payload["text_clean"] = "\n\n".join(
            p.clean_text for p in document.pages if p.clean_text
        ).strip()
    if len(document.pages) == 1 and include_boxes:
        page = document.pages[0]
        payload["lines"] = [b.as_dict() for b in page.blocks]
        payload["text"] = page.text
    return _normaliza_importes(payload)


def _cloud_stats(document: Any) -> dict[str, Any]:
    """
    Resumen de la nube.

    Deliberadamente SIN `mean_score`: la API no publica confianza de texto, y
    rellenarlo con el score de deteccion de region seria enganar al que lee el
    JSON. Se publican por separado y etiquetados.
    """
    pages = document.pages
    all_boxes = [b for p in pages for b in p.layout_boxes]
    scores = [b.score for b in all_boxes if b.score is not None]
    suspect = [b for p in pages for b in p.suspect_blocks]
    stats: dict[str, Any] = {
        "text_confidence": None,
        "text_confidence_note": (
            "la API no devuelve score de texto; los scores son de deteccion de region"
        ),
        "regions": len(all_boxes),
        "blocks": sum(len(p.blocks) for p in pages),
        "suspect_blocks": len(suspect),
    }
    if scores:
        stats["min_region_score"] = round(min(scores), 6)
        stats["mean_region_score"] = round(sum(scores) / len(scores), 6)
    if suspect:
        stats["suspect_reason"] = suspect[0].suspect_reason
    return stats


# --------------------------------------------------------------------------- #
# Enrutado
# --------------------------------------------------------------------------- #


def _resolve_engine(requested: str | None) -> str:
    engine = (requested or DEFAULTS["engine"]).strip().lower()
    if engine not in ("auto", "cloud", "local"):
        raise HTTPException(
            status_code=400,
            detail=f"engine={engine!r} no valido. Usa auto, cloud o local.",
        )
    return engine


def _fallback_note(exc: CloudError) -> dict[str, Any]:
    return {
        "engine": "cloud",
        "kind": exc.kind,
        "status": exc.status,
        "retryable": exc.retryable,
        "reason": str(exc),
    }


def _local_payload(
    path: Path,
    filename: str | None,
    include_boxes: bool,
    scale: float | None,
    auto_scale: bool | None,
    chosen: str,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ejecuta el motor local y etiqueta de donde viene la respuesta."""
    source = _Source(path)
    try:
        payload = _local_document(source, filename, include_boxes, scale, auto_scale)
    finally:
        source.close()
    payload["requested_engine"] = chosen
    if fallback is not None:
        payload["fallback"] = fallback
    return payload


def _process(
    path: Path,
    filename: str | None,
    include_boxes: bool,
    scale: float | None,
    auto_scale: bool | None,
    engine: str | None = None,
    compare: bool = False,
) -> dict[str, Any]:
    """
    Nucleo del OCR: elige motor, ejecuta y, si la nube falla, cae al local.

    Ojo: NO se debe invocar el endpoint `ocr` desde otro endpoint pasandole sus
    parametros: los `Query(...)` sin resolver llegarian como objetos en vez de
    valores. Por eso el nucleo esta aqui, fuera de la capa HTTP.
    """
    chosen = _resolve_engine(engine)

    # `local` explicito: ni se toca la red.
    if chosen == "local":
        return _local_payload(
            path, filename, include_boxes, scale, auto_scale, chosen
        )

    cloud = get_cloud()

    if not cloud.config.configured:
        if chosen == "cloud":
            raise HTTPException(
                status_code=503,
                detail="Motor en la nube no configurado (falta OCR_CLOUD_TOKEN).",
            )
        logger.warning("Nube sin configurar; se usa el motor local.")
        return _local_payload(
            path,
            filename,
            include_boxes,
            scale,
            auto_scale,
            chosen,
            {"engine": "cloud", "kind": "config", "reason": "sin token"},
        )

    # Cortacircuitos: si la nube acaba de fallar varias veces seguidas, no se
    # vuelve a pagar la sonda. Sin este paso, con la API caida cada peticion
    # costaba el timeout entero antes de degradar al motor local.
    breaker_ok, breaker_reason = cloud.available()
    if not breaker_ok:
        if chosen == "cloud":
            raise HTTPException(
                status_code=503,
                detail=f"Motor en la nube no disponible: {breaker_reason}.",
            )
        logger.warning("Nube descartada por el cortacircuitos (%s); motor local.", breaker_reason)
        return _local_payload(
            path,
            filename,
            include_boxes,
            scale,
            auto_scale,
            chosen,
            {
                "engine": "cloud",
                "kind": "circuit_open",
                "status": None,
                "retryable": True,
                "reason": breaker_reason,
            },
        )

    # A partir de aqui: nube primero (nube siempre, salvo fallo).
    reference: dict[str, Any] | None = None
    try:
        payload = _cloud_document(path, filename, include_boxes)
    except CloudError as exc:
        cloud.note_failure(exc)
        if chosen == "cloud":
            raise HTTPException(
                status_code=503,
                detail=f"Motor en la nube no disponible: {exc}",
            ) from exc
        logger.warning("Nube no disponible (%s): se continua con el motor local.", exc)
        return _local_payload(
            path,
            filename,
            include_boxes,
            scale,
            auto_scale,
            chosen,
            _fallback_note(exc),
        )

    cloud.note_success()
    payload["requested_engine"] = chosen

    if compare:
        # Se ejecuta tambien el local para poder comparar. Sirve para localizar
        # justo donde falla el modelo propio, que es el objetivo del proyecto.
        source = _Source(path)
        try:
            reference = _local_document(source, filename, include_boxes, scale, auto_scale)
        except Exception as exc:  # la comparacion nunca debe romper la peticion
            logger.warning("No se pudo generar la referencia local: %s", exc)
        finally:
            source.close()
        if reference is not None:
            reference.pop("file", None)
            payload["local_reference"] = reference

    return payload


# --------------------------------------------------------------------------- #
# Subida de ficheros a disco
# --------------------------------------------------------------------------- #


def _tmp_dir() -> Path:
    path = Path(DEFAULTS["tmp_dir"])
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"No se puede crear OCR_TMP_DIR={path}: {exc}",
        ) from exc
    if not os.access(path, os.W_OK):
        # Caso tipico: volumen nombrado creado por el demonio como root:root
        # mientras el contenedor corre con el usuario `ocr`.
        raise HTTPException(
            status_code=500,
            detail=(
                f"OCR_TMP_DIR={path} no es escribible por el usuario del "
                "contenedor. Revisa el dueño del volumen."
            ),
        )
    return path


async def _spool(file: UploadFile) -> Path:
    """
    Vuelca la subida a un fichero temporal en trozos.

    Nunca se carga el cuerpo entero en memoria: un PDF de 2 GB no debe caber en
    el heap del contenedor. El tope se comprueba a medida que llegan los trozos,
    asi que un fichero demasiado grande se aborta al pasarse, no al final.
    """
    limit = int(DEFAULTS["max_upload_mb"] * 1024 * 1024)
    suffix = Path(file.filename or "upload").suffix.lower()[:16]
    handle = tempfile.NamedTemporaryFile(
        dir=_tmp_dir(), suffix=suffix, prefix="ocr-", delete=False
    )
    written = 0
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if limit and written > limit:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"El archivo supera OCR_MAX_UPLOAD_MB="
                        f"{DEFAULTS['max_upload_mb']:.0f} MB."
                    ),
                )
            handle.write(chunk)
        handle.close()
    except BaseException:
        handle.close()
        os.unlink(handle.name)
        raise

    if written == 0:
        os.unlink(handle.name)
        raise HTTPException(status_code=400, detail="Archivo vacio.")
    return Path(handle.name)


def _discard(path: Path | None) -> None:
    """Borra el temporal. Nunca propaga errores."""
    if path is None:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


@app.post("/ocr")
async def ocr(
    file: UploadFile = File(..., description="Imagen (png/jpg/webp) o PDF"),
    include_boxes: bool = Query(True, description="Incluir coordenadas de cada linea"),
    scale: float | None = Query(
        None,
        gt=0.1,
        le=8.0,
        description="Escala de renderizado. Si se indica, desactiva la escalada automatica.",
    ),
    auto_scale: bool | None = Query(
        None, description="Forzar on/off la escalada automatica de resolucion"
    ),
    engine: str | None = Query(
        None,
        description="auto (nube con respaldo local), cloud (solo nube) o local. "
        "Por defecto, OCR_ENGINE.",
    ),
    compare: bool = Query(
        False,
        description="Ejecutar tambien el motor local y devolverlo en `local_reference`.",
    ),
) -> JSONResponse:
    """Ejecuta OCR y devuelve texto + score por linea."""
    path = await _spool(file)
    try:
        payload = await run_in_threadpool(
            _process, path, file.filename, include_boxes, scale, auto_scale, engine, compare
        )
    finally:
        _discard(path)
    return JSONResponse(payload)


@app.post("/ocr/text")
async def ocr_text_only(
    file: UploadFile = File(..., description="Imagen o PDF"),
    scale: float | None = Query(None, gt=0.1, le=8.0),
    auto_scale: bool | None = Query(None),
    engine: str | None = Query(None, description="auto, cloud o local"),
) -> JSONResponse:
    """Version minima: solo texto y score, sin coordenadas."""
    path = await _spool(file)
    try:
        payload = await run_in_threadpool(
            _process, path, file.filename, False, scale, auto_scale, engine, False
        )
    finally:
        _discard(path)
    return JSONResponse(payload)


@app.post("/ocr/stream")
async def ocr_stream(
    file: UploadFile = File(..., description="Imagen o PDF grande"),
    include_boxes: bool = Query(False, description="Incluir coordenadas de cada linea"),
    scale: float | None = Query(None, gt=0.1, le=8.0),
    auto_scale: bool | None = Query(None),
    engine: str | None = Query(None, description="auto, cloud o local"),
) -> StreamingResponse:
    """
    Igual que `/ocr` pero entrega NDJSON: un objeto JSON por linea, a medida que
    cada pagina termina.

    Pensado para documentos grandes: el cliente puede procesar la pagina 1 sin
    esperar a la 400 y sin que su parser tenga que sostener el documento entero.
    Eventos: `start`, `page`, `fallback`, `error`, `done`.
    """
    chosen = _resolve_engine(engine)
    path = await _spool(file)

    async def stream() -> Any:
        try:
            async for line in _stream_ndjson(path, file.filename, include_boxes, scale, auto_scale, chosen):
                yield line
        finally:
            _discard(path)

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
    )


async def _stream_ndjson(
    path: Path,
    filename: str | None,
    include_boxes: bool,
    scale: float | None,
    auto_scale: bool | None,
    chosen: str,
) -> Any:
    """
    Genera NDJSON.

    Aviso: al ser una respuesta en streaming las cabeceras ya se han enviado
    cuando se conoce el primer error, asi que un fallo total NO puede cambiar el
    codigo HTTP. El cliente debe comprobar que llego un evento `done`.
    """

    def emit(event: str, **fields: Any) -> str:
        return json.dumps({"event": event, **fields}, ensure_ascii=False) + "\n"

    started = time.perf_counter()
    source: _Source | None = None
    try:
        # Abrir primero permite anunciar el numero de paginas antes de trabajar.
        source = await run_in_threadpool(_Source, path)
        yield emit("start", file=filename, pages=source.count, engine=chosen)

        cloud = get_cloud()
        # Mismo cortacircuitos que en /ocr: una API caida no debe costar el
        # timeout entero en cada peticion.
        breaker_ok, breaker_reason = (
            cloud.available() if cloud.config.configured else (False, "sin token")
        )

        if chosen in ("auto", "cloud") and breaker_ok:
            try:
                payload = await run_in_threadpool(_cloud_document, path, filename, include_boxes)
            except CloudError as exc:
                cloud.note_failure(exc)
                if chosen == "cloud":
                    yield emit("error", error=str(exc), kind=exc.kind)
                    return
                yield emit("fallback", fallback=_fallback_note(exc))
            else:
                cloud.note_success()
                for page in payload["results"]:
                    yield emit("page", result=page)
                yield emit(
                    "done",
                    engine="cloud",
                    job_id=payload.get("job_id"),
                    pages=payload["pages"],
                    elapsed=payload.get("elapsed"),
                    stats=payload["stats"],
                )
                return
        elif chosen in ("auto", "cloud"):
            note = {
                "engine": "cloud",
                "kind": "config" if not cloud.config.configured else "circuit_open",
                "status": None,
                "retryable": True,
                "reason": breaker_reason,
            }
            if chosen == "cloud":
                yield emit("error", error=f"Motor en la nube no disponible: {breaker_reason}.")
                return
            yield emit("fallback", fallback=note)

        # Motor local, pagina a pagina.
        if not wants_local_engine():
            yield emit("error", error="Motor local deshabilitado (OCR_ENGINE=cloud).")
            return

        scales = source.scales(scale, auto_scale)
        all_lines: list[dict[str, Any]] = []
        done = 0
        for index in range(source.count):
            entry = await run_in_threadpool(
                _run_local_page, source, index, scales, include_boxes
            )
            # `pop` en la misma linea: _public ya no tiene nada que retirar y,
            # sobre todo, las lineas siguen disponibles para las estadisticas.
            all_lines.extend(entry.pop("_lines"))
            yield emit("page", result=entry)
            done += 1

        yield emit(
            "done",
            engine="local",
            pages=done,
            elapsed=round(time.perf_counter() - started, 4),
            stats=_summarize(all_lines),
        )
    except HTTPException as exc:
        yield emit("error", error=str(exc.detail), status=exc.status_code)
    except Exception as exc:  # noqa: BLE001 - se informa por el flujo
        logger.exception("Fallo en /ocr/stream")
        yield emit("error", error=str(exc))
    finally:
        if source is not None:
            source.close()

