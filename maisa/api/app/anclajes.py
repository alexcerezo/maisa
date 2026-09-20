"""Anclajes: donde esta dentro del PDF cada dato que el motor leyo.

El visor ensena el documento al lado de la decision, pero hasta ahora habia que
buscar a ojo el `TOTAL` o el NIF en la pagina. Esto devuelve **donde** esta cada
campo para que el panel lo pueda resaltar.

Hay dos caminos, y son distintos de verdad, no dos formas de decir lo mismo:

  * **Capa de texto** (471 de las 500). El PDF trae el texto de verdad y
    `pdf.js` sabe dar la caja de cada fragmento al pintarlo. Aqui **no** se
    calcula geometria: se devuelven `tokens` (el valor ya normalizado, listo
    para buscar) y el navegador resuelve el rectangulo con el mismo motor que
    dibuja la pagina. Calcularlo aqui con `pypdf` daria cajas que no cuadran con
    el render: dos extractores de texto no parten las lineas igual.
  * **OCR** (29 escaneadas). No hay capa de texto que buscar, asi que la
    geometria viene de la cache del motor, que guarda la caja de cada linea
    reconocida. Aqui si se resuelve en el servidor y se devuelven `anclas`
    listas, en puntos del PDF.

Por eso un campo puede traer `tokens` y `anclas` a la vez: el cliente usa las
anclas si las hay y, si no, busca los tokens.

**Normalizar y no comparar literal.** El motor guarda el valor canonico
(`3012.89`, `ES2100491500051234567890`) y el PDF escribe el suyo
(`3.012,89 EUR`, `ES21 0049 1500 0512 3456 7890`). Comparar cadenas no
encontraria nada; se comparan los dos lados **reducidos a alfanumericos**, que
es lo que hace que `3.012,89` y `3012.89` sean el mismo token.

**Un resaltado equivocado es peor que ninguno.** Por eso un token corto no se
usa (el tipo de IVA, `21`, casaria con medio documento) y cada ancla dice su
`confianza`, mas alta cuando la linea que casa trae ademas la etiqueta del campo
(`TOTAL`, `NIF`). El panel puede distinguir "esto es el total" de "esto parece
el total".
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable

# Campos que **estan en el documento**. Los que vienen del ERP (`asiento`,
# `importe_erp`, `estado_erp`, `nif_maestro`...) se quedan fuera a proposito: el
# PDF no los trae, asi que buscar su valor en la pagina solo puede dar un falso
# positivo.
#
# Tupla por campo: (clave, etiqueta, de donde sale el valor, pistas). `fuentes`
# se mira en orden y gana la primera que exista: la lectura del documento manda
# sobre el valor ya conciliado, porque es la que esta escrita en el papel.
CAMPOS: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("pedido", "Pedido", ("pedido_candidatos", "pedido"), ("pedido", "po", "ref")),
    ("nif", "NIF", ("nif_candidatos",), ("nif", "cif")),
    ("iban", "IBAN", ("iban_candidatos",), ("iban", "cuenta", "abono")),
    ("fecha", "Fecha", ("fecha_candidatos", "fecha"), ("fecha", "emision")),
    ("base", "Base imponible", ("base",), ("base", "imponible", "subtotal")),
    ("iva", "Cuota de IVA", ("iva",), ("iva", "cuota")),
    ("total", "Total", ("total",), ("total",)),
)

# Por debajo de esto un token no identifica nada: `21` (el tipo de IVA) sale en
# medio documento y `1` en todos. Es preferible no resaltar a resaltar mal.
MIN_TOKEN = 4

# A partir de aqui se admite coincidencia aproximada. Con tokens cortos una
# errata los convierte en cualquier cosa, asi que la tolerancia empieza donde el
# token ya identifica por si solo.
MIN_TOKEN_APROX = 6
# Cuantas coincidencias se devuelven por campo. Un NIF sale dos veces (cabecera
# y pie); a partir de ahi es ruido y engorda la respuesta sin anadir nada.
MAX_ANCLAS = 4

# Un token que casa en una linea con la etiqueta del campo es el dato; uno que
# casa suelto puede ser otra cosa con la misma forma; y uno que casa con erratas
# es una pista que el usuario tiene que confirmar mirando.
CONFIANZA_CON_PISTA = 0.95
CONFIANZA_SIN_PISTA = 0.7
CONFIANZA_APROXIMADA = 0.5

_RE_NO_ALFA = re.compile(r"[^0-9a-z]+")
_RE_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

# Meses en espanol. Parte del corpus escribe la fecha en largo ("15 de enero de
# 2026") y sin esto esos documentos se quedaban sin resaltar la fecha: el token
# numerico no aparece por ningun lado. Se incluyen las abreviaturas porque
# "15 ene 2026" tambien sale.
MESES: dict[int, tuple[str, str]] = {
    1: ("enero", "ene"),
    2: ("febrero", "feb"),
    3: ("marzo", "mar"),
    4: ("abril", "abr"),
    5: ("mayo", "may"),
    6: ("junio", "jun"),
    7: ("julio", "jul"),
    8: ("agosto", "ago"),
    9: ("septiembre", "sep"),
    10: ("octubre", "oct"),
    11: ("noviembre", "nov"),
    12: ("diciembre", "dic"),
}


def normaliza(texto: str) -> str:
    """Reduce a alfanumericos en minusculas, sin acentos ni separadores.

    Es la operacion que hace comparables el valor del motor y el texto del PDF.
    Se quitan los acentos porque el OCR los pierde con frecuencia (`Factura` /
    `Factura` sin tilde) y quedarse con ellos rompe la comparacion.
    """
    plano = unicodedata.normalize("NFKD", str(texto))
    sin_tildes = "".join(c for c in plano if not unicodedata.combining(c))
    return _RE_NO_ALFA.sub("", sin_tildes.lower())


def _tokens(campo: str, valor: Any) -> list[str]:
    """Tokens con los que buscar ese campo dentro del texto del PDF.

    La fecha es el unico campo que necesita mas de un token: el motor la guarda
    en ISO (`2026-01-08`) y el documento la escribe en espanol (`08/01/2026`),
    asi que hay que probar las dos formas. Lo demas es un unico token
    normalizado, porque los separadores ya se han ido.
    """
    if campo == "fecha":
        return _tokens_fecha(valor)
    token = normaliza(valor)
    return [token] if len(token) >= MIN_TOKEN else []


def _tokens_fecha(valor: Any) -> list[str]:
    """Las formas en que un documento escribe una fecha, normalizadas.

    Se generan las cuatro combinaciones de cero/no-cero en dia y mes porque
    `08/01/2026`, `8/1/2026` y `8/01/2026` son la misma fecha y las tres salen
    en el corpus. `yyyymmdd` cubre a quien la escribe en ISO y las formas con el
    nombre del mes cubren a quien la escribe en largo.
    """
    coincidencia = _RE_ISO.match(str(valor).strip())
    if not coincidencia:
        token = normaliza(valor)
        return [token] if len(token) >= MIN_TOKEN else []
    anio, mes, dia = coincidencia.groups()
    numero_mes = int(mes)
    formas = {
        f"{anio}{mes}{dia}",
        f"{dia}{mes}{anio}",
        f"{int(dia)}{mes}{anio}",
        f"{dia}{int(mes)}{anio}",
        f"{int(dia)}{int(mes)}{anio}",
    }
    largo, corto = MESES.get(numero_mes, ("", ""))
    if largo:
        formas.add(f"{int(dia)}de{largo}de{anio}")
        formas.add(f"{dia}de{largo}de{anio}")
        formas.add(f"{int(dia)}de{corto}de{anio}")
    return sorted(forma for forma in formas if len(forma) >= MIN_TOKEN)


def _valor(campos: dict, fuentes: Iterable[str]) -> str | None:
    """Primer valor util de esas claves. Las listas de candidatos dan el primero."""
    for clave in fuentes:
        valor = campos.get(clave)
        if isinstance(valor, list):
            valor = next((v for v in valor if isinstance(v, str) and v.strip()), None)
        if isinstance(valor, (str, int, float)) and not isinstance(valor, bool):
            texto = str(valor).strip()
            if texto:
                return texto
    return None


def _pagina_publica(pagina: dict) -> dict | None:
    """Pagina de la cache del motor -> medidas en puntos del PDF.

    Las cajas del OCR estan en **pixeles del bitmap** que se renderizo (hasta
    288 dpi). El navegador pinta la pagina en puntos, asi que se divide por la
    escala con la que se genero. Sin `escala` no hay conversion posible y la
    pagina se descarta: es mejor no ofrecer geometria que ofrecerla mal.
    """
    escala = pagina.get("escala")
    if not isinstance(escala, (int, float)) or float(escala) <= 0:
        return None
    indice = pagina.get("pagina")
    if not isinstance(indice, int) or indice < 0:
        return None
    salida: dict[str, Any] = {"pagina": indice}
    for clave, destino in (("ancho", "ancho"), ("alto", "alto")):
        valor = pagina.get(clave)
        if isinstance(valor, (int, float)) and float(valor) > 0:
            salida[destino] = round(float(valor) / float(escala), 3)
    return salida


def _lineas(pagina: dict) -> list[dict]:
    """Lineas de una pagina con su caja ya en puntos del PDF."""
    escala = float(pagina["escala"])
    lineas: list[dict] = []
    for linea in pagina.get("lineas") or []:
        if not isinstance(linea, dict):
            continue
        caja = linea.get("caja")
        texto = linea.get("texto")
        if not isinstance(caja, list) or len(caja) != 4 or not isinstance(texto, str):
            continue
        try:
            x0, y0, x1, y1 = (float(v) / escala for v in caja)
        except (TypeError, ValueError):
            continue
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        lineas.append({"texto": texto, "plano": normaliza(texto), "bbox": [x0, y0, x1, y1]})
    return lineas


def _bloques(token: str, cuantos: int) -> list[tuple[str, int]]:
    """Trocea el token en bloques contiguos con su posicion.

    Es la mitad barata de la busqueda aproximada y se apoya en el principio del
    palomar: si el token aparece en el texto con a lo sumo `cuantos - 1` errores,
    **alguno** de los `cuantos` bloques tiene que aparecer intacto. Buscar un
    bloque exacto es un `find` y descarta casi todo sin calcular distancias.
    """
    largo = max(1, len(token) // cuantos)
    bloques: list[tuple[str, int]] = []
    for indice in range(cuantos):
        inicio = indice * largo
        fin = len(token) if indice == cuantos - 1 else inicio + largo
        if fin > inicio:
            bloques.append((token[inicio:fin], inicio))
    return bloques


def _distancia(a: str, b: str, tope: int) -> int | None:
    """Distancia de edicion entre `a` y `b`, o `None` si pasa de `tope`.

    Se corta en cuanto una fila entera supera el tope, que es lo que hace que
    comparar ventanas de 24 caracteres contra un IBAN salga barato.
    """
    if abs(len(a) - len(b)) > tope:
        return None
    fila = list(range(len(b) + 1))
    for posicion, caracter_a in enumerate(a, 1):
        nueva = [posicion]
        minimo = posicion
        for columna, caracter_b in enumerate(b, 1):
            valor = min(
                fila[columna] + 1,
                nueva[columna - 1] + 1,
                fila[columna - 1] + (0 if caracter_a == caracter_b else 1),
            )
            nueva.append(valor)
            if valor < minimo:
                minimo = valor
        if minimo > tope:
            return None
        fila = nueva
    return fila[-1] if fila[-1] <= tope else None


def _casa(plano: str, token: str) -> int | None:
    """Cuantos caracteres fallan en la mejor coincidencia, o `None` si no hay.

    Devuelve `0` para coincidencia exacta (subcadena) y un numero pequeno para
    coincidencia aproximada. Existe porque el OCR local lee mal: `B90233414`
    sale como `B9023341` y `PO-2026-0480` como `PD-2026-0480`. Exigir
    coincidencia exacta dejaria sin resaltar justo las facturas escaneadas, que
    son las que mas lo necesitan; y resaltar la linea que difiere en un caracter
    sigue siendo util, porque la caja es la linea entera.

    Se admite un fallo por cada 12 caracteres del token. La busqueda va por
    bloques exactos (barato) y solo verifica con distancia de edicion las
    ventanas que pasan ese filtro, probando desplazamientos de -1, 0 y +1
    caracteres y anchos de `n-1`, `n` y `n+1`: asi entra tanto la letra que el
    OCR cambio como la que se comio o se invento, en cualquier posicion.
    """
    if not plano:
        return None
    if token in plano:
        return 0
    if len(token) < MIN_TOKEN_APROX:
        return None
    permitidos = 1 if len(token) < 12 else 2
    mejor: int | None = None
    for bloque, posicion_bloque in _bloques(token, permitidos + 1):
        if not bloque:
            continue
        encontrado = plano.find(bloque)
        while encontrado != -1:
            for desplazamiento in (-1, 0, 1):
                inicio = encontrado - posicion_bloque + desplazamiento
                if inicio < 0:
                    continue
                for ancho in (len(token) - 1, len(token), len(token) + 1):
                    if ancho <= 0 or inicio + ancho > len(plano):
                        continue
                    fallos = _distancia(token, plano[inicio : inicio + ancho], permitidos)
                    if fallos is None:
                        continue
                    if mejor is None or fallos < mejor:
                        mejor = fallos
                        if mejor == 0:
                            return 0
            encontrado = plano.find(bloque, encontrado + 1)
    return mejor


def _busca(tokens: list[str], pistas: tuple[str, ...], geo: list[dict]) -> list[dict]:
    """Anclas de un campo: la mejor linea que casa, en todas las paginas.

    Gana el token con mejor confianza, no el primero de la lista: si la forma
    ISO de la fecha no aparece pero la espanola si, el resultado tiene que ser
    esa. Dentro de un mismo token se devuelven todas las coincidencias (hasta
    `MAX_ANCLAS`), porque un dato repetido en cabecera y pie se comprueba mejor
    viendolo en los dos sitios.
    """
    mejor: list[dict] = []
    mejor_confianza = 0.0
    for token in tokens:
        encontradas: list[dict] = []
        confianza = 0.0
        for pagina in geo:
            for linea in _lineas(pagina):
                fallos = _casa(linea["plano"], token)
                if fallos is None:
                    continue
                con_pista = any(pista in linea["plano"] for pista in pistas)
                if fallos:
                    valor = CONFIANZA_APROXIMADA
                elif con_pista:
                    valor = CONFIANZA_CON_PISTA
                else:
                    valor = CONFIANZA_SIN_PISTA
                confianza = max(confianza, valor)
                encontradas.append(
                    {
                        "pagina": pagina["pagina"],
                        "bbox": [round(v, 3) for v in linea["bbox"]],
                        "texto": linea["texto"],
                        "confianza": valor,
                        "aproximado": bool(fallos),
                    }
                )
                if len(encontradas) >= MAX_ANCLAS:
                    break
            if len(encontradas) >= MAX_ANCLAS:
                break
        if encontradas and confianza > mejor_confianza:
            mejor, mejor_confianza = encontradas, confianza
    return mejor


def valores_del_motor(registro: dict) -> dict[str, str | None]:
    """Lo que el motor leyo para cada campo corregible, o `None` si no lo leyo.

    Se calcula **desde la traza** en vez de guardarse junto a la correccion: la
    traza es la fuente de verdad y una copia en Mongo se quedaria obsoleta en
    cuanto el lote se reproduzca. El panel lo usa para enseñar el contraste
    ("el motor no lo leyo" / "el motor leyo esto y el operador dice aquello").
    """
    campos = registro.get("campos") or {}
    return {campo: _valor(campos, fuentes) for campo, _etiqueta, fuentes, _pistas in CAMPOS}


def construir(registro: dict, geo: list[dict] | None) -> dict:
    """Respuesta de `/anclajes` para una factura de la traza.

    `geo` es la geometria de la cache del motor, o `None` si esa factura no paso
    por OCR (las 471 con capa de texto) o si su entrada es anterior a que la
    cache guardara cajas. En el primer caso el cliente resuelve con `tokens`; en
    el segundo no hay nada que resolver y se dice, en vez de devolver una lista
    vacia que parece "este documento no tiene datos".
    """
    campos = registro.get("campos") or {}
    escalon = registro.get("escalon_lectura")
    es_ocr = escalon == "cache_ocr"
    paginas = [p for p in (_pagina_publica(cada) for cada in (geo or [])) if p is not None]

    salida: list[dict] = []
    for campo, etiqueta, fuentes, pistas in CAMPOS:
        valor = _valor(campos, fuentes)
        if valor is None:
            continue
        tokens = _tokens(campo, valor)
        if not tokens:
            continue
        salida.append(
            {
                "campo": campo,
                "etiqueta": etiqueta,
                "valor": valor,
                "tokens": tokens,
                "pistas": list(pistas),
                "anclas": _busca(tokens, pistas, geo or []) if paginas else [],
            }
        )

    aviso: str | None = None
    if es_ocr and not paginas:
        aviso = (
            "Esta factura se leyo con OCR pero su cache no guarda coordenadas: "
            "no se puede resaltar el dato dentro del documento."
        )

    return {
        "file_id": registro.get("file_id"),
        "sha256": registro.get("sha256"),
        "origen": "ocr" if es_ocr else "capa_texto",
        "paginas": paginas,
        "campos": salida,
        "aviso": aviso,
    }


class CacheGeo:
    """Geometria de OCR leida de la cache del motor, memorizada por fichero.

    La cache es inmutable una vez escrita (su nombre es el `sha256` del PDF), asi
    que se puede guardar en memoria. Se comprueba `mtime` y tamano de todas
    formas: si alguien regenera la cache con otro motor mientras la API corre, el
    proceso no debe seguir sirviendo la geometria vieja.
    """

    def __init__(self, carpeta: Path | None) -> None:
        self._carpeta = carpeta
        self._memoria: dict[str, tuple[int, int, list[dict]]] = {}

    @property
    def disponible(self) -> bool:
        return self._carpeta is not None and self._carpeta.is_dir()

    def ruta(self, sha256: str | None) -> Path | None:
        """Ruta del fichero de cache, o `None` si el sha no es utilizable.

        El sha llega de la traza, no del cliente, pero se valida igual: es un
        nombre de fichero construido con un dato y no puede salirse de la
        carpeta.
        """
        if not self.disponible or not sha256:
            return None
        limpio = sha256.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", limpio):
            return None
        assert self._carpeta is not None
        return self._carpeta / f"{limpio}.json"

    def leer(self, sha256: str | None) -> list[dict]:
        """Geometria de esa factura. Lista vacia si no hay o no es legible."""
        ruta = self.ruta(sha256)
        if ruta is None:
            return []
        try:
            estado = ruta.stat()
        except OSError:
            return []
        clave = str(ruta)
        guardado = self._memoria.get(clave)
        if guardado is not None and guardado[0] == estado.st_mtime_ns and guardado[1] == estado.st_size:
            return guardado[2]
        try:
            datos = json.loads(ruta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        geo = datos.get("geo") if isinstance(datos, dict) else None
        paginas = _sanea_geo(geo)
        self._memoria[clave] = (estado.st_mtime_ns, estado.st_size, paginas)
        return paginas


def _sanea_geo(crudo: Any) -> list[dict]:
    """Geometria de la cache, comprobada campo a campo.

    La API no puede fiarse de la forma del fichero: es material del motor y un
    cambio de formato no deberia convertirse en un 500 en el visor. Lo que no
    encaje se descarta y la factura se queda sin resaltado.
    """
    if not isinstance(crudo, list):
        return []
    paginas: list[dict] = []
    for pagina in crudo:
        if not isinstance(pagina, dict):
            continue
        escala = pagina.get("escala")
        indice = pagina.get("pagina")
        if not isinstance(escala, (int, float)) or float(escala) <= 0:
            continue
        if not isinstance(indice, int) or indice < 0:
            continue
        lineas = []
        for linea in pagina.get("lineas") or []:
            if not isinstance(linea, dict):
                continue
            caja = linea.get("caja")
            texto = linea.get("texto")
            if not isinstance(texto, str) or not texto.strip():
                continue
            if not isinstance(caja, list) or len(caja) != 4:
                continue
            try:
                caja = [float(v) for v in caja]
            except (TypeError, ValueError):
                continue
            lineas.append({"texto": texto, "caja": caja})
        if not lineas:
            continue
        saneada: dict[str, Any] = {"pagina": indice, "escala": float(escala), "lineas": lineas}
        for clave in ("ancho", "alto"):
            valor = pagina.get(clave)
            if isinstance(valor, (int, float)) and float(valor) > 0:
                saneada[clave] = float(valor)
        paginas.append(saneada)
    return paginas
