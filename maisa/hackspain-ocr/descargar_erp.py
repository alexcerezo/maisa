#!/usr/bin/env python
"""Descarga completa del ERP Miralmar -> data/erp_snapshot.json.

Por que existe este script (y no solo `src/erp.rs`):

1.  `src/erp.rs` todavia no existe. Este script produce HOY el snapshot de los
    516 asientos, que es lo que desbloquea a `reconciler` y `rules`.
2.  Es la **segunda via de lectura** del ERP: lo lee por su cuenta, sin pasar
    por el motor, y sirve para contrastar el resultado de `outcomes.jsonl`
    (usa `--out` para volcarlo a otro fichero sin pisar el snapshot bueno).
3.  Es el plan B si el ERP se cae durante la demo.

Es deliberadamente **stdlib only** (sin dependencias). El ERP habla XML en
ISO-8859-1 y falla de tres maneras distintas *a proposito*: hay que reintentar
cada una por separado.

Los tres fallos del bridge (y por que hay que tratarlos distinto):

    ORA-00600  HTTP 500  cada 10a llamada AUTENTICADA, contador GLOBAL del
                         proceso. La sesion sigue viva -> reintentar la MISMA
                         pagina con backoff.
    SES-401    HTTP 401  token caducado (900 s), agotado (300 usos) o inexistente
                         -> volver a hacer login y reintentar.
    ERP-429    HTTP 429  rate limit: ventana deslizante de 1 s, >10 peticiones.
                         Llega ANTES del login (el portazo es lo primero que se
                         ejecuta en el servidor) -> esperar `Retry-After`.

Nota: el lote 1 tarda ~0.12 s por peticion en el servidor, asi que en serie se
queda en ~8 peticiones/s, por debajo del limite de 10. NO paralelizar: 10
hilos garantizan 429.

Salida (`data/erp_snapshot.json`): UN objeto JSON, no un array. Las claves de
arriba son metadatos del snapshot y la clave `asientos` contiene los documentos
(cada uno trae `_id`, `snapshot_id`, `clave_factura`, `vigente`,
`esquema_version`).

OJO: el fichero NO se puede volcar en Mongo tal cual, por tres motivos:
  - `mongoimport --jsonArray` exige un ARRAY en la raiz y aqui la raiz es un
    OBJETO; con `--file <snapshot>` importaria 1 documento, no los 516.
  - Falta `--db`: sin el, la importacion se iria a la base `test`.
  - El validador de `asientos` exige `fecha: date` e `importe: decimal`, y el
    snapshot los trae como `string` y `double` -> Mongo rechazaria los 516
    documentos con codigo 121.
De las tres se encarga `importar_asientos_mongo.py`:
    python importar_asientos_mongo.py --snapshot data/erp_snapshot.json

Identidad de la factura (`clave_factura`)
-----------------------------------------
    proveedor | pedido | fecha | importe     p.ej.  P010|PO-2026-0543|2026-06-11|473.70

El `asiento_id` del ERP identifica la FILA contable, no la factura. Y el NIF NO
sirve como clave, por tres motivos medidos sobre el lote 1:

  - falta en 20 de los 516 asientos (bloque AS-00499..AS-00518): no cubre el
    100% de los registros, asi que una clave por NIF deja ese bloque fuera;
  - `proveedor` y `nif` son 1:1 en los datos reales, luego el NIF no aporta
    capacidad discriminante alguna sobre el codigo de proveedor;
  - meterlo solo anadiria inestabilidad: un asiento que gane o pierda NIF
    cambiaria de clave.

Los cuatro componentes de arriba estan presentes en el 100% de los asientos y
son unicos sobre el lote 1 (516/516 sin colisiones). `pedido` por si solo
tambien es unico hoy, pero un pedido puede legalmente llevar varias facturas:
con `fecha` e `importe` la clave no colapsa facturas distintas.
(Comprobado: `nif+importe` SI colisiona en un caso real y las dos son facturas
distintas -> AS-70022 y AS-72010.)

Deduplicacion
-------------
Dos asientos con la misma `clave_factura` son la MISMA factura duplicada: se
conserva uno solo. El elegido es el de `asiento_id` mas bajo, porque el orden
de las paginas del ERP NO va por id y "el primero que llega" no seria
reproducible entre descargas.

Lo descartado no se pierde en silencio: queda registrado en `duplicados`
dentro de los metadatos del snapshot (esto es un sistema de conciliacion,
tirar filas sin traza no es aceptable). `--sin-dedup` conserva todos.

Uso:
    python descargar_erp.py                       # escribe data/erp_snapshot.json
    python descargar_erp.py --dry-run             # verifica sin escribir
    python descargar_erp.py --solo-pagina 1       # humo rapido
    python descargar_erp.py --sin-dedup           # conserva los duplicados

Configuracion (nada de rutas ni credenciales fijas en el codigo):
    ERP_BASE_URL   direccion del bridge      (por defecto http://127.0.0.1:8009)
    ERP_USUARIO    usuario del bridge        (por defecto "alberto")
    ERP_CLAVE      contrasena del bridge     (por defecto la del reto)

Los valores por defecto son los del bridge del reto, para que el programa
funcione recien clonado; en produccion se definen las variables (o se pasan
--base-url / --usuario / --clave). Las banderas mandan sobre el entorno. Todas
las rutas de fichero son relativas a la raiz del proyecto.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree as ET

# El ERP imprime '·' y acentos; la consola de Windows en cp850 revienta al
# volcar text del servidor. Forzamos UTF-8 en stdout/stderr.
for _flujo in (sys.stdout, sys.stderr):
    _reconfigurar = getattr(_flujo, "reconfigure", None)
    if callable(_reconfigurar):
        _reconfigurar(encoding="utf-8", errors="replace")

ESQUEMA_VERSION = 1

# --------------------------------------------------------------------------- #
# Configuracion: entorno primero, valores por defecto despues
# --------------------------------------------------------------------------- #
# Ninguna direccion de maquina ni credencial va fijada en el codigo: los tres
# parametros de conexion se pueden dar por variable de entorno (o por bandera
# de linea de comandos, que tiene prioridad sobre el entorno). Los valores por
# defecto son los del bridge del reto para que el programa funcione recien
# clonado en cualquier ordenador; en produccion se define `ERP_CLAVE` y no se
# depende de ningun valor por defecto (ver `aviso_de_credencial`).
VARIABLES_ENTORNO = {
    "usuario": "ERP_USUARIO",
    "clave": "ERP_CLAVE",
    "base_url": "ERP_BASE_URL",
}


def variable_de_entorno(nombre: str, defecto: str) -> str:
    """Valor de la variable `nombre`, o `defecto` si no esta o viene vacia."""
    return os.environ.get(nombre, "").strip() or defecto


USUARIO_DEFECTO = variable_de_entorno(VARIABLES_ENTORNO["usuario"], "alberto")
CLAVE_DEFECTO = variable_de_entorno(VARIABLES_ENTORNO["clave"], "FACTURAS2009")
BASE_DEFECTO = variable_de_entorno(VARIABLES_ENTORNO["base_url"],
                                   "http://127.0.0.1:8009")
POR_PAGINA_DEFECTO = 20


def aviso_de_credencial() -> str | None:
    """Aviso si se esta usando la credencial por defecto en vez del entorno.

    Devolver `None` significa "configurado explicitamente". No se imprime la
    credencial: solo se dice de donde sale.
    """
    if os.environ.get(VARIABLES_ENTORNO["clave"], "").strip():
        return None
    return (
        f"aviso: no has definido {VARIABLES_ENTORNO['clave']}; se usa la "
        "credencial por defecto del bridge del reto. En produccion define "
        f"{VARIABLES_ENTORNO['usuario']} / {VARIABLES_ENTORNO['clave']} "
        "(o pasa --usuario / --clave)."
    )

ESTADOS_VALIDOS = {"PENDIENTE", "PAGADA"}

# `re.ASCII` no es decorativo: sin el, `\d` tambien casa digitos no ASCII
# (arabigo-indicos, por ejemplo), de modo que un id como 'AS-٠٠١٢٣' pasaria
# la validacion. Los identificadores del ERP son ASCII o no son validos.
PATRON_ASIENTO = re.compile(r"^AS-\d{5}$", re.ASCII)
PATRON_FECHA_ES = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", re.ASCII)
PATRON_FECHA_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$", re.ASCII)

# Separador de la clave compuesta. Ninguno de los componentes (codigo de
# proveedor, pedido, fecha ISO, importe) puede contenerlo.
SEPARADOR_CLAVE = "|"

# El cuerpo de un error HTTP viaja pegado al codigo de estado para no perder la
# explicacion del ERP; este es el separador entre ambos.
SEPARADOR_HTTP = "\x00HTTP"

# Ventana del rate limit del bridge (10 peticiones/s): esperamos algo mas de 1 s.
ESPERA_429 = 1.1

# Campos que el ERP puede dejar vacios: no invalidan el asiento, pero se
# reportan como aviso y quedan registrados en `avisos` del snapshot.
CAMPOS_OPCIONALES = ("nif", "pedido", "proveedor")

# Marcador para emitir los importes como NUMERO json con sus dos decimales
# exactos (json.dumps no sabe serializar Decimal y float() perderia el
# formato "12874.40"). Se sustituye tras el volcado y se valida re-parseando.
MARCA_DECIMAL = "@@DEC:"
RE_MARCA_DECIMAL = re.compile(r'"@@DEC:(-?\d+\.\d{2})@@"', re.ASCII)


class ErrorErp(Exception):
    """Fallo irrecuperable del bridge (no merece mas reintentos)."""


# --------------------------------------------------------------------------- #
# Helpers de dominio
# --------------------------------------------------------------------------- #

def _texto_nodo(nodo: ET.Element | None, etiqueta: str, defecto: str = "") -> str:
    """`findtext` que tolera que falte la etiqueta o el propio nodo padre.

    `findtext` devuelve `None` si el elemento no existe, y en el ERP eso pasa:
    los asientos sin NIF simplemente omiten `<nif>` en lugar de mandarlo vacio.
    """
    if nodo is None:
        return defecto
    return (nodo.findtext(etiqueta) or defecto).strip()


def _entero_nodo(nodo: ET.Element | None, etiqueta: str, defecto: int = 0) -> int:
    """`findtext` + `int`, con un error claro en vez de un `ValueError` crudo.

    Un contador de paginacion ilegible no puede degradarse en silencio (dirige
    el bucle de descarga), pero una etiqueta ausente si se admite como defecto.
    """
    texto = _texto_nodo(nodo, etiqueta)
    if not texto:
        return defecto
    try:
        return int(texto)
    except ValueError as error:
        raise ErrorErp(f"<{etiqueta}> no es un entero: {texto!r}") from error


def importe_a_decimal(texto: str) -> Decimal:
    """'12.874,40' -> Decimal('12874.40'). Formato contable espanol.

    El punto solo se trata como separador de miles cuando hay coma, porque en
    el formato espanol los miles van en grupos exactos de tres cifras
    ('12.874,40'). Sin esa distincion un '473.70' suelto se leeria como 47370:
    un error de 100x en un importe y completamente silencioso.
    """
    limpio = (texto or "").strip().replace("\u00a0", "")
    if not limpio:
        raise InvalidOperation("importe vacio")
    if "," in limpio:
        # Espanol de verdad: los puntos son de miles y la coma es decimal.
        limpio = limpio.replace(".", "").replace(",", ".")
    elif re.search(r"\.\d{1,2}$", limpio, re.ASCII):
        # Sin coma y con una o dos cifras tras el ultimo punto: ya viene ISO.
        pass
    else:
        # Sin coma y sin decimal: los puntos solo pueden ser de miles.
        limpio = limpio.replace(".", "")
    return Decimal(limpio)


def fecha_a_iso(texto: str) -> str:
    """'03/09/2024' -> '2024-09-03'. Acepta tambien ISO ya normalizado."""
    valor = (texto or "").strip()
    coincidencia = PATRON_FECHA_ES.match(valor)
    if coincidencia:
        dia, mes, anio = coincidencia.groups()
        return f"{anio}-{int(mes):02d}-{int(dia):02d}"
    if PATRON_FECHA_ISO.match(valor):
        return valor
    return ""


def formato_importe(dec: Decimal) -> str:
    """'2480,5' -> '2480.50'. Cuantizado a 2 decimales, estable y sin float."""
    return str(dec.quantize(Decimal("0.01")))


def clave_factura(asiento: dict) -> str:
    """Clave compuesta que identifica la FACTURA (no la fila del ERP).

        proveedor | pedido | fecha | importe

    Se construye desde el Decimal ya normalizado, nunca desde el float del
    JSON, para que la cuantizacion sea exacta y el texto coincida siempre con
    el `importe` que se escribe en el documento.
    """
    return SEPARADOR_CLAVE.join((
        asiento["proveedor"],
        asiento["pedido"],
        asiento["fecha"],
        formato_importe(asiento["importe"]),
    ))


def importe_json(dec: Decimal) -> str:
    """Devuelve el texto crudo para inyectar como numero JSON."""
    return f"{MARCA_DECIMAL}{formato_importe(dec)}@@"


def codigo_de_error(cuerpo: str) -> tuple[str | None, str | None]:
    """Extrae <codigo>/<mensaje> del XML de error del ERP."""
    try:
        raiz = ET.fromstring(cuerpo)
    except ET.ParseError:
        return None, None
    return raiz.findtext("codigo"), raiz.findtext("mensaje")


# --------------------------------------------------------------------------- #
# Cliente
# --------------------------------------------------------------------------- #

class ClienteErp:
    """Cliente con reintento diferenciado por tipo de fallo."""

    def __init__(self, base: str, usuario: str, clave: str,
                 max_intentos: int = 8, verboso: bool = False) -> None:
        self.base = base.rstrip("/")
        self.usuario = usuario
        self.clave = clave
        self.max_intentos = max_intentos
        self.verboso = verboso
        self.token: str | None = None
        self.reintentos = {"ora_00600": 0, "ses_401": 0, "erp_429": 0}
        self.peticiones = 0

    def _log(self, mensaje: str) -> None:
        if self.verboso:
            print(f"    · {mensaje}")

    def _peticion(self, ruta: str, datos: bytes | None = None) -> str:
        """Una peticion cruda. Devuelve el cuerpo decodificado en ISO-8859-1."""
        cabeceras = {"Accept": "text/xml"}
        if datos is None and self.token:
            cabeceras["X-ERP-Token"] = self.token
        peticion = urllib.request.Request(
            self.base + ruta, data=datos, headers=cabeceras,
            method="POST" if datos is not None else "GET",
        )
        self.peticiones += 1
        try:
            with urllib.request.urlopen(peticion, timeout=30) as respuesta:
                return respuesta.read().decode("iso-8859-1", errors="replace")
        except urllib.error.HTTPError as error:
            # Devolvemos el cuerpo igualmente: el ERP explica el fallo ahi.
            cuerpo = error.read().decode("iso-8859-1", errors="replace")
            return f"{cuerpo}{SEPARADOR_HTTP}{error.code}"
        except urllib.error.URLError as error:
            raise ErrorErp(
                f"no puedo hablar con el ERP en {self.base} ({error.reason}).\n"
                "    Arranca el bridge del ERP en esa direccion (el del reto se\n"
                "    arranca con `python alberto_erp.py --rapido`) y, si no esta\n"
                "    en el puerto 8009, apunta la URL con --base-url o con la\n"
                f"    variable de entorno {VARIABLES_ENTORNO['base_url']}."
            ) from error

    def login(self) -> None:
        """POST /erp/login. Devuelve y guarda el token de sesion."""
        datos = urllib.parse.urlencode(
            {"usuario": self.usuario, "clave": self.clave}
        ).encode("utf-8")

        for intento in range(1, self.max_intentos + 1):
            crudo = self._peticion("/erp/login", datos=datos)
            cuerpo, _, codigo_http = crudo.partition(SEPARADOR_HTTP)
            codigo_http = codigo_http or "200"

            try:
                raiz = ET.fromstring(cuerpo)
            except ET.ParseError as error:
                raise ErrorErp(f"respuesta de login ilegible: {cuerpo[:200]!r}") from error

            token = raiz.findtext("token")
            if token:
                self.token = token.strip()
                self._log(f"login OK (token {self.token[:8]}…, intento {intento})")
                return

            codigo, mensaje = codigo_de_error(cuerpo)

            if codigo == "ERP-429" or codigo_http == "429":
                self.reintentos["erp_429"] += 1
                self._log(f"429 en login, espero {ESPERA_429}s (intento {intento})")
                time.sleep(ESPERA_429)
                continue

            if codigo == "SES-401" or codigo_http == "401":
                # Credenciales mal: reintentar no arregla nada.
                # La clave se omite a proposito: un mensaje de error no puede
                # acabar con credenciales en un log.
                raise ErrorErp(
                    f"credenciales rechazadas (SES-401). {mensaje or ''}\n"
                    f"    Revisa usuario/clave del bridge (usuario probado: "
                    f"{self.usuario}; define {VARIABLES_ENTORNO['usuario']} / "
                    f"{VARIABLES_ENTORNO['clave']} o pasa --usuario / --clave)"
                )

            raise ErrorErp(f"login fallo: HTTP {codigo_http} {codigo or ''} {mensaje or ''}".strip())

        raise ErrorErp(f"login sin exito tras {self.max_intentos} intentos")

    def _reaccionar(self, ruta: str, codigo: str | None, codigo_http: str,
                    mensaje: str | None, intento: int) -> float:
        """Clasifica un fallo y devuelve cuantos segundos esperar antes de reintentar.

        El bridge inyecta fallos a proposito y cada uno pide una reaccion
        distinta; confundirlos es lo que hace que un reintento ciego se quede
        atascado repitiendo el fallo equivocado:

          ORA-00600 -> la sesion sigue viva, hay que repetir la MISMA pagina.
          SES-401   -> el token ya no vale: rehacer login y reintentar.
          ERP-429   -> rate limit: bajar el ritmo, la peticion no se perdio.
          otro      -> irrecuperable, no tiene sentido insistir.
        """
        if codigo == "ORA-00600" or codigo_http == "500":
            self.reintentos["ora_00600"] += 1
            espera = min(0.2 * intento, 2.0)
            self._log(f"{ruta}: ORA-00600 (fallo inyectado), reintento en {espera:.1f}s")
            return espera

        if codigo == "SES-401" or codigo_http == "401":
            self.reintentos["ses_401"] += 1
            self._log(f"{ruta}: SES-401, rehago login")
            self.token = None
            self.login()
            return 0.1

        if codigo == "ERP-429" or codigo_http == "429":
            self.reintentos["erp_429"] += 1
            self._log(f"{ruta}: ERP-429, espero {ESPERA_429}s (limite: 10 peticiones/s)")
            return ESPERA_429

        raise ErrorErp(f"{ruta}: HTTP {codigo_http} {codigo or ''} {mensaje or ''}".strip())

    def consultar(self, ruta: str) -> ET.Element:
        """GET autenticado con reintento diferenciado."""
        ultimo = "desconocido"

        for intento in range(1, self.max_intentos + 1):
            crudo = self._peticion(ruta)
            cuerpo, _, codigo_http = crudo.partition(SEPARADOR_HTTP)
            codigo_http = codigo_http or "200"

            if codigo_http == "200":
                try:
                    return ET.fromstring(cuerpo)
                except ET.ParseError as error:
                    raise ErrorErp(f"{ruta}: XML ilegible: {cuerpo[:200]!r}") from error

            codigo, mensaje = codigo_de_error(cuerpo)
            ultimo = f"HTTP {codigo_http} {codigo or ''} {mensaje or ''}".strip()
            time.sleep(self._reaccionar(ruta, codigo, codigo_http, mensaje, intento))

        raise ErrorErp(f"{ruta}: sin exito tras {self.max_intentos} intentos ({ultimo})")

    def estado(self) -> dict:
        """GET /erp/estado. Salud del bridge antes de gastar cuota de token."""
        raiz = self.consultar("/erp/estado")
        return {
            "version": _texto_nodo(raiz, "version"),
            "activo_segundos": _entero_nodo(raiz, "activo_segundos"),
            "asientos": _entero_nodo(raiz, "asientos"),
            "actualizacion_cargada": _texto_nodo(raiz, "actualizacion_cargada"),
            "animo": _texto_nodo(raiz, "animo"),
        }

    def pagina(self, numero: int) -> tuple[list[dict], dict]:
        """Descarga una pagina de /erp/asientos y la normaliza."""
        raiz = self.consultar(f"/erp/asientos?pagina={numero}")
        meta_el = raiz.find("meta")
        if meta_el is None:
            # Sin <meta> no sabemos cuantas paginas quedan: parar aqui es mejor
            # que seguir a ciegas y escribir un snapshot incompleto.
            raise ErrorErp(f"/erp/asientos?pagina={numero}: falta el bloque <meta>")
        meta = {
            "total": _entero_nodo(meta_el, "total"),
            "paginas": _entero_nodo(meta_el, "paginas"),
            "pagina": _entero_nodo(meta_el, "pagina"),
            "por_pagina": _entero_nodo(meta_el, "por_pagina", POR_PAGINA_DEFECTO),
            "generado": _texto_nodo(meta_el, "generado"),
        }
        return [_normalizar_asiento(n) for n in raiz.findall("./asientos/asiento")], meta


def _normalizar_asiento(nodo: ET.Element) -> dict:
    """<asiento> XML -> dict del dominio (nombres de `src/domain.rs`)."""
    ident = _texto_nodo(nodo, "id")
    crudo_importe = _texto_nodo(nodo, "importe")
    try:
        importe = importe_a_decimal(crudo_importe)
    except InvalidOperation as error:
        # Un importe ilegible es un fallo del que hay que informar, no un
        # traceback: sin este envoltorio el script revienta con un stack de
        # <decimal> y sin decir de que asiento habla.
        raise ErrorErp(
            f"{ident or '<sin id>'}: importe ilegible ({crudo_importe!r})"
        ) from error

    asiento = {
        "asiento_id": ident,
        "fecha": fecha_a_iso(_texto_nodo(nodo, "fecha")),
        "proveedor": _texto_nodo(nodo, "proveedor"),
        "nif": _texto_nodo(nodo, "nif"),
        "pedido": _texto_nodo(nodo, "pedido"),
        "importe": importe,
        "estado": _texto_nodo(nodo, "estado"),
    }
    # La identidad de la factura se deriva aqui, donde el importe todavia es
    # Decimal: mas abajo ya solo queda el float serializado.
    asiento["clave_factura"] = clave_factura(asiento)
    return asiento


# --------------------------------------------------------------------------- #
# Verificacion
# --------------------------------------------------------------------------- #

def deduplicar(asientos: list[dict]) -> tuple[list[dict], list[dict]]:
    """Colapsa facturas duplicadas: una `clave_factura` -> un solo asiento.

    Se conserva el de `asiento_id` mas bajo y se ordena explicitamente para
    elegirlo: el orden de las paginas del ERP no va por id, asi que quedarse
    con "el primero que llego" no seria reproducible entre descargas.

    Devuelve `(conservados, traza)`. La traza describe que se descarto y por
    que clave, para poder auditar el colapso (o revertirlo).
    """
    grupos: dict[str, list[dict]] = {}
    for asiento in asientos:
        grupos.setdefault(asiento["clave_factura"], []).append(asiento)

    conservado_por_clave: dict[str, str] = {}
    traza: list[dict] = []
    for clave, grupo in grupos.items():
        if len(grupo) == 1:
            continue
        ordenado = sorted(grupo, key=lambda a: a["asiento_id"])
        conservado_por_clave[clave] = ordenado[0]["asiento_id"]
        traza.append({
            "clave_factura": clave,
            "conservado": ordenado[0]["asiento_id"],
            "descartados": [a["asiento_id"] for a in ordenado[1:]],
        })

    if not traza:
        return list(asientos), []

    # Se filtra sobre la lista original para no alterar su orden.
    conservados = [
        a for a in asientos
        if a["clave_factura"] not in conservado_por_clave
        or a["asiento_id"] == conservado_por_clave[a["clave_factura"]]
    ]
    return conservados, traza


def _revisar_recuento(descargados: int, total_erp: int, paginas_erp: int,
                      por_pagina: int, pagina_max: int | None) -> list[str]:
    """Contrasta lo descargado con lo que el ERP declara en <meta>.

    `por_pagina` viene del propio ERP y no se asume constante: si el bridge
    cambiara el tamano de pagina, un 20 fijo aqui produciria un falso fallo en
    `--solo-pagina`.
    """
    problemas: list[str] = []
    esperado = total_erp if pagina_max is None else min(total_erp, pagina_max * por_pagina)

    if descargados != esperado:
        problemas.append(f"descargados: {descargados} != esperado {esperado} (ERP dice {total_erp})")
    if paginas_erp and pagina_max is None and descargados > total_erp:
        problemas.append(f"mas asientos que los declarados por el ERP ({total_erp})")
    return problemas


def _revisar_identidad(asiento: dict, vistos: set[str]) -> list[str]:
    """`asiento_id` tiene que ser unico, reconocible y con campos de control."""
    problemas: list[str] = []
    ident = asiento["asiento_id"]

    if ident in vistos:
        problemas.append(f"asiento_id duplicado: {ident}")
    vistos.add(ident)
    if not PATRON_ASIENTO.match(ident):
        problemas.append(f"asiento_id no cumple ^AS-\\d{{5}}$: {ident}")
    if asiento["estado"] not in ESTADOS_VALIDOS:
        problemas.append(f"estado fuera de {{PENDIENTE, PAGADA}}: {ident}={asiento['estado']!r}")
    if not asiento["fecha"]:
        problemas.append(f"sin fecha: {ident}")
    return problemas


def _revisar_clave(asiento: dict, claves_vistas: set[str]) -> list[str]:
    """La clave compuesta es lo que sostiene la conciliacion entera.

    Si le falta un componente deja de discriminar y acabaria colapsando
    facturas distintas en la deduplicacion: es un fallo, no un aviso. Y si se
    repite despues de deduplicar, es que la deduplicacion no se aplico.
    """
    problemas: list[str] = []
    clave = asiento["clave_factura"]

    if clave in claves_vistas:
        problemas.append(f"clave_factura repetida tras deduplicar: {clave}")
    claves_vistas.add(clave)
    partes = clave.split(SEPARADOR_CLAVE)
    if len(partes) != 4 or any(not parte for parte in partes):
        problemas.append(f"clave_factura incompleta: {asiento['asiento_id']}={clave!r}")
    return problemas


def verificar(asientos: list[dict], descargados: int, total_erp: int, paginas_erp: int,
              por_pagina: int, pagina_max: int | None) -> tuple[list[str], list[str]]:
    """Comprueba invariantes antes de escribir. Devuelve `(problemas, avisos)`.

    Un problema impide escribir el snapshot; un aviso solo se registra.

    `asientos` es la lista YA deduplicada, mientras que `descargados` es lo que
    trajo el ERP antes de colapsar: la comparacion contra el total declarado se
    hace sobre lo descargado, no sobre lo deduplicado, porque si no cada
    factura duplicada se reportaria como una descarga incompleta.
    """
    problemas = _revisar_recuento(descargados, total_erp, paginas_erp, por_pagina, pagina_max)
    vistos: set[str] = set()
    claves_vistas: set[str] = set()
    faltantes: dict[str, list[str]] = {campo: [] for campo in CAMPOS_OPCIONALES}

    for asiento in asientos:
        problemas += _revisar_identidad(asiento, vistos)
        problemas += _revisar_clave(asiento, claves_vistas)
        for campo in CAMPOS_OPCIONALES:
            if not asiento[campo]:
                faltantes[campo].append(asiento["asiento_id"])

    # Un string vacio sigue cumpliendo el $jsonSchema, asi que el documento es
    # valido; pero degrada el match. Se avisa y se deja constancia en el propio
    # snapshot (`avisos`), en lugar de perderse en la consola.
    avisos = [
        f"{campo} vacio en {len(ids)}/{len(asientos)}: {ids[:3]}"
        for campo, ids in faltantes.items() if ids
    ]
    return problemas, avisos


# --------------------------------------------------------------------------- #
# Volcado
# --------------------------------------------------------------------------- #

def construir_snapshot(asientos: list[dict], snapshot_id: str, meta: dict) -> dict:
    """Estructura del fichero: metadatos del snapshot + asientos importables.

    `_id` sigue siendo `<snapshot_id>#<asiento_id>` (identidad de ALMACENAMIENTO,
    lo que mantiene la reimportacion idempotente y es a lo que apunta el indice
    documentado). `clave_factura` es la identidad de NEGOCIO de la factura, y es
    la que debe usarse para conciliar: sobrevive a que el mismo asiento se
    renumere entre snapshots.
    """
    documentos = []
    for asiento in asientos:
        documentos.append({
            "_id": f"{snapshot_id}#{asiento['asiento_id']}",
            "asiento_id": asiento["asiento_id"],
            "snapshot_id": snapshot_id,
            "clave_factura": asiento["clave_factura"],
            "fecha": asiento["fecha"],
            "proveedor": asiento["proveedor"],
            "nif": asiento["nif"],
            "pedido": asiento["pedido"],
            "importe": importe_json(asiento["importe"]),
            "estado": asiento["estado"],
            "vigente": True,
            "esquema_version": ESQUEMA_VERSION,
        })

    return {
        "_id": snapshot_id,
        "snapshot_id": snapshot_id,
        "descargado_en": meta["descargado_en"],
        "total_asientos": len(documentos),
        "asientos_descargados": meta["asientos_descargados"],
        "paginas": meta["paginas"],
        "vigente": meta["vigente"],
        "estado": meta["estado"],
        "reintentos": meta["reintentos"],
        "duracion_ms": meta["duracion_ms"],
        "esquema_version": ESQUEMA_VERSION,
        "avisos": meta["avisos"],
        "duplicados": meta["duplicados"],
        "asientos": documentos,
    }


def escribir_snapshot(datos: dict, destino: Path) -> None:
    """Escribe el JSON de forma atomica y emitiendo importes como numero."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporal = destino.with_suffix(destino.suffix + ".tmp")

    texto = json.dumps(datos, ensure_ascii=False, indent=2)
    texto = RE_MARCA_DECIMAL.sub(r"\1", texto)

    # La sustitucion es una transformacion textual: se valida re-parseando.
    releido = json.loads(texto)
    if len(releido["asientos"]) != len(datos["asientos"]):
        raise ErrorErp("el volcado corrompio el numero de asientos")
    if MARCA_DECIMAL in texto:
        raise ErrorErp("quedo un marcador decimal sin sustituir en el JSON")
    for original, final in zip(datos["asientos"], releido["asientos"]):
        if not isinstance(final["importe"], (int, float)):
            raise ErrorErp(f"importe no numerico tras el volcado: {final['importe']!r}")

    temporal.write_text(texto + "\n", encoding="utf-8")
    os.replace(temporal, destino)


