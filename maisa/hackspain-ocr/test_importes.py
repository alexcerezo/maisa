"""Regresion del parseo de importes: el punto no siempre es separador de miles."""

import sys
from decimal import InvalidOperation
from pathlib import Path

# `descargar_erp` vive junto a este test. La ruta se DERIVA de __file__ en
# tiempo de ejecucion, nunca se escribe a mano: asi el test funciona lo copie
# quien lo copie, lo ejecute desde el directorio que sea y sin depender de que
# el lanzador anada el directorio del script al sys.path (con `python -P` o
# PYTHONSAFEPATH=1 no lo hace, y el import fallaria).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from descargar_erp import importe_a_decimal  # noqa: E402  (tras el bootstrap)

CASOS = {
    "12.874,40": "12874.40",   # espanol con miles
    "2480,50": "2480.50",      # espanol sin miles
    "473.70": "473.70",        # ISO de 2 decimales: NO son 47370
    "0.5": "0.5",              # ISO de 1 decimal
    "1.234": "1234",           # miles: 3 cifras tras el punto
    "1.234.567": "1234567",    # varios grupos de miles
    "0,00": "0.00",
    "100": "100",
    "-1.234,56": "-1234.56",
    "  2480,50  ": "2480.50",  # espacios de relleno
    # Con coma presente los puntos son SIEMPRE de miles, aunque el agrupamiento
    # no sea el canonico: se documenta para que nadie lo cambie sin querer.
    "1.2.3,45": "123.45",
}

BASURA = ("", "   ", "abc", "12,34,56")


def main() -> int:
    fallos = []
    for texto, esperado in CASOS.items():
        obtenido = str(importe_a_decimal(texto))
        if obtenido != esperado:
            fallos.append(f"{texto!r} -> {obtenido} (esperado {esperado})")
        print(f"  {'OK ' if obtenido == esperado else 'MAL'} {texto!r:>14} -> {obtenido:>10}")

    # Un importe ilegible tiene que fallar, no colarse como 0.
    for basura in BASURA:
        try:
            valor = importe_a_decimal(basura)
            fallos.append(f"{basura!r} deberia fallar, devolvio {valor}")
            print(f"  MAL {basura!r:>14} -> {valor} (deberia fallar)")
        except InvalidOperation:
            print(f"  OK  {basura!r:>14} -> InvalidOperation")

    print()
    print("importes:", "TODO OK" if not fallos else f"{len(fallos)} FALLO(S)")
    for fallo in fallos:
        print("   ", fallo)
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
