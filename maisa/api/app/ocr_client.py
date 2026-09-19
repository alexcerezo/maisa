"""Proxy del servicio de OCR.

El frontend no debe conocer ni la URL ni el contrato del OCR: habla con la API y
la API reenvia. Asi hay un unico origen, un unico punto de observabilidad y el
OCR puede cambiar de host sin tocar el visor.

Los errores del OCR se **propagan con su codigo**: si el OCR dice 400 (fichero
no soportado) o 503 (motor en la nube caido y sin respaldo local), el cliente ve
ese mismo estado con el mensaje del OCR, no un 500 opaco.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .errors import ApiError

logger = logging.getLogger("albertitos-api")

# Estados del OCR que se reenvian tal cual. El resto de 4xx/5xx se normaliza a
# 502 (dependencia que responde mal) para no inventar semantica ajena.
ESTADOS_PROPAGADOS = {400, 401, 403, 404, 409, 413, 415, 422, 429, 500, 502, 503, 504}

TAMANO_TROZO = 1024 * 1024


class OcrClient:
    """Cliente HTTP reutilizado contra el servicio de OCR."""

    def __init__(self, base_url: str, timeout_s: float = 180.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._cliente = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_s, connect=5.0),
            headers={"User-Agent": "albertitos-api/1.0"},
        )

    async def cerrar(self) -> None:
        await self._cliente.aclose()

    # ------------------------------------------------------------------ #
    # Salud
    # ------------------------------------------------------------------ #
    async def salud(self, timeout_s: float) -> dict[str, Any]:
        """GET /health del OCR. Lanza si no responde (lo traduce el router)."""
        respuesta = await self._cliente.get("/health", timeout=timeout_s)
        respuesta.raise_for_status()
        try:
            return respuesta.json()
        except ValueError:
            return {"status": "desconocido"}

    # ------------------------------------------------------------------ #
    # OCR de un documento
    # ------------------------------------------------------------------ #
    async def procesar(
        self,
        *,
        nombre: str,
        contenido: bytes,
        content_type: str | None,
        engine: str | None = None,
        detalle: bool = False,
    ) -> dict[str, Any]:
        ruta = "/ocr" if detalle else "/ocr/text"
        parametros = {"engine": engine} if engine else None
        ficheros = {"file": (nombre or "documento", contenido, content_type or "application/octet-stream")}

        inicio = time.perf_counter()
        try:
            respuesta = await self._cliente.post(ruta, params=parametros, files=ficheros)
        except httpx.TimeoutException as exc:
            logger.warning("OCR: tiempo agotado (%s)", exc)
            raise ApiError(
                504,
                "tiempo_agotado",
                "El servicio de OCR no respondio a tiempo.",
                {"url": self.base_url, "timeout_s": self.timeout_s},
            ) from None
        except httpx.HTTPError as exc:
            logger.warning("OCR: no se pudo contactar (%s)", exc)
            raise ApiError(
                503,
                "ocr_no_disponible",
                "El servicio de OCR no esta disponible.",
                {"url": self.base_url},
            ) from None
        segundos = time.perf_counter() - inicio

        if respuesta.status_code >= 400:
            raise _error_de_ocr(respuesta)

        try:
            payload = respuesta.json()
        except ValueError:
            raise ApiError(
                502,
                "respuesta_ocr_invalida",
                "El servicio de OCR devolvio una respuesta que no es JSON.",
                {"url": self.base_url, "status": respuesta.status_code},
            ) from None

        return _normalizar(payload, segundos, len(contenido))


def _error_de_ocr(respuesta: httpx.Response) -> ApiError:
    """Convierte un error del OCR en un ApiError con el mismo estado."""
    mensaje = "El servicio de OCR devolvio un error."
    detalle: dict[str, Any] | None = None
    try:
        cuerpo = respuesta.json()
    except ValueError:
        cuerpo = None
    if isinstance(cuerpo, dict):
        posible = cuerpo.get("detail") or cuerpo.get("error") or cuerpo.get("mensaje")
        if isinstance(posible, str) and posible:
            mensaje = posible
        elif isinstance(posible, dict):
            mensaje = str(posible.get("mensaje") or posible.get("codigo") or mensaje)
            detalle = posible
    status = respuesta.status_code if respuesta.status_code in ESTADOS_PROPAGADOS else 502
    if status >= 500:
        mensaje = f"El servicio de OCR fallo: {mensaje}"
    detalle = {**(detalle or {}), "ocr_status": respuesta.status_code}
    return ApiError(status, "error_ocr", mensaje, detalle)


def _normalizar(payload: dict[str, Any], segundos: float, tamano: int) -> dict[str, Any]:
    """Sobre comun para el visor: texto plano + metadatos, sin perder el bruto."""
    texto = _extraer_texto(payload)
    stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    lineas = payload.get("lines")
    if lineas is None and isinstance(payload.get("results"), list):
        lineas = sum(len(pagina.get("lines") or []) for pagina in payload["results"])
    return {
        "texto": texto,
        "score": stats.get("mean_score"),
        "score_nota": stats.get("text_confidence_note"),
        "motor": payload.get("engine"),
        "paginas": payload.get("pages"),
        "lineas": lineas,
        "segundos_ocr": payload.get("elapsed"),
        "segundos_proxy": round(segundos, 4),
        "bytes_enviados": tamano,
        "stats": stats,
        "bruto": payload,
    }


def _extraer_texto(payload: dict[str, Any]) -> str:
    """El OCR pone `text` solo en documentos de una pagina; si no, hay `results`."""
    if isinstance(payload.get("text"), str) and payload["text"].strip():
        return payload["text"]
    if isinstance(payload.get("text_clean"), str) and payload["text_clean"].strip():
        return payload["text_clean"]
    partes = []
    for pagina in payload.get("results") or []:
        if isinstance(pagina, dict) and isinstance(pagina.get("text"), str):
            partes.append(pagina["text"])
    return "\n\n".join(parte for parte in partes if parte.strip())


async def leer_con_tope(fichero: Any, max_bytes: int) -> bytes:
    """Lee el upload por trozos y corta en cuanto se pasa del tope.

    Se comprueba mientras se lee: un fichero de 10 GB se rechaza sin llegar a
    ocupar 10 GB en memoria.
    """
    trozos: list[bytes] = []
    total = 0
    while True:
        trozo = await fichero.read(TAMANO_TROZO)
        if not trozo:
            break
        total += len(trozo)
        if total > max_bytes:
            raise ApiError(
                413,
                "demasiado_grande",
                f"El fichero supera el limite de {max_bytes // (1024 * 1024)} MB.",
            )
        trozos.append(trozo)
    if total == 0:
        raise ApiError(400, "fichero_vacio", "No se ha recibido ningun fichero.")
    return b"".join(trozos)
