"""Normalizacion determinista de identificadores, fechas e importes.

El mundo de Maisa mezcla convenciones: el ERP habla ISO-8859-1 con fechas
DD/MM/AAAA e importes ``12.874,40``; algunas facturas usan formato ingles
(``EUR 930.20``) y otras el espanol. Todo lo que llega al motor de decision
pasa por aqui, y aqui no se adivina: se convierte o se devuelve ``None``.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

_NO_ALFA = re.compile(r"[^0-9A-Za-z]")
_RE_MILES_ES = re.compile(r"^\d{1,3}(?:\.\d{3})+$")

_MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

# El destinatario es siempre el mismo cliente; su CIF nunca es el del emisor.
CIF_CLIENTE = "A58231074"


def _limpia_importe(valor: object) -> str:
    s = unicodedata.normalize("NFKC", str(valor))
    s = re.sub(r"(?i)\b(?:eur|euros?|usd)\b", " ", s)
    s = s.replace("\u20ac", " ").replace("$", " ")
    s = s.replace("\u00a0", " ").replace(" ", "")
    return re.sub(r"[^0-9.,\-]", "", s)


def a_decimal(valor: object) -> Decimal | None:
    """``'12.874,40'`` -> ``Decimal('12874.40')``; ``'930.20'`` -> ``Decimal('930.20')``.

    Devuelve ``None`` si no hay un numero reconocible. Nunca lanza.
    """
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, Decimal):
        return valor
    if isinstance(valor, (int, float)):
        return Decimal(str(valor))
    s = _limpia_importe(valor)
    if not s or not re.search(r"\d", s):
        return None
    negativo = s.startswith("-")
    s = s.lstrip("-+")
    if "," in s and "." in s:
        # El separador decimal es el que aparece mas a la derecha.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        partes = s.split(",")
        if len(partes) == 2 and len(partes[1]) in (1, 2):
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "." in s and _RE_MILES_ES.match(s):
        s = s.replace(".", "")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return -d if negativo else d


def cuantiza(valor: Decimal | None) -> Decimal | None:
    """Redondea a centimos con ROUND_HALF_UP (no el banquero de ``round``)."""
    if valor is None:
        return None
    return valor.quantize(Decimal("0.01"))


_NIF_COMPLETO = re.compile(r"^([A-Z]\d{7}[0-9A-Z]|\d{8}[A-Z])")


def norm_nif(valor: object) -> str:
    """NIF/CIF en mayusculas sin separadores. ``'b-46102331'`` -> ``'B46102331'``.

    Se corta a la forma canonica (9 caracteres): un lector goloso puede
    arrastrar la inicial de la palabra siguiente (``'A58231074S'``) y ese
    ruido rompe la comparacion con el maestro.
    """
    if valor is None:
        return ""
    s = unicodedata.normalize("NFKC", str(valor)).upper()
    s = _NO_ALFA.sub("", s)
    m = _NIF_COMPLETO.match(s)
    return m.group(1) if m else s


_IBAN_LARGO = {"ES": 24}


def norm_iban(valor: object) -> str:
    """IBAN sin espacios ni guiones, en mayusculas, cortado a su longitud legal.

    Los IBAN espanoles miden 24 caracteres; sin el corte el patron de lectura
    se come la palabra siguiente (``...2211CLIENTE``) y el cotejo falla.
    """
    if valor is None:
        return ""
    s = unicodedata.normalize("NFKC", str(valor)).upper()
    s = re.sub(r"[^0-9A-Z]", "", s)
    largo = _IBAN_LARGO.get(s[:2])
    return s[:largo] if largo and len(s) > largo else s


# Confusiones tipicas de OCR en el tramo numerico de un identificador.
_CONFUSIONES = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1",
                              "Z": "2", "E": "3", "A": "4", "S": "5", "G": "6",
                              "T": "7", "B": "8", "P": "9"})


def repara_ocr(valor: str) -> str:
    """Canoniza confusiones de OCR en la parte no alfabetica de un codigo.

    El prefijo alfabetico se conserva intacto: si se tradujera, ``'B461O2331'``
    (NIF leido mal) se convertiria en ``'846102331'`` y la correccion seria
    peor que el error.
    """
    s = unicodedata.normalize("NFKC", str(valor)).upper()
    m = re.match(r"^([A-Z]+)(.*)$", s)
    if not m:
        return s.translate(_CONFUSIONES)
    return m.group(1) + m.group(2).translate(_CONFUSIONES)


def candidatos_importe(valor: object) -> list[Decimal]:
    """Reconstrucciones plausibles de un importe leido por OCR.

    El OCR se come o duplica el separador decimal (``52498`` por ``524.98``,
    ``1.240.84`` por ``1240.84``), pero **no inventa digitos**. Por eso se
    generan los valores que resultan de colocar el punto en cada posicion
    valida para dinero (entero, un decimal, dos decimales) y se deja que quien
    conoce el importe esperado elija. Si los digitos no coinciden, ningun
    candidato coincidira: un descuadre real nunca se repara.
    """
    digitos = re.sub(r"\D", "", str(valor))
    if not digitos:
        return []
    vistos: list[Decimal] = []
    for corte in (len(digitos), len(digitos) - 1, len(digitos) - 2):
        if corte <= 0:
            continue
        texto = digitos[:corte] + ("." + digitos[corte:] if corte < len(digitos) else "")
        try:
            d = Decimal(texto)
        except InvalidOperation:
            continue
        if d > 0 and d not in vistos:
            vistos.append(d)
    return vistos


def norm_pedido(valor: object) -> str:
    """``'po 2026 96'`` y ``'PO-2026-0096'`` colapsan a ``'PO-2026-0096'``.

    El anio se conserva tal cual lo diga el documento (aunque venga con un
    digito comido por el OCR): repararlo es cosa de quien conoce el ERP.
    """
    if valor is None:
        return ""
    s = unicodedata.normalize("NFKC", str(valor)).upper()
    s = re.sub(r"[^0-9A-Z]", "", s)
    m = re.search(r"PO(\d{3,4})(\d{1,4})$", s) or re.search(r"(\d{4})(\d{3,4})$", s)
    if m:
        return f"PO-{m.group(1)}-{int(m.group(2)):04d}"
    return s


def norm_nombre(valor: object) -> str:
    """Nombre de proveedor sin acentos, puntuacion ni sufijos societarios."""
    if valor is None:
        return ""
    s = unicodedata.normalize("NFKD", str(valor))
    s = "".join(c for c in s if not unicodedata.combining(c)).upper()
    s = re.sub(r"[^0-9A-Z]+", " ", s).strip()
    s = re.sub(r"\b(?:S L|S A|S C|SL|SA|SC|SOCIEDAD|LIMITADA|ANONIMA)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def a_fecha(valor: object) -> date | None:
    """Acepta ``DD/MM/AAAA``, ``DD-MM-AAAA``, ISO y ``17 de mayo de 2026``.

    Devuelve ``None`` para fechas imposibles (31/02/2026) o texto no-fecha.
    """
    if valor is None:
        return None
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    s = unicodedata.normalize("NFKC", str(valor)).strip().lower()
    if not s:
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})", s)
    if m:
        dia, mes, anio = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if anio < 100:
            anio += 2000
        try:
            return date(anio, mes, dia)
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})\s+de\s+([a-z]+)\s+de\s+(\d{4})", s)
    if m and m.group(2) in _MESES:
        try:
            return date(int(m.group(3)), _MESES[m.group(2)], int(m.group(1)))
        except ValueError:
            return None
    return None


def similitud(a: str, b: str) -> float:
    """Similitud 0..1 por distancia de edicion normalizada (stdlib, sin deps)."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    fila = list(range(lb + 1))
    for i in range(1, la + 1):
        anterior, fila[0] = fila[0], i
        for j in range(1, lb + 1):
            actual = fila[j]
            coste = 0 if a[i - 1] == b[j - 1] else 1
            fila[j] = min(fila[j] + 1, fila[j - 1] + 1, anterior + coste)
            anterior = actual
    return 1.0 - fila[lb] / max(la, lb)


