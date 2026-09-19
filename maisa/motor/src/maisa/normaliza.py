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
    # Ingles (facturas con "Issue date: 03 Feb 2026").
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    # Catalan.
    "gener": 1, "febrer": 2, "marc": 3, "maig": 5, "juny": 6,
    "juliol": 7, "agost": 8, "setembre": 9, "novembre": 11,
    "desembre": 12,
    # Italiano.
    "gennaio": 1, "febbraio": 2, "aprile": 4, "maggio": 5, "giugno": 6,
    "luglio": 7, "settembre": 9, "ottobre": 10, "dicembre": 12,
    # Aleman.
    "januar": 1, "janner": 1, "februar": 2, "marz": 3, "mai": 5, "juni": 6,
    "juli": 7, "oktober": 10, "dezember": 12,
    # Frances.
    "janvier": 1, "fevrier": 2, "avril": 4, "juin": 6, "juillet": 7,
    "aout": 8, "decembre": 12,
    # Portugues.
    "janeiro": 1, "fevereiro": 2, "marco": 3, "maio": 5, "junho": 6, "julho": 7,
    "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}

# Numerales escritos con letras. El corpus del lote 2 trae fechas como
# "dos de enero de dos mil veintiseis", "sette agosto duemilaventisei" o
# "am siebten Marz zweitausendsechsundzwanzig": sin esto la fecha no se lee y
# la factura acaba escalada por un fallo de lectura, no por un hecho de negocio.
# Las claves van sin acentos y en minusculas; los choques entre idiomas son
# inofensivos porque casi siempre coinciden en valor ("seis" ES/PT = 6).
_NUMERALES = {
    "cero": 0, "zero": 0,
    "uno": 1, "un": 1, "una": 1, "eins": 1, "ein": 1, "eines": 1,     "deux": 2, "due": 2, "zwei": 2, "dos": 2, "dois": 2,
    "tres": 3, "tre": 3, "drei": 3, "trois": 3,
    "cuatro": 4, "quatre": 4, "vier": 4,
    "cinco": 5, "cinc": 5, "cinque": 5, "fünf": 5, "funf": 5,
    "seis": 6, "sis": 6, "sechs": 6, "sei": 6, "six": 6,
    "siete": 7, "set": 7, "sieben": 7, "sette": 7, "sept": 7, "siebten": 7,
    "ocho": 8, "vuit": 8, "acht": 8, "otto": 8, "huit": 8, "oito": 8,
    "nueve": 9, "nou": 9, "neun": 9, "nove": 9, "neuf": 9,
    "diez": 10, "deu": 10, "zehn": 10, "dieci": 10, "dix": 10, "dez": 10,
    "once": 11, "onze": 11, "elf": 11, "undici": 11, "doze": 12,
    "doce": 12, "dotze": 12, "zwolf": 12, "zwölf": 12, "dodici": 12, "douze": 12,
    "trece": 13, "tretze": 13, "dreizehn": 13, "tredici": 13, "treize": 13,
    "catorce": 14, "catorze": 14, "vierzehn": 14, "quattordici": 14, "quatorze": 14,
    "quince": 15, "quinze": 15, "fünfzehn": 15, "funfzehn": 15, "quindici": 15,
    "dieciseis": 16, "setze": 16, "sechzehn": 16, "sedici": 16, "seize": 16,
    "dezesseis": 16, "diciassette": 17, "disset": 17, "siebzehn": 17, "dix-sept": 17,
    "diecisiete": 17, "dezessete": 17, "dieciocho": 18, "divuit": 18, "achtzehn": 18,
    "diciotto": 18, "dezoito": 18, "diecinueve": 19, "dinou": 19, "neunzehn": 19,
    "diciannove": 19, "dezenove": 19, "veinte": 20, "vint": 20, "zwanzig": 20,
    "venti": 20, "vingt": 20, "vinte": 20,
    "veintiuno": 21, "veintiun": 21, "veintidos": 22, "veintitres": 23,
    "veinticuatro": 24, "veinticinco": 25, "veintiseis": 26, "veintisiete": 27,
    "veintiocho": 28, "veintinueve": 29,
    "ventuno": 21, "ventidue": 22, "ventitre": 23, "ventiquattro": 24,
    "venticinque": 25, "ventisei": 26, "ventisette": 27, "ventotto": 28,
    "ventinove": 29,
    "treinta": 30, "trenta": 30, "trente": 30, "dreissig": 30, "dreißig": 30,
    "trinta": 30, "cuarenta": 40, "quaranta": 40, "quarante": 40, "vierzig": 40,
    "quarenta": 40, "cincuenta": 50, "cinquanta": 50, "cinquante": 50,
    "fünfzig": 50, "funfzig": 50, "cinqüenta": 50, "cinquenta": 50,
    "sesenta": 60, "seixanta": 60, "soixante": 60, "sechzig": 60, "sessenta": 60,
    "setenta": 70, "setanta": 70, "siebzig": 70, "oitenta": 70,
    "ochenta": 80, "vuitanta": 80, "achtzig": 80, "quatre-vingts": 80,
    "noventa": 90, "noranta": 90, "neunzig": 90, "quatre-vingt-dix": 90,
    "cien": 100, "cent": 100, "cem": 100, "hundert": 100,
}
_NUMERALES_ORD = {
    "primero": 1, "primer": 1, "ersten": 1, "erste": 1, "premier": 1,
    "segundo": 2, "zweiten": 2, "second": 2,
    "tercero": 3, "dritten": 3, "troisieme": 3,
    "cuarto": 4, "vierten": 4, "quinto": 5, "fünften": 5, "funften": 5,
    "sexto": 6, "sechsten": 6, "septimo": 7, "achten": 8, "octavo": 8,
    "noveno": 9, "neunten": 9, "decimo": 10, "zehnten": 10,
}
_NUMERALES.update(_NUMERALES_ORD)