# --------------------------------------------------------------------------- #
# Ejecucion
# --------------------------------------------------------------------------- #

def descargar_asientos(cliente: ClienteErp,
                       solo_pagina: int | None) -> tuple[list[dict], dict]:
    """Recorre /erp/asientos pagina a pagina y devuelve (asientos, ultima meta).

    Se va en serie y sin hilos a proposito: el limite del bridge es de 10
    peticiones/s en una ventana de 1 s, y mas de 8 en paralelo garantizan un
    ERP-429. En serie cada peticion cuesta ~0,12 s en el servidor.
    """
    asientos: list[dict] = []
    meta: dict = {}
    pagina = 1

    while True:
        lote, meta = cliente.pagina(pagina)
        asientos.extend(lote)
        total_paginas = meta["paginas"] or pagina
        print(f"  pagina {pagina:>2}/{total_paginas}  "
              f"{len(lote):>2} asientos  (acumulado {len(asientos)})")
        if solo_pagina and pagina >= solo_pagina:
            break
        if pagina >= total_paginas:
            break
        pagina += 1

    return asientos, meta


def aplicar_deduplicacion(asientos: list[dict],
                          sin_dedup: bool) -> tuple[list[dict], list[dict]]:
    """Colapsa duplicados si procede e informa de lo descartado.

    Lo descartado nunca se tira en silencio: va a `duplicados` en el snapshot.
    """
    print()
    print("deduplicacion")

    if sin_dedup:
        print(f"  --sin-dedup: se conservan los {len(asientos)} asientos tal cual")
        return list(asientos), []

    facturas, traza = deduplicar(asientos)
    print(f"  {len(asientos)} asientos -> {len(facturas)} facturas "
          f"({len(asientos) - len(facturas)} descartado(s) "
          f"en {len(traza)} clave(s) duplicada(s))")
    for grupo in traza:
        print(f"    {grupo['clave_factura']}")
        print(f"      conserva {grupo['conservado']}  "
              f"descarta {', '.join(grupo['descartados'])}")
    return facturas, traza


