"""Regresion de la convencion numerica del texto que sale del OCR.

Los dos motores devuelven de vez en cuando importes en convencion inglesa aunque
el papel este en espanol. Aqui se fija que la reescritura los corrige y, sobre
todo, que NO toca nada que ya estuviera bien: un falso positivo aqui cambiaria
un importe correcto por otro inventado.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.importes import a_convencion_es  # noqa: E402

# (entrada, esperado). Los dos primeros son los casos reales que motivaron el
# arreglo: `TOTAL1.240.84EUR` del motor local en scan_001 (se contabilizaba
# 124084) y `TOTAL 2.229.30 EUR` de la nube en scan_015.
CASOS = {
    "TOTAL1.240.84EUR": "TOTAL1.240,84EUR",
    "TOTAL 2.229.30 EUR": "TOTAL 2.229,30 EUR",
    "TOTAL 1.410.74 EUR": "TOTAL 1.410,74 EUR",
    "TOTAL 1,000,57 EUR": "TOTAL 1.000,57 EUR",
    "Base 1.533.53 IVA21%322.04": "Base 1.533,53 IVA21%322.04",
    "Total 1.234.567.89": "Total 1.234.567,89",
    "99.999.999.99": "99.999.999,99",
    "a 12.345.67 b 89.012.34": "a 12.345,67 b 89.012,34",
    "1.234.56": "1.234,56",
    "x1.234.56y": "x1.234,56y",
}

INTACTOS = (
    "TOTAL1.855,57EUR",  # ya en espanol
    "TOTAL 1.009,43 EUR",
    "Base 473.70",  # ISO de 2 decimales: no son 47370
    "0.5",
    "Importe 1.234",  # miles sin decimales
    "12.345.678",  # varios grupos de miles, sin parte decimal
    "Fecha 21.04.2026",
    "IP 192.168.1.1",
    "v1.2.3",
    "1,234.56",  # ingles completo: lo resuelve el motor, no esto
    "1.234.56.78",  # cuatro grupos: los limites del patron lo dejan en paz
    "sin numeros",
    "",
)


def test_corrige_convencion_inglesa() -> None:
    for texto, esperado in CASOS.items():
        assert a_convencion_es(texto) == esperado, texto


def test_no_toca_lo_que_ya_esta_bien() -> None:
    for texto in INTACTOS:
        assert a_convencion_es(texto) == texto, texto


def test_es_idempotente() -> None:
    # Se aplica en dos sitios (texto de pagina y lista de lineas son el mismo
    # objeto), asi que aplicarlo dos veces no puede cambiar el resultado.
    for texto in (*CASOS, *INTACTOS):
        una = a_convencion_es(texto)
        assert a_convencion_es(una) == una, texto