# Conectores que no aportan valor ("dos mil e vinte e seis", "vint-i-sis").
_CONECTORES_NUM = {
    "de", "del", "d", "di", "da", "do", "das", "y", "e", "et", "and",
    "und", "i", "el", "la", "le", "los", "las", "a", "al", "dia", "day", "ano",
    "año", "year", "mes", "month",
}
# Marcadores de millar de cada idioma ("dos mil", "duemila", "zweitausend").
_MILLARES = ("tausend", "mille", "mila", "mil")

_RE_MES = re.compile(
    r"\b(" + "|".join(sorted(_MESES, key=len, reverse=True)) + r")\b")


def _sin_acentos(texto: str) -> str:
    s = unicodedata.normalize("NFKD", texto.replace("ß", "ss"))
    return "".join(c for c in s if not unicodedata.combining(c))


def _num_simple(texto: str) -> int | None:
    """Suma los numerales de un fragmento ("veintiseis"=26, "sechsundzwanzig"=26)."""
    texto = texto.strip()
    if texto in _NUMERALES:
        return _NUMERALES[texto]
    total, visto = 0, False
    for parte in re.split(r"[\s\-]+|und", texto):
        if not parte or parte in _CONECTORES_NUM:
            continue
        valor = _NUMERALES.get(parte)
        if valor is None:
            return None
        total += valor
        visto = True
    return total if visto else None


def _num_letras(texto: str) -> int | None:
    """``'dos mil veintiseis'`` -> 2026, ``'zweitausendsechsundzwanzig'`` -> 2026."""
    t = _sin_acentos(texto).lower().strip(" .,:;")
    if not t:
        return None
    for marca in _MILLARES:
        if marca in t:
            izquierda, _, derecha = t.partition(marca)
            miles, resto = _num_simple(izquierda), _num_simple(derecha)
            if miles is None or resto is None:
                return None
            return miles * 1000 + resto
    return _num_simple(t)


def _dia_alrededor(texto: str, corte: int) -> int | None:
    """Dia de la fecha: ultimo numero o numeral que precede al nombre del mes."""
    cola = texto[max(0, corte - 48):corte]
    palabras = re.findall(r"[0-9A-Za-zÀ-ÿ'\-]+", cola)
    recogidas: list[str] = []
    for palabra in reversed(palabras):
        if palabra.lower() in _CONECTORES_NUM or palabra in ("'", "-"):
            continue
        if re.fullmatch(r"\d{1,2}", palabra):
            if not recogidas:
                return int(palabra)
            break
        if not re.fullmatch(r"[A-Za-zÀ-ÿ'\-]+", palabra):
            break
        if (palabra.lower() in _NUMERALES or palabra.lower() in _MILLARES
                or _num_letras(palabra) is not None):
            recogidas.append(palabra)
            continue
        break
    if not recogidas:
        return None
    return _num_letras(" ".join(reversed(recogidas)))


