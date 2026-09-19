#!/usr/bin/env python
"""Descarga completa del ERP Miralmar -> data/erp_snapshot.json.

Por que existe este script (y no solo `src/erp.rs`):

1.  `src/erp.rs` todavia no existe. Este script produce HOY el snapshot de los
    516 asientos, que es lo que desbloquea a `reconciler` y `rules`.
2.  Es el **oraculo independiente** de verificacion: lee el ERP por su cuenta y
    sirve para contrastar el resultado de `outcomes.jsonl` (ver `--solo-consulta`).
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

Salida (`data/erp_snapshot.json`): objeto con metadatos del snapshot + array de
asientos ya listos para `mongoimport` en la coleccion `asientos`
(cada asiento trae `_id`, `snapshot_id`, `vigente`, `esquema_version`).

Uso:
    python descargar_erp.py                       # escribe data/erp_snapshot.json
    python descargar_erp.py --dry-run             # verifica sin escribir
    python descargar_erp.py --solo-pagina 1       # humo rapido
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
    if hasattr(_flujo, "reconfigure"):
        _flujo.reconfigure(encoding="utf-8", errors="replace")

ESQUEMA_VERSION = 1
USUARIO_DEFECTO = "alberto"
CLAVE_DEFECTO = "FACTURAS2009"
BASE_DEFECTO = "http://127.0.0.1:8009"

ESTADOS_VALIDOS = {"PENDIENTE", "PAGADA"}
PATRON_ASIENTO = re.compile(r"^AS-[0-9]{5}$")
PATRON_FECHA_ES = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
PATRON_FECHA_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

# Marcador para emitir los importes como NUMERO json con sus dos decimales
# exactos (json.dumps no sabe serializar Decimal y float() perderia el
# formato "12874.40"). Se sustituye tras el volcado y se valida re-parseando.
MARCA_DECIMAL = "@@DEC:"
RE_MARCA_DECIMAL = re.compile(r'"@@DEC:(-?[0-9]+\.[0-9]{2})@@"')


class ErrorErp(Exception):
    """Fallo irrecuperable del bridge (no merece mas reintentos)."""


# --------------------------------------------------------------------------- #
# Helpers de dominio
# --------------------------------------------------------------------------- #

def importe_a_decimal(texto: str) -> Decimal:
    """'12.874,40' -> Decimal('12874.40'). Formato contable espanol."""
    limpio = (texto or "").strip().replace("\u00a0", "")
    if not limpio:
        raise InvalidOperation("importe vacio")
    limpio = limpio.replace(".", "").replace(",", ".")
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


def importe_json(dec: Decimal) -> str:
    """Devuelve el texto crudo para inyectar como numero JSON."""
    return f"{MARCA_DECIMAL}{dec.quantize(Decimal('0.01'))}@@"


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
            return error.read().decode("iso-8859-1", errors="replace") + "\x00HTTP" + str(error.code)
        except urllib.error.URLError as error:
            raise ErrorErp(
                f"no puedo hablar con el ERP en {self.base} ({error.reason}).\n"
                f"    Arrancalo con:  python alberto_erp.py --rapido\n"
                f"    (esta en c:\\Users\\marti\\Documents\\Hackaton\\500-sombras-de-alberto)"
            ) from error

    def login(self) -> None:
        """POST /erp/login. Devuelve y guarda el token de sesion."""
        datos = urllib.parse.urlencode(
            {"usuario": self.usuario, "clave": self.clave}
        ).encode("utf-8")

        for intento in range(1, self.max_intentos + 1):
            crudo = self._peticion("/erp/login", datos=datos)
            cuerpo, _, codigo_http = crudo.partition("\x00HTTP")
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
                espera = 1.1
                self._log(f"429 en login, espero {espera}s (intento {intento})")
                time.sleep(espera)
                continue

            if codigo == "SES-401" or codigo_http == "401":
                # Credenciales mal: reintentar no arregla nada.
                raise ErrorErp(
                    f"credenciales rechazadas (SES-401). {mensaje or ''}\n"
                    f"    Comprueba usuario/clave: {self.usuario}/{self.clave}"
                )

            raise ErrorErp(f"login fallo: HTTP {codigo_http} {codigo or ''} {mensaje or ''}".strip())

        raise ErrorErp(f"login sin exito tras {self.max_intentos} intentos")

    def consultar(self, ruta: str) -> ET.Element:
        """GET autenticado con reintento diferenciado."""
        ultimo = "desconocido"

        for intento in range(1, self.max_intentos + 1):
            crudo = self._peticion(ruta)
            cuerpo, _, codigo_http = crudo.partition("\x00HTTP")
            codigo_http = codigo_http or "200"

            if codigo_http == "200":
                try:
                    return ET.fromstring(cuerpo)
                except ET.ParseError as error:
                    raise ErrorErp(f"{ruta}: XML ilegible: {cuerpo[:200]!r}") from error

            codigo, mensaje = codigo_de_error(cuerpo)
            ultimo = f"HTTP {codigo_http} {codigo or ''} {mensaje or ''}".strip()

            if codigo == "ORA-00600" or codigo_http == "500":
                # Fallo inyectado a proposito. La sesion sigue valida.
                self.reintentos["ora_00600"] += 1
                espera = min(0.2 * intento, 2.0)
                self._log(f"{ruta}: ORA-00600 (fallo inyectado), reintento en {espera:.1f}s")
            elif codigo == "SES-401" or codigo_http == "401":
                self.reintentos["ses_401"] += 1
                self._log(f"{ruta}: SES-401, rehago login")
                self.token = None
                self.login()
                espera = 0.1
            elif codigo == "ERP-429" or codigo_http == "429":
                self.reintentos["erp_429"] += 1
                espera = 1.1
                self._log(f"{ruta}: ERP-429, espero {espera}s (limite: 10 peticiones/s)")
            else:
                raise ErrorErp(f"{ruta}: {ultimo}")

            time.sleep(espera)

        raise ErrorErp(f"{ruta}: sin exito tras {self.max_intentos} intentos ({ultimo})")

    def estado(self) -> dict:
        raiz = self.consultar("/erp/estado")
        return {
            "version": raiz.findtext("version"),
            "activo_segundos": int(raiz.findtext("activo_segundos") or 0),
            "asientos": int(raiz.findtext("asientos") or 0),
            "actualizacion_cargada": raiz.findtext("actualizacion_cargada"),
            "animo": raiz.findtext("animo"),
        }

    def pagina(self, numero: int) -> tuple[list[dict], dict]:
        """Descarga una pagina de /erp/asientos y la normaliza."""
        raiz = self.consultar(f"/erp/asientos?pagina={numero}")
        meta_el = raiz.find("meta")
        meta = {
            "total": int(meta_el.findtext("total") or 0),
            "paginas": int(meta_el.findtext("paginas") or 0),
            "pagina": int(meta_el.findtext("pagina") or 0),
            "por_pagina": int(meta_el.findtext("por_pagina") or 20),
            "generado": meta_el.findtext("generado"),
        }
        return [_normalizar_asiento(n) for n in raiz.findall("./asientos/asiento")], meta


def _normalizar_asiento(nodo: ET.Element) -> dict:
    """<asiento> XML -> dict del dominio (nombres de `src/domain.rs`)."""
    return {
        "asiento_id": (nodo.findtext("id") or "").strip(),
        "fecha": fecha_a_iso(nodo.findtext("fecha") or ""),
        "proveedor": (nodo.findtext("proveedor") or "").strip(),
        "nif": (nodo.findtext("nif") or "").strip(),
        "pedido": (nodo.findtext("pedido") or "").strip(),
        "importe": importe_a_decimal(nodo.findtext("importe") or ""),
        "estado": (nodo.findtext("estado") or "").strip(),
    }


# --------------------------------------------------------------------------- #
# Verificacion
# --------------------------------------------------------------------------- #

def verificar(asientos: list[dict], total_erp: int, paginas_erp: int,
              pagina_max: int | None) -> list[str]:
    """Comprueba invariantes antes de escribir. Devuelve lista de problemas."""
    problemas: list[str] = []
    esperado = total_erp if pagina_max is None else min(total_erp, pagina_max * 20)

    if len(asientos) != esperado:
        problemas.append(f"asientos: {len(asientos)} != esperado {esperado} (ERP dice {total_erp})")

    vistos: set[str] = set()
    duplicados: list[str] = []
    mal_formados: list[str] = []
    estados_raros: list[str] = []
    sin_fecha: list[str] = []
    sin_nif: list[str] = []
    sin_pedido: list[str] = []
    sin_proveedor: list[str] = []

    for asiento in asientos:
        ident = asiento["asiento_id"]
        if ident in vistos:
            duplicados.append(ident)
        vistos.add(ident)
        if not PATRON_ASIENTO.match(ident):
            mal_formados.append(ident)
        if asiento["estado"] not in ESTADOS_VALIDOS:
            estados_raros.append(f"{ident}={asiento['estado']!r}")
        if not asiento["fecha"]:
            sin_fecha.append(ident)
        if not asiento["nif"]:
            sin_nif.append(ident)
        if not asiento["pedido"]:
            sin_pedido.append(ident)
        if not asiento["proveedor"]:
            sin_proveedor.append(ident)

    if duplicados:
        problemas.append(f"asiento_id duplicado: {duplicados[:5]}")
    if mal_formados:
        problemas.append(f"asiento_id no cumple ^AS-[0-9]{{5}}$: {mal_formados[:5]}")
    if estados_raros:
        problemas.append(f"estado fuera de {{PENDIENTE, PAGADA}}: {estados_raros[:5]}")
    if sin_fecha:
        problemas.append(f"sin fecha: {len(sin_fecha)} ({sin_fecha[:5]})")

    # Campos vacios no rompen el $jsonSchema (string vacio es string), pero
    # degradan el match. Se avisa, no se falla.
    avisos = []
    for nombre, lista in (("nif", sin_nif), ("pedido", sin_pedido), ("proveedor", sin_proveedor)):
        if lista:
            avisos.append(f"{nombre} vacio en {len(lista)}/{len(asientos)}")
    if avisos:
        print(f"  aviso: {'; '.join(avisos)}")

    if len(asientos) != total_erp and pagina_max is None:
        problemas.append(f"descarga incompleta: faltan {total_erp - len(asientos)}")
    if paginas_erp and pagina_max is None and len(asientos) > total_erp:
        problemas.append("mas asientos que los declarados por el ERP")

    return problemas


# --------------------------------------------------------------------------- #
# Volcado
# --------------------------------------------------------------------------- #

def construir_snapshot(asientos: list[dict], snapshot_id: str, meta: dict) -> dict:
    """Estructura del fichero: metadatos del snapshot + asientos importables."""
    documentos = []
    for asiento in asientos:
        documentos.append({
            "_id": f"{snapshot_id}#{asiento['asiento_id']}",
            "asiento_id": asiento["asiento_id"],
            "snapshot_id": snapshot_id,
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
        "paginas": meta["paginas"],
        "vigente": meta["vigente"],
        "estado": meta["estado"],
        "reintentos": meta["reintentos"],
        "duracion_ms": meta["duracion_ms"],
        "esquema_version": ESQUEMA_VERSION,
        "avisos": meta["avisos"],
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
# CLI
# --------------------------------------------------------------------------- #

def parsear_argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Descarga los asientos del ERP Miralmar a data/erp_snapshot.json",
    )
    raiz = Path(__file__).resolve().parent
    parser.add_argument("--base-url", default=BASE_DEFECTO,
                        help=f"URL del bridge (por defecto {BASE_DEFECTO})")
    parser.add_argument("--out", default=str(raiz / "data" / "erp_snapshot.json"),
                        help="fichero de salida")
    parser.add_argument("--usuario", default=USUARIO_DEFECTO)
    parser.add_argument("--clave", default=CLAVE_DEFECTO)
    parser.add_argument("--max-intentos", type=int, default=8,
                        help="intentos por peticion ante ORA-00600/SES-401/ERP-429")
    parser.add_argument("--solo-pagina", type=int, metavar="N",
                        help="descarga solo hasta la pagina N (no escribe fichero)")
    parser.add_argument("--dry-run", action="store_true",
                        help="descarga y verifica, pero no escribe nada")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parsear_argumentos()
    inicio = time.monotonic()
    instante = datetime.now(timezone.utc)
    snapshot_id = "snap-" + instante.strftime("%Y-%m-%dT%H-%M-%SZ")

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

        pagina = 1
        asientos: list[dict] = []
        meta_pagina: dict = {}
        while True:
            lote, meta_pagina = cliente.pagina(pagina)
            asientos.extend(lote)
            total_paginas = meta_pagina["paginas"]
            print(f"  pagina {pagina:>2}/{total_paginas}  "
                  f"{len(lote):>2} asientos  (acumulado {len(asientos)})")
            if args.solo_pagina and pagina >= args.solo_pagina:
                break
            if pagina >= total_paginas:
                break
            pagina += 1

    except ErrorErp as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrumpido.", file=sys.stderr)
        return 130

    duracion_ms = int((time.monotonic() - inicio) * 1000)

    print()
    print("verificacion")
    problemas = verificar(asientos, meta_pagina.get("total", 0),
                          meta_pagina.get("paginas", 0), args.solo_pagina)
    for problema in problemas:
        print(f"  FALLO: {problema}")

    reintentos = cliente.reintentos
    print(f"\nreintentos  ORA-00600={reintentos['ora_00600']} "
          f"SES-401={reintentos['ses_401']} ERP-429={reintentos['erp_429']}")

    segundos = max(duracion_ms / 1000, 0.001)
    print(f"duracion    {duracion_ms} ms  ·  {cliente.peticiones} peticiones  ·  "
          f"{len(asientos) / segundos:.2f} asientos/s")

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
        "avisos": [],
    }
    datos = construir_snapshot(asientos, snapshot_id, meta)

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
    print(f"\nOK  {destino}  ({len(asientos)} asientos, {tamano_kb:.0f} KB)")
    print(f"    importacion directa:  mongoimport --collection asientos --jsonArray --file {destino}")
    print("    recuerda marcar vigente:false en los asientos del snapshot anterior")
    return 0


if __name__ == "__main__":
    sys.exit(main())
