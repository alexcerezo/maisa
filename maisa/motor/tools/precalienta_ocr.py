"""Precalienta la cache de OCR del motor.

La entrega tiene que poder reproducirse **sin red y sin contenedor de OCR**
(ver el docstring de ``maisa.lectura``). Los 471 documentos que traen capa de
texto se leen gratis con ``pypdf``; los 29 escaneos no, y son los que hacen que
el lote dependa de que el servicio de vision este vivo. Esta herramienta paga
esa dependencia una vez y la deja escrita en ``motor/.cache/ocr/``, que si esta
versionado en git: a partir de ahi el lote corre en cualquier sitio.

Vivia en ``jev/scripts/precalentar-ocr.ts``, cuando era un paquete Node. Se
reescribio en Python al retirar Jev por dos razones: el motor es Python, y la
version TypeScript **duplicaba** el formato de la cache (``VERSION_CACHE``,
``firma_motor``, ``firma_nube``, la forma del JSON) en lugar de importarlo, asi
que podia desincronizarse del motor sin que nada lo detectara. Aqui se usan las
funciones del motor tal cual: si el formato cambia, esto cambia con el.

Uso, desde ``maisa/motor/``, con el servicio de vision levantado:

    ../../.venv/bin/python tools/precalienta_ocr.py
    ../../.venv/bin/python tools/precalienta_ocr.py --forzar
    ../../.venv/bin/python tools/precalienta_ocr.py --cache /tmp/ocr-experimento

Por defecto hace **solo los 29 escaneos**: un documento que la capa de texto
resuelve no llega nunca al peldano de cache, asi que precalentarlo seria pagar
minutos de vision por una entrada que nadie lee. ``--todas`` levanta esa
restriccion, que es lo que pide una evaluacion que quiera comparar motores sobre
los 500.

**Procedencia.** ``motor`` y ``escalon`` se derivan del ``engine`` que devuelve
**la respuesta**, no del que se pidio: con ``auto`` la nube puede fallar y la
pagina acaba leida en local. Estampar la firma local a todo dejaria las
lecturas de nube indistinguibles de las locales, y entonces la cache serviria
texto de nube a un brazo local. Por eso el defecto es ``--engine local``, que es
el unico motor que el motor usa de verdad: su escalera apaga la nube.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

from maisa.lectura import (  # noqa: E402
    CACHE_OCR,
    OcrNoDisponible,
    _escribe_cache,
    _lee_cache,
    _ocr_url,
    _paginas_geo,
    _paginas_respuesta,
    _peticion_ocr,
    capa_texto,
    calidad_texto,
    firma_motor,
    firma_nube,
    sha256_pdf,
)

#: Mismo umbral que usa `lectura.lee`: por encima de esto el motor para en la
#: capa de texto y **nunca** mira la cache. Precalentar esos documentos seria
#: pagar 15 minutos de OCR por ficheros que nadie va a leer.
UMBRAL_CALIDAD = 0.6


def ya_cacheada(
    ruta_cache: Path, sha: str, paginas_pdf: int, firma: str, firma_nube_actual: str
) -> bool:
    """Una entrada ya vale si el motor la aceptaria y trae todas las paginas.

    La autoridad es ``_lee_cache``: es exactamente la regla con la que el motor
    decide entre tirar de cache o volver a llamar al servicio. Tener aqui una
    copia de esa regla es lo que fallaba: se exigia el campo ``paginas``, que
    solo traen las entradas v2, asi que las **legacy** (unicamente ``sha256`` y
    ``texto``) no se reconocian y el lote las volvia a leer enteras. Y la cache
    del motor esta commiteada justo en ese formato.

    Una entrada que el motor rechazaria (otro motor, otra version) cuenta como
    no precalentada: en la entrega eso obliga a tener el servicio vivo.
    """
    entrada = _lee_cache(ruta_cache, sha, firma, firma_nube_actual)
    if entrada is None:
        return False
    return paginas_pdf == 0 or len(entrada.paginas) >= paginas_pdf


def precalienta_una(
    ruta: Path,
    motor: str,
    cache: Path,
    forzar: bool,
    timeout: float,
    conexion: float,
    firma: str = "",
    firma_nube_actual: str = "",
) -> str:
    """Una factura. Devuelve ``ok``, ``vacio``, ``saltada`` o ``nube``."""
    sha = sha256_pdf(ruta)
    ruta_cache = cache / f"{sha}.json"
    _, paginas_pdf = capa_texto(ruta)
    if not forzar and ya_cacheada(ruta_cache, sha, paginas_pdf, firma, firma_nube_actual):
        return "saltada"

    datos = _peticion_ocr(ruta, motor, timeout, conexion)
    paginas = _paginas_respuesta(datos)
    if not paginas:
        return "vacio"

    # Quien leyo de verdad, no quien se pidio: en `auto` la nube puede fallar.
    de_nube = datos.get("engine") == "cloud"
    _escribe_cache(
        ruta_cache, sha, paginas,
        "vision_nube" if de_nube else "vision_ocr",
        firma or firma_motor(), firma_nube_actual or firma_nube(),
        geo=_paginas_geo(datos),
    )
    return "nube" if de_nube else "ok"


def main() -> int:
    parser = argparse.ArgumentParser(description="Precalienta la cache de OCR del corpus.")
    parser.add_argument("--dir", default=str(RAIZ.parent / "data" / "facturas"), help="carpeta con los PDFs")
    parser.add_argument("--cache", default=str(CACHE_OCR),
                        help="carpeta de cache de destino (por defecto la del motor, que esta en git)")
    parser.add_argument("--ocr", default=_ocr_url(), help="URL del servicio de vision")
    parser.add_argument("--engine", default="local", help="local | cloud | auto (auto mezcla procedencia)")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--forzar", action="store_true", help="rehace tambien lo ya cacheado")
    parser.add_argument("--todas", action="store_true",
                        help="precalienta el corpus entero, no solo lo que la capa de texto no resuelve")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--conexion", type=float, default=10.0)
    args = parser.parse_args()

    # El motor resuelve la URL con `_ocr_url()` (variable de entorno primero) en
    # TODAS sus llamadas, incluidas `firma_motor`/`firma_nube`. Se fija aqui, que
    # es antes de cualquier consulta, para que `--ocr` apunte de verdad a donde
    # dice: si no, las firmas saldrian del servicio por defecto y la cache
    # quedaria estampada por un motor distinto del que leyo.
    os.environ["MAISA_OCR_URL"] = args.ocr

    directorio = Path(args.dir)
    cache = Path(args.cache)
    todos = sorted(p for p in directorio.iterdir() if p.suffix.lower() == ".pdf")
    if not todos:
        print(f"No hay PDFs en {directorio}")
        return 1

    # Por defecto solo lo que el motor necesita: los documentos que resuelve la
    # capa de texto no pasan de ese peldano, asi que su entrada de cache no se
    # lee nunca. Con `--todas` se precalienta el corpus entero, que es lo que
    # pide una evaluacion que quiera comparar motores sobre los 500.
    ficheros = todos
    if not args.todas:
        ficheros = [p for p in todos if calidad_texto(*capa_texto(p)) < UMBRAL_CALIDAD]

    firma, firma_n = firma_motor(3.0), firma_nube(3.0)
    if not firma and not firma_n:
        print(f"El servicio de vision no responde en {args.ocr}. Levantalo antes de precalentar.")
        return 1

    if args.engine == "local" and not firma:
        print(f"El motor local no responde en {args.ocr}. Precalentar con --engine local lo necesita.")
        return 1
    if args.engine == "cloud" and not firma_n:
        print("La nube no responde; no hay firma de nube con la que estampar.")
        return 1
    if args.engine == "auto":
        print("AVISO: --engine auto deja el corpus MIXTO (unas paginas en local, otras en nube).")
        print("       El motor apaga la nube por diseno: para su cache usa --engine local.")

    if cache == CACHE_OCR:
        print("AVISO: la cache del motor esta versionada en git y la entrega se firma con ella.")
        if args.forzar:
            print("       Con --forzar se reescribe entera. Medido sobre este corpus: 9 de los 29")
            print("       textos cambian (los commiteados salieron de un modelo anterior), y eso")
            print("       mueve la huella del lote. Usa --cache a un directorio temporal si solo")
            print("       quieres medir, y no commitees la cache sin volver a validar la entrega.")
        else:
            print("       Sin --forzar no se toca: lo que ya vale se salta.")

    print(f"corpus:   {directorio}")
    print(f"cache:    {cache}")
    print(f"engine:   {args.engine}")
    print(f"local:    {firma or '(sin firma)'}")
    print(f"nube:     {firma_n or '(sin firma)'}")
    if not args.todas:
        print(f"alcance:  solo los que la capa de texto no resuelve ({len(ficheros)} de {len(todos)})")
    print(f"ficheros: {len(ficheros)}, concurrencia {args.concurrency}")
    print()
    if not ficheros:
        print("Nada que precalentar: la capa de texto resuelve todo el corpus.")
        return 0

    t0 = time.monotonic()
    cuenta = {"ok": 0, "vacio": 0, "saltada": 0, "nube": 0, "fallo": 0}
    fallos: list[tuple[str, str]] = []

    def trabajo(ruta: Path) -> tuple[str, str]:
        try:
            return precalienta_una(
                ruta, args.engine, cache, args.forzar, args.timeout, args.conexion, firma, firma_n
            ), ruta.name
        except OcrNoDisponible as exc:
            return "fallo", f"{ruta.name}: {exc}"
        except Exception as exc:  # noqa: BLE001 - un fichero raro no aborta el lote
            return "fallo", f"{ruta.name}: {type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        for i, (estado, detalle) in enumerate(pool.map(trabajo, ficheros), 1):
            cuenta[estado] += 1
            if estado == "fallo":
                fallos.append((detalle.split(":")[0], detalle))
            if i % 25 == 0 or i == len(ficheros):
                s = time.monotonic() - t0
                print(f"  {i}/{len(ficheros)}  {s:.0f} s  ({s / i:.1f} s/fichero)")

    s = time.monotonic() - t0
    print()
    print(f"listo: {len(ficheros)} ficheros en {s:.0f} s")
    print(f"  leidos    {cuenta['ok']}")
    print(f"  en nube   {cuenta['nube']}")
    print(f"  saltados  {cuenta['saltada']} (ya estaban en cache)")
    print(f"  sin texto {cuenta['vacio']}")
    print(f"  fallos    {cuenta['fallo']}")
    for _, detalle in fallos[:20]:
        print(f"      {detalle}")
    return 0 if not cuenta["fallo"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