def _anio_tras_mes(texto: str, inicio: int) -> int | None:
    """Anio de la fecha: primer numero de cuatro cifras o numerales tras el mes."""
    cola = texto[inicio:inicio + 56]
    m = re.search(r"\b(\d{4})\b", cola)
    if m:
        return int(m.group(1))
    palabras = re.findall(r"[0-9A-Za-zÀ-ÿ'\-]+", cola)
    recogidas: list[str] = []
    for palabra in palabras:
        if palabra.lower() in _CONECTORES_NUM or palabra in ("'", "-"):
            continue
        if not re.fullmatch(r"[A-Za-zÀ-ÿ'\-]+", palabra):
            break
        if (palabra.lower() in _NUMERALES or palabra.lower() in _MILLARES
                or _num_letras(palabra) is not None):
            recogidas.append(palabra)
            continue
        break
    if not recogidas:
        return None
    return _num_letras(" ".join(recogidas))

# El destinatario es siempre el mismo cliente; su CIF nunca es el del emisor.
CIF_CLIENTE = "A58231074"


def _limpia_importe(valor: object) -> str:
    s = unicodedata.normalize("NFKC", str(valor))
    s = re.sub(r"(?i)\b(?:eur|euros?|usd|gbp|chf|jpy|mxn|brl|reales|reais|"
               r"dolares?|d[oó]lares?|libras?|francos?|yenes?|fr)\b\.?", " ", s)
    for simbolo in ("\u20ac", "$", "\u00a3", "\u00a5", "\u20b9"):
        s = s.replace(simbolo, " ")
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


_NIF_COMPLETO = re.compile(
    r"^([A-Z]\d{7}[0-9A-Z]"      # NIF/CIF espanol: letra + 7 digitos + letra/digito
    r"|\d{8}[A-Z]"               # NIF espanol: 8 digitos + letra
    r"|[A-Z]{2}\d{9,12}"         # IVA intracomunitario (DE/FR/IT/PT/NL)
    r"|\d{14}"                   # Brasil (CNPJ)
    r"|\d{13})")                 # Japon (numero de registro)


def norm_nif(valor: object) -> str:
    """NIF/CIF/VAT en mayusculas sin separadores. ``'b-46102331'`` -> ``'B46102331'``.

    Se corta a la forma canonica: un lector goloso puede arrastrar la inicial de
    la palabra siguiente (``'A58231074S'``) y ese ruido rompe la comparacion con
    el maestro. Ademas de los formatos espanoles se reconocen los del lote 2
    (``DE812345678``, ``FR40303265045``, ``5010401075570``, CNPJ braseno), cada
    uno con su longitud legal.
    """
    if valor is None:
        return ""
    s = unicodedata.normalize("NFKC", str(valor)).upper()
    s = _NO_ALFA.sub("", s)
    m = _NIF_COMPLETO.match(s)
    return m.group(1) if m else s


_IBAN_LARGO = {
    "ES": 24, "DE": 22, "FR": 27, "IT": 27, "PT": 25, "GB": 22, "NL": 18,
    "CH": 21, "BR": 29, "JP": 23,
}


def norm_iban(valor: object) -> str:
    """IBAN sin espacios ni guiones, en mayusculas, cortado a su longitud legal.

    Los IBAN espanoles miden 24 caracteres; sin el corte el patron de lectura
    se come la palabra siguiente (``...2211CLIENTE``) y el cotejo falla. Lo
    mismo pasa con las cuentas extranjeras del lote 2: el lector goloso
    arrastra la palabra siguiente al IBAN aleman (``DE89...1300`` + ``RECHNUNG``
    -> ``DE89370400440532013000RECH``) porque el salto de linea tambien es un
    separador valido dentro de un IBAN. El pais, por suerte, determina la
    longitud exacta, asi que el corte no necesita adivinar.
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
    """Acepta ``DD/MM/AAAA``, ``DD-MM-AAAA``, ISO y fechas con el mes en letras.

    El mes puede venir en castellano, ingles, catalan, italiano, aleman, frances
    o portugues, y el dia y el anio pueden estar escritos con letras
    (``dos de enero de dos mil veintiseis``). Devuelve ``None`` para fechas
    imposibles (31/02/2026) o texto que no es una fecha.
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
    return _fecha_con_mes_en_letras(s)


def _fecha_con_mes_en_letras(texto: str) -> date | None:
    """Fecha del tipo ``<dia> <mes> <anio>`` con cualquiera de las 6 lenguas.

    Se ancla en el nombre del mes, que es lo unico inequivoco; el dia se busca
    justo antes y el anio justo despues, admitiendo cifras o numerales.
    """
    plano = _sin_acentos(texto).lower()
    for mes in _RE_MES.finditer(plano):
        dia = _dia_alrededor(plano, mes.start())
        anio = _anio_tras_mes(plano, mes.end())
        if dia is None or anio is None:
            continue
        if anio < 100:
            anio += 2000
        try:
            return date(anio, _MESES[mes.group(1)], dia)
        except ValueError:
            continue
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