def informar_verificacion(problemas: list[str], avisos: list[str]) -> None:
    """Vuelca el resultado de `verificar` a consola. Los avisos van primero."""
    print()
    print("verificacion")
    for aviso in avisos:
        print(f"  aviso: {aviso}")
    for problema in problemas:
        print(f"  FALLO: {problema}")
    if not problemas and not avisos:
        print("  sin incidencias")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parsear_argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Descarga los asientos del ERP Miralmar a data/erp_snapshot.json",
    )
    raiz = Path(__file__).resolve().parent
    parser.add_argument("--base-url", default=BASE_DEFECTO,
                        help=f"URL del bridge (por defecto {BASE_DEFECTO}; "
                             f"env {VARIABLES_ENTORNO['base_url']})")
    parser.add_argument("--out", default=str(raiz / "data" / "erp_snapshot.json"),
                        help="fichero de salida (relativo a la raiz del proyecto)")
    parser.add_argument("--usuario", default=USUARIO_DEFECTO,
                        help=f"env {VARIABLES_ENTORNO['usuario']}")
    parser.add_argument("--clave", default=CLAVE_DEFECTO,
                        help=f"contrasena del bridge; env {VARIABLES_ENTORNO['clave']} "
                             "(no se escribe nunca en los mensajes)")
    parser.add_argument("--max-intentos", type=int, default=8,
                        help="intentos por peticion ante ORA-00600/SES-401/ERP-429")
    parser.add_argument("--solo-pagina", type=int, metavar="N",
                        help="descarga solo hasta la pagina N (no escribe fichero)")
    parser.add_argument("--dry-run", action="store_true",
                        help="descarga y verifica, pero no escribe nada")
    parser.add_argument("--sin-dedup", action="store_true",
                        help="no colapsa facturas duplicadas (conserva todos los asientos)")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parsear_argumentos()
    inicio = time.monotonic()
    instante = datetime.now(timezone.utc)
    snapshot_id = "snap-" + instante.strftime("%Y-%m-%dT%H-%M-%SZ")

    aviso = aviso_de_credencial()
    if aviso and not args.quiet:
        print(aviso, file=sys.stderr)

    print(f"ERP        {args.base_url}")
    print(f"snapshot   {snapshot_id}")

    cliente = ClienteErp(args.base_url, args.usuario, args.clave,
                         max_intentos=args.max_intentos,
                         verboso=not args.quiet)

    try:
        estado = cliente.estado()
        print(f"estado     {estado['asientos']} asientos declarados · "
              f"lote2 cargado: {estado['actualizacion_cargada']}")

        cliente.login()
        asientos, meta_pagina = descargar_asientos(cliente, args.solo_pagina)

    except ErrorErp as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrumpido.", file=sys.stderr)
        return 130

    duracion_ms = int((time.monotonic() - inicio) * 1000)
    descargados = len(asientos)

    facturas, traza_duplicados = aplicar_deduplicacion(asientos, args.sin_dedup)

    problemas, avisos = verificar(
        facturas, descargados,
        meta_pagina.get("total") or descargados,
        meta_pagina.get("paginas") or 0,
        meta_pagina.get("por_pagina") or POR_PAGINA_DEFECTO,
        args.solo_pagina,
    )
    informar_verificacion(problemas, avisos)

    reintentos = cliente.reintentos
    print(f"\nreintentos  ORA-00600={reintentos['ora_00600']} "
          f"SES-401={reintentos['ses_401']} ERP-429={reintentos['erp_429']}")

    segundos = max(duracion_ms / 1000, 0.001)
    print(f"duracion    {duracion_ms} ms  ·  {cliente.peticiones} peticiones  ·  "
          f"{descargados / segundos:.2f} asientos/s")

    if args.solo_pagina:
        print(f"\n--solo-pagina {args.solo_pagina}: humo OK, no se escribe fichero.")
        return 1 if problemas else 0

    if problemas:
        print("\nNo se escribe el fichero: el snapshot quedaria invalido.", file=sys.stderr)
        return 1

    meta = {
        "descargado_en": instante.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "paginas": meta_pagina["paginas"],
        "vigente": True,
        "estado": "COMPLETO",
        "reintentos": reintentos,
        "duracion_ms": duracion_ms,
        "avisos": avisos,
        "asientos_descargados": descargados,
        "duplicados": traza_duplicados,
    }
    datos = construir_snapshot(facturas, snapshot_id, meta)

    if args.dry_run:
        print(f"\n--dry-run: no se escribe {args.out}")
        return 0

    destino = Path(args.out)
    try:
        escribir_snapshot(datos, destino)
    except (OSError, ErrorErp) as error:
        print(f"\nERROR escribiendo {destino}: {error}", file=sys.stderr)
        return 3

    tamano_kb = destino.stat().st_size / 1024
    print(f"\nOK  {destino}  ({len(facturas)} facturas de {descargados} asientos, "
          f"{tamano_kb:.0f} KB)")
    print(f"    cargar en Mongo:  python importar_asientos_mongo.py --snapshot {destino}")
    print("    recuerda marcar vigente:false en los asientos del snapshot anterior")
    return 0


if __name__ == "__main__":
    sys.exit(main())