def mejor_match(valor: str, vocabulario: list[str], umbral: float = 0.82) -> tuple[str, float] | None:
    """Empareja un valor posiblemente corrupto contra un vocabulario cerrado.

    Es la correccion de OCR que no adivina: si el parecido no llega al umbral,
    se devuelve ``None`` y la factura acaba escalada en lugar de inventada.
    """
    if not valor:
        return None
    mejor: tuple[str, float] | None = None
    for candidato in vocabulario:
        s = similitud(valor, candidato)
        if s >= umbral and (mejor is None or s > mejor[1]):
            mejor = (candidato, s)
    return mejor


def match_estricto(valor: str, vocabulario: list[str]) -> tuple[str, str] | None:
    """Empareja sin adivinar: coincidencia exacta o coincidencia tras reparar
    confusiones de OCR.

    Devuelve ``(candidato, via)`` con ``via`` en ``exacto`` o ``reparado``.

    Es deliberadamente mas conservador que :func:`mejor_match`. En codigos
    con tramos numericos cortos (``PO-2026-0806`` vs ``PO-2026-0006``) la
    distancia de edicion vale 0,92 y una correccion difusa convierte un
    pedido **inexistente** en uno real: exactamente el error que la norma
    prohibe cometer. La reparacion de confusiones de OCR solo toca caracteres
    que un OCR realmente confunde, nunca un digito por otro digito.
    """
    if not valor:
        return None
    if valor in vocabulario:
        return (valor, "exacto")
    reparado = repara_ocr(valor)
    if reparado != valor and reparado in vocabulario:
        return (reparado, "reparado")
    return None


def match_seguro(valor: str, vocabulario: list[str], umbral: float) -> tuple[str, str] | None:
    """Como :func:`match_estricto` y ademas admite una correccion difusa, pero
    solo entre cadenas de **la misma longitud**.

    La longitud fija es lo que hace segura la correccion en identificadores de
    formato rigido: un IBAN espanol mide siempre 24 caracteres y un NIF 9, asi
    que una sustitucion de caracter es un error de lectura y no una cuenta o
    una empresa distinta.
    """
    exacto = match_estricto(valor, vocabulario)
    if exacto:
        return exacto
    mejor: tuple[str, str] | None = None
    for candidato in vocabulario:
        if len(candidato) != len(valor):
            continue
        s = similitud(valor, candidato)
        if s >= umbral and (mejor is None or s > similitud(valor, mejor[0])):
            mejor = (candidato, f"difuso {s:.2f}")
    return mejor
