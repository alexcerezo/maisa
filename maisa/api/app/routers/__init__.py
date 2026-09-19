"""Routers de la API. Todos cuelgan de /api salvo la salud y el visor."""

from . import asientos, estadisticas, facturas, health, meta, ocr

__all__ = ["asientos", "estadisticas", "facturas", "health", "meta", "ocr"]
