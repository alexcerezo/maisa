"""Audita y firma la cache de OCR del motor.

La cache de ``motor/.cache/ocr/`` esta versionada en git y **se sirve sin
reconocer el PDF**: ``_lee_cache`` devuelve el texto guardado si el ``sha256``
del fichero coincide, y a partir de ahi nadie vuelve a mirar el escaneo. Es lo
que hace que el lote de 500 corra sin el contenedor de OCR, y a la vez su punto
debil: quien pueda escribir un JSON en ese directorio decide que dice una
factura. La entrada no se distingue de una legitima porque el ``sha256`` que
lleva dentro lo copia cualquiera del PDF.

Desde que ``_escribe_cache`` estampa un ``hmac``, la entrada manipulada se
detecta: la firma se calcula sobre el contenido con la clave
``MAISA_CACHE_CLAVE``, que vive **fuera** de ``.cache/`` (variable de entorno, o
``maisa/.env``), asi que escribir el JSON no basta para que cuadre. Esta
herramienta es el otro lado: mira el directorio entero y dice cuantas entradas
van firmadas, cuantas no y cuantas no cuadran.

Uso, desde ``maisa/motor/``:

    ../../.venv/bin/python tools/firma_cache.py                 # audita
    ../../.venv/bin/python tools/firma_cache.py --json          # para CI
    ../../.venv/bin/python tools/firma_cache.py --firma         # firma las sueltas

**Sin clave no se firma nada y no se puede verificar nada**: la auditoria lo
declara (``sin_clave``) en vez de dar por buenas las entradas. Es el
comportamiento por defecto del repo, porque ``MAISA_CACHE_CLAVE`` no esta en
``.env.example`` con valor real; a partir de ahi, ``--firma`` deja las 30
entradas versionadas selladas de una vez.

**Las entradas sin sello no se rechazan.** Hay 30 commiteadas antes de que
existiera el campo, y un clon nuevo tiene que seguir reproduciendo el lote sin
clave ni servicio de OCR. Por eso la firma es un **detector**, no un portero: el
motor sigue leyendo lo que no esta firmado, pero ya puede decir cuanto de lo que
sirve nadie lo ha garantizado. ``--firma`` es el paso que convierte la cache
heredada en cache firmada, y ``--firma --verifica`` (por defecto) comprueba el
resultado en la misma pasada.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

from maisa import lectura  # noqa: E402 - necesita el sys.path de arriba

#: Un ``sin_firma`` no es un fallo (hay cache heredada legitima), pero un
#: ``manipuladas`` si: alguien reescribio una entrada sellada.
SALIDA_OK = 0
SALIDA_MANIPULADA = 1
SALIDA_SIN_CLAVE = 2


def firma_sueltas(directorio: Path) -> tuple[int, int]:
    """Estampa el sello en las entradas que no lo traen. Devuelve (firmadas, fallos).

    Se reescribe la entrada **con el mismo formato que ``_escribe_cache``**
    (JSON compacto en una linea, sin salto final, ``hmac`` al final), para que
    volver a pasar por aqui no cambie un byte y el diff de git sea el minimo.
    """
    firmadas = fallos = 0
    for ruta in sorted(directorio.glob("*.json")):
        try:
            crudo = ruta.read_text(encoding="utf-8")
            datos = json.loads(crudo)
        except (OSError, ValueError):
            fallos += 1
            continue
        if not isinstance(datos, dict):
            fallos += 1
            continue
        sello = lectura.firma_entrada(datos)
        if not sello or datos.get("hmac") == sello:
            continue
        datos["hmac"] = sello
        try:
            ruta.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
        except OSError:
            fallos += 1
            continue
        firmadas += 1
    return firmadas, fallos


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audita (y opcionalmente firma) la cache de OCR del motor."
    )
    parser.add_argument(
        "--cache", type=Path, default=lectura.CACHE_OCR, help="directorio de la cache"
    )
    parser.add_argument(
        "--firma", action="store_true", help="estampa el sello en las entradas que no lo traen"
    )
    parser.add_argument("--json", action="store_true", help="salida legible por maquina")
    args = parser.parse_args(argv)

    if not args.cache.is_dir():
        print(f"no existe el directorio de cache: {args.cache}", file=sys.stderr)
        return SALIDA_MANIPULADA

    clave = lectura._clave_cache() is not None
    firmadas = fallos = 0
    if args.firma:
        if not clave:
            print(
                f"no hay {lectura.CLAVE_CACHE_ENV}: no se puede firmar nada",
                file=sys.stderr,
            )
            return SALIDA_SIN_CLAVE
        firmadas, fallos = firma_sueltas(args.cache)

    informe = lectura.verifica_cache(args.cache)
    if args.json:
        print(json.dumps({**informe, "clave_configurada": clave, "firmadas_ahora": firmadas}))
    else:
        print(f"cache: {args.cache}")
        print(f"  clave {lectura.CLAVE_CACHE_ENV}: {'si' if clave else 'NO'}")
        print(f"  entradas            {informe['total']}")
        print(f"  firmadas y validas  {informe['firmadas_ok']}")
        print(f"  sin firma           {informe['sin_firma']}")
        print(f"  manipuladas         {informe['manipuladas']}")
        print(f"  ilegibles           {informe['ilegibles']}")
        if informe["sin_clave"]:
            print(f"  sin poder verificar {informe['sin_clave']} (hay sello, no hay clave)")
        if args.firma:
            print(f"  firmadas ahora      {firmadas}")
            if fallos:
                print(f"  fallos al firmar    {fallos}")

    if informe["manipuladas"]:
        print(
            "\nhay entradas con sello que NO cuadra: alguien reescribio la cache. "
            "Reconocelas con el OCR antes de publicar.",
            file=sys.stderr,
        )
        return SALIDA_MANIPULADA
    if informe["sin_clave"] and not clave:
        print(
            f"\nhay {informe['sin_clave']} entradas firmadas que no se pueden verificar: "
            f"falta {lectura.CLAVE_CACHE_ENV}.",
            file=sys.stderr,
        )
        return SALIDA_SIN_CLAVE
    if not clave:
        print(
            f"\nsin {lectura.CLAVE_CACHE_ENV} no se firma: las entradas se aceptan igual, "
            "pero nadie garantiza que sean las que escribio el OCR.",
            file=sys.stderr,
        )
    return SALIDA_OK


if __name__ == "__main__":
    raise SystemExit(main())
