"""Convencion numerica espanola para el texto que sale del OCR.

Los dos motores devuelven de vez en cuando los importes con convencion inglesa
aunque el papel este en espanol: `1.240,84` sale como `1.240.84` y `2.229,30`
como `2.229.30`. Medido sobre las 29 facturas del corpus que pasan por OCR, la
nube lo hace en 13 de 29 documentos y el motor local en 9 de 29.

Importa porque `maisa.normaliza.a_decimal` espera convencion espanola: ante
`2.229.30` no ve un numero valido y devuelve `None`, de modo que el importe se
pierde; y ante `1.240.84` puede quedarse con los digitos pegados y contabilizar
`124084` en vez de `1240.84`, un error de dos ordenes de magnitud que la decision
no siempre detecta.

Se reescribe aqui, en la frontera del OCR, para que todo lo que consuma el texto
(el motor) vea un unico formato y nadie tenga que adivinar la convencion.
"""

from __future__ import annotations

import re

# El patron exige al menos un grupo de tres cifras y un final de exactamente dos,
# que en espanol no es un numero valido. Asi `1.234` (miles), `21.04.2026`
# (fecha), `192.168.1.1` (IP) y `1.234.567` (miles sin decimales) no encajan y
# no pueden estropearse. Los limites impiden morder un numero por el medio.
_EN_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+)(\.\d{2})(?![\d.,])")
_MIX_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+)(,\d{2})(?![\d.,])")


def a_convencion_es(texto: str) -> str:
    """Reescribe los importes en convencion inglesa a convencion espanola.

    Idempotente: el texto ya en espanol sale intacto.
    """
    if "." not in texto and "," not in texto:
        return texto
    texto = _EN_RE.sub(lambda m: f"{m.group(1)},{m.group(2)[1:]}", texto)
    return _MIX_RE.sub(
        lambda m: f"{m.group(1).replace(',', '.')},{m.group(2)[1:]}", texto
    )
