"""Obtencion del texto de una factura, con la escalera mas barata primero.

Escalon 1: la capa de texto del PDF (exacta, 0 coste). Escalon 2: el motor de
vision del contenedor ``ocr-api``. La cache se indexa por ``sha256`` del PDF,
no por nombre: un fichero renombrado no vuelve a pagar OCR.
"""
from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import requests
from pypdf import PdfReader

from .texto import Lectura, extrae

OCR_URL = "http://127.0.0.1:8866"
CACHE_OCR = Path(__file__).resolve().parents[2] / ".cache" / "ocr"

# Caracteres que indican que la capa de texto no es texto real.
_RE_BASURA = re.compile(r"[\ufffd\u0000-\u0008\u000b\u000c\u000e-\u001f]")


@dataclass
class Documento:
    """Resultado de leer un PDF: su lectura y como se obtuvo."""

    lectura: Lectura
    sha256: str
    escalon: str
    cache: bool
    segundos: float
    calidad: float


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


def ocr_contenedor(ruta: Path, timeout: int = 300) -> str:
    """Escalon 2: manda el PDF al contenedor y devuelve el texto reconocido."""
    with ruta.open("rb") as fh:
        respuesta = requests.post(
            f"{OCR_URL}/ocr",
            params={"engine": "local"},
            files={"file": (ruta.name, fh, "application/pdf")},
            timeout=timeout,
        )
    respuesta.raise_for_status()
    return respuesta.json().get("text", "") or ""


def _cache_ocr(ruta_cache: Path, sha: str, texto: str) -> None:
    ruta_cache.parent.mkdir(parents=True, exist_ok=True)
    ruta_cache.write_text(
        json.dumps({"sha256": sha, "texto": texto}, ensure_ascii=False), encoding="utf-8"
    )


def lee(ruta: Path, umbral_calidad: float = 0.6, usar_cache: bool = True) -> Documento:
    """Lee una factura aplicando la escalera texto -> vision, con cache."""
    import time

    arranque = time.monotonic()
    sha = sha256_pdf(ruta)
    ruta_cache = CACHE_OCR / f"{sha}.json"

    meta = metadatos_texto(ruta)
    texto, paginas = capa_texto(ruta)
    calidad = calidad_texto(texto, paginas)
    if calidad >= umbral_calidad:
        return Documento(
            lectura=extrae(texto, ruta.name, paginas, "texto_determinista", meta),
            sha256=sha, escalon="capa_texto", cache=False,
            segundos=time.monotonic() - arranque, calidad=calidad,
        )

    if usar_cache and ruta_cache.exists():
        try:
            datos = json.loads(ruta_cache.read_text(encoding="utf-8"))
            if datos.get("sha256") == sha and datos.get("texto"):
                return Documento(
                    lectura=extrae(datos["texto"], ruta.name, paginas, "vision_ocr", meta),
                    sha256=sha, escalon="cache_ocr", cache=True,
                    segundos=time.monotonic() - arranque, calidad=calidad,
                )
        except (json.JSONDecodeError, OSError):
            pass

    texto_ocr = ocr_contenedor(ruta)
    if texto_ocr:
        _cache_ocr(ruta_cache, sha, texto_ocr)
    return Documento(
        lectura=extrae(texto_ocr, ruta.name, paginas, "vision_ocr", meta),
        sha256=sha, escalon="vision_ocr", cache=False,
        segundos=time.monotonic() - arranque,
        calidad=max(calidad, calidad_texto(texto_ocr, paginas)),
    )


def lee_lote(
    rutas: list[Path], trabajadores: int = 3, umbral_calidad: float = 0.6
) -> list[Documento]:
    """Lee un lote en paralelo conservando el orden de entrada."""
    if not rutas:
        return []
    with ThreadPoolExecutor(max_workers=trabajadores) as pool:
        return list(pool.map(lambda r: lee(r, umbral_calidad), rutas))
