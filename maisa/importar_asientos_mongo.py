#!/usr/bin/env python3
"""Carga los asientos del snapshot del ERP en la coleccion `asientos` de Mongo.

Por que existe este script y no basta con `mongoimport`
------------------------------------------------------

`descargar_erp.py` escribe `data/erp_snapshot.json` como **un objeto JSON**: las
claves de primer nivel son metadatos del snapshot y los asientos cuelgan de la
clave `asientos`. Eso choca con Mongo en tres puntos:

  1. `mongoimport --jsonArray` exige un ARRAY en la raiz, y aqui la raiz es un
     OBJETO; con `--file data/erp_snapshot.json` se importaria 1 documento en
     lugar de los 516. (Solucion: se emite NDJSON, un documento por linea.)
  2. Falta `--db`: sin el, la importacion se iria a la base `test`.
  3. El validador de `asientos` (`docker/mongosh/02-schema-init.js:249,253,258`)
     exige `fecha: date`, `importe: decimal` y `esquema_version: int`, y el
     snapshot los trae como `string`, `double` e `int`. Sin convertir, Mongo
     rechaza los 516 documentos con codigo 121.

Este script hace las tres cosas, y ademas:

  - **Valida antes de importar**, para que un dato malo salga como un mensaje de
    Python entendible y no como un error 121 de Mongo a medio importar.
  - Es **idempotente**: importa con `--mode=upsert --upsertFields=_id`, asi que
    reejecutarlo actualiza en vez de duplicar.
  - **Retira la vigencia del snapshot anterior** (`vigente: false`), que es el
    paso manual que el descargador solo se limitaba a recordar por pantalla.
  - Registra los metadatos del snapshot en la coleccion `erp_snapshots`.

Uso
---
    python importar_asientos_mongo.py                      # snapshot por defecto
    python importar_asientos_mongo.py --dry-run            # no toca Mongo
    python importar_asientos_mongo.py --snapshot otro.json
    python importar_asientos_mongo.py --sin-retirar-anteriores

Solo usa la biblioteca estandar y la CLI de `docker compose`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

RAIZ = Path(__file__).resolve().parent

SNAPSHOT_POR_DEFECTO = RAIZ / "data" / "erp_snapshot.json"

COLECCION_ASIENTOS = "asientos"
COLECCION_SNAPSHOTS = "erp_snapshots"

CLAVE_ASIENTOS = "asientos"

# El validador de `asientos` (docker/mongosh/02-schema-init.js:236) los marca
# como obligatorios; se comprueban aqui para dar un error legible.
CAMPOS_REQUERIDOS = (
    "_id",
    "asiento_id",
    "snapshot_id",
    "nif",
    "pedido",
    "importe",
    "estado",
    "vigente",
)

ESTADOS_VALIDOS = frozenset({"PENDIENTE", "PAGADA"})

# Los importes del ERP son cantidades con dos decimales; se fijan a esa escala
# para evitar el ruido de la coma flotante al pasarlos a Decimal.
ESCALA_IMPORTE = Decimal("0.01")

# Envoltorios de Extended JSON: son la unica forma de forzar el tipo BSON en
# `mongoimport`, que si no infiere `double` para los numeros y `string` para
# las fechas -> el validador de `asientos` los rechazaria con codigo 121.
BSON_FECHA = "$date"
BSON_DECIMAL = "$numberDecimal"
BSON_ENTERO = "$numberInt"

# Campos de metadatos del snapshot que se registran con tipo BSON explicito.
CAMPOS_ENTEROS_META = (
    "total_asientos",
    "asientos_descargados",
    "paginas",
    "duracion_ms",
    "esquema_version",
)


class ErrorImportacion(Exception):
    """Fallo previsto y explicable de la importacion."""


# ---------------------------------------------------------------------------
# Lectura de datos
# ---------------------------------------------------------------------------


def leer_env(ruta: Path) -> dict[str, str]:
    """Lee un `.env` sencillo (`CLAVE=valor`).

    Se evita `python-dotenv` a proposito: este proyecto es stdlib-only.
    """
    valores: dict[str, str] = {}
    if not ruta.is_file():
        return valores
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        limpia = linea.strip()
        if not limpia or limpia.startswith("#") or "=" not in limpia:
            continue
        clave, _, valor = limpia.partition("=")
        valores[clave.strip()] = valor.strip().strip('"').strip("'")
    return valores


def cargar_snapshot(ruta: Path) -> dict:
    try:
        with ruta.open(encoding="utf-8") as manejador:
            datos = json.load(manejador)
    except FileNotFoundError as error:
        raise ErrorImportacion(f"no existe el snapshot {ruta}") from error
    except json.JSONDecodeError as error:
        raise ErrorImportacion(f"{ruta} no es JSON valido: {error}") from error

    if not isinstance(datos, dict):
        raise ErrorImportacion(
            f"la raiz de {ruta} es un {type(datos).__name__}, no un objeto: "
            "el snapshot esperado tiene metadatos y la clave 'asientos'"
        )
    if not isinstance(datos.get(CLAVE_ASIENTOS), list):
        raise ErrorImportacion(f"{ruta} no trae la clave '{CLAVE_ASIENTOS}' como lista")
    return datos


# ---------------------------------------------------------------------------
# Conversion de tipos
# ---------------------------------------------------------------------------


def fecha_a_extended_json(valor: object) -> object:
    """`"2026-03-21"` -> `{"$date": "2026-03-21T00:00:00Z"}`.

    El validador admite `date` o `null`, asi que un valor ausente o vacio se
    emite como `null` en lugar de reventar.
    """
    if isinstance(valor, str) and valor.strip():
        return {BSON_FECHA: f"{valor.strip()}T00:00:00Z"}
    return None


def importe_a_extended_json(valor: object) -> dict[str, str]:
    try:
        exacto = Decimal(str(valor)).quantize(ESCALA_IMPORTE)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ErrorImportacion(f"importe ilegible ({valor!r})") from error
    return {BSON_DECIMAL: format(exacto, "f")}


def convertir_a_bson(asiento: dict) -> dict:
    """Aplica al asiento los tipos BSON que exige el validador de `asientos`."""
    documento = dict(asiento)
    documento["fecha"] = fecha_a_extended_json(documento.get("fecha"))
    documento["importe"] = importe_a_extended_json(documento.get("importe"))
    documento["esquema_version"] = {BSON_ENTERO: str(int(documento.get("esquema_version", 1)))}
    return documento


def metadatos_a_bson(snapshot: dict) -> dict:
    """Los metadatos del snapshot, sin el array de asientos y con tipos BSON."""
    documento = {clave: valor for clave, valor in snapshot.items() if clave != CLAVE_ASIENTOS}
    descargado = documento.get("descargado_en")
    if isinstance(descargado, str) and descargado.strip():
        documento["descargado_en"] = {BSON_FECHA: descargado.strip()}
    for campo in CAMPOS_ENTEROS_META:
        if isinstance(documento.get(campo), int):
            documento[campo] = {BSON_ENTERO: str(documento[campo])}
    reintentos = documento.get("reintentos")
    if isinstance(reintentos, dict):
        documento["reintentos"] = {
            clave: {BSON_ENTERO: str(valor)} for clave, valor in reintentos.items()
        }
    return documento


# ---------------------------------------------------------------------------
# Validacion previa
# ---------------------------------------------------------------------------


def revisar_asiento(asiento: object) -> list[str]:
    """Devuelve los problemas de un asiento, en el vocabulario del validador."""
    if not isinstance(asiento, dict):
        return [f"no es un objeto sino un {type(asiento).__name__}"]

    ident = asiento.get("asiento_id", "<sin asiento_id>")
    problemas = [
        f"falta '{campo}'" for campo in CAMPOS_REQUERIDOS if asiento.get(campo) is None
    ]

    estado = asiento.get("estado")
    if estado is not None and estado not in ESTADOS_VALIDOS:
        problemas.append(f"estado {estado!r} fuera de {sorted(ESTADOS_VALIDOS)}")

    if not isinstance(asiento.get("vigente"), bool):
        problemas.append("'vigente' no es booleano")

    importe = asiento.get("importe")
    if importe is not None:
        try:
            Decimal(str(importe))
        except (InvalidOperation, ValueError, TypeError):
            problemas.append(f"importe ilegible ({importe!r})")

    return [f"{ident}: {problema}" for problema in problemas]


def revisar_asientos(asientos: list) -> list[str]:
    problemas: list[str] = []
    for asiento in asientos:
        problemas.extend(revisar_asiento(asiento))
    return problemas


# ---------------------------------------------------------------------------
# Puente con docker / Mongo
# ---------------------------------------------------------------------------


def ejecutar(comando: list[str], entrada: bytes | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            comando,
            cwd=RAIZ,
            input=entrada,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as error:
        raise ErrorImportacion(
            f"no se ha podido ejecutar {comando[0]!r}: hace falta la CLI de docker en el PATH"
        ) from error


def _texto(resultado: subprocess.CompletedProcess) -> tuple[str, str]:
    salida = resultado.stdout.decode("utf-8", "replace").strip()
    errores = resultado.stderr.decode("utf-8", "replace").strip()
    return salida, errores


def credenciales(args: argparse.Namespace) -> tuple[str, str, str]:
    entorno = leer_env(RAIZ / ".env")
    usuario = args.usuario or entorno.get("MONGO_ROOT_USER") or os.environ.get("MONGO_ROOT_USER")
    clave = args.clave or entorno.get("MONGO_ROOT_PASSWORD") or os.environ.get(
        "MONGO_ROOT_PASSWORD"
    )
    base = (
        args.base_datos
        or entorno.get("MONGO_DB")
        or os.environ.get("MONGO_DB")
        or "albertitos"
    )
    if not usuario or not clave:
        raise ErrorImportacion(
            "faltan credenciales: define MONGO_ROOT_USER y MONGO_ROOT_PASSWORD en "
            "maisa/.env o pasalas con --usuario / --clave"
        )
    return usuario, clave, base


def importar_ndjson(
    documentos: list[dict],
    coleccion: str,
    args: argparse.Namespace,
    usuario: str,
    clave: str,
    base: str,
) -> int:
    """Manda `documentos` como NDJSON a `mongoimport` dentro del contenedor.

    Se escribe por **stdin** y no por un fichero temporal a proposito: PowerShell
    trunca los here-strings de varias lineas al pasarlos por una tuberia a
    `docker compose exec`, y 516 lineas de NDJSON no sobrevivirian.
    """
    carga = "\n".join(json.dumps(documento, ensure_ascii=False) for documento in documentos)
    comando = [
        "docker",
        "compose",
        "exec",
        "-T",
        "mongo",
        "mongoimport",
        "--host",
        "127.0.0.1",
        "--port",
        str(args.puerto),
        "--username",
        usuario,
        "--password",
        clave,
        "--authenticationDatabase",
        "admin",
        "--db",
        base,
        "--collection",
        coleccion,
        "--mode=upsert",
        "--upsertFields=_id",
    ]
    resultado = ejecutar(comando, entrada=carga.encode("utf-8"))
    salida, errores = _texto(resultado)
    if resultado.returncode != 0:
        raise ErrorImportacion(
            f"mongoimport fallo sobre '{coleccion}' (codigo {resultado.returncode}): "
            f"{errores or salida}"
        )
    if not args.quiet:
        # mongoimport escribe su resumen en stderr, no en stdout.
        resumen = (errores or salida or "(sin salida)").splitlines()[-1].strip()
        print(f"    {coleccion:14} {resumen}")
    return resultado.returncode


def consultar(js: str, args: argparse.Namespace, usuario: str, clave: str, base: str) -> dict:
    """Ejecuta un `--eval` de una sola linea y devuelve el JSON que imprime."""
    comando = [
        "docker",
        "compose",
        "exec",
        "-T",
        "mongo",
        "mongosh",
        "--host",
        "127.0.0.1",
        "--port",
        str(args.puerto),
        "--username",
        usuario,
        "--password",
        clave,
        "--authenticationDatabase",
        "admin",
        base,
        "--quiet",
        "--eval",
        js,
    ]
    resultado = ejecutar(comando)
    salida, errores = _texto(resultado)
    if resultado.returncode != 0:
        raise ErrorImportacion(f"mongosh fallo: {errores or salida}")
    for linea in reversed(salida.splitlines()):
        candidata = linea.strip()
        if candidata.startswith("{"):
            try:
                return json.loads(candidata)
            except json.JSONDecodeError:
                continue
    raise ErrorImportacion(f"mongosh no devolvio JSON interpretable: {salida!r}")


# ---------------------------------------------------------------------------
# Orquestacion
# ---------------------------------------------------------------------------


def retirar_vigencia_anterior(
    snapshot_id: str, args: argparse.Namespace, usuario: str, clave: str, base: str
) -> int:
    """Marca `vigente: false` en los asientos de snapshots distintos al actual."""
    js = (
        'print(JSON.stringify({modificados: db.%s.updateMany('
        '{snapshot_id: {$ne: "%s"}}, {$set: {vigente: false}}).modifiedCount}))'
        % (COLECCION_ASIENTOS, snapshot_id)
    )
    resultado = consultar(js, args, usuario, clave, base)
    return int(resultado.get("modificados", 0))


def verificar(
    snapshot_id: str,
    args: argparse.Namespace,
    usuario: str,
    clave: str,
    base: str,
) -> dict:
    js = (
        'print(JSON.stringify({'
        'asientos: db.%s.countDocuments(),'
        'vigentes: db.%s.countDocuments({vigente: true}),'
        'del_snapshot: db.%s.countDocuments({snapshot_id: "%s"}),'
        'claves: db.%s.distinct("clave_factura").length,'
        'snapshots: db.%s.countDocuments()}))'
        % (
            COLECCION_ASIENTOS,
            COLECCION_ASIENTOS,
            COLECCION_ASIENTOS,
            snapshot_id,
            COLECCION_ASIENTOS,
            COLECCION_SNAPSHOTS,
        )
    )
    return consultar(js, args, usuario, clave, base)


def informar(resultado: dict, esperado: int, snapshot_id: str) -> list[str]:
    """Comprueba los invariantes de la importacion. Devuelve los fallos."""
    fallos: list[str] = []
    if resultado.get("del_snapshot") != esperado:
        fallos.append(
            f"del snapshot '{snapshot_id}' hay {resultado.get('del_snapshot')} asientos, "
            f"se esperaban {esperado}"
        )
    if resultado.get("vigentes") != esperado:
        fallos.append(
            f"vigentes hay {resultado.get('vigentes')}, se esperaban {esperado}"
        )
    if resultado.get("claves") != esperado:
        fallos.append(
            f"claves_factura distintas hay {resultado.get('claves')}, se esperaban {esperado} "
            "(¿se han colado asientos de otro snapshot?)"
        )
    return fallos


def cargar(
    documentos: list[dict],
    metadatos: dict,
    args: argparse.Namespace,
    snapshot_id: str,
    usuario: str,
    clave: str,
    base: str,
) -> dict:
    """Vuelca asientos y metadatos, retira la vigencia anterior y verifica."""
    importar_ndjson(documentos, COLECCION_ASIENTOS, args, usuario, clave, base)
    importar_ndjson([metadatos], COLECCION_SNAPSHOTS, args, usuario, clave, base)
    if args.sin_retirar_anteriores:
        print("    vigencia   no se han retirado los snapshots anteriores")
    else:
        modificados = retirar_vigencia_anterior(snapshot_id, args, usuario, clave, base)
        print(f"    vigencia   {modificados} asiento(s) de snapshots previos retirados")
    return verificar(snapshot_id, args, usuario, clave, base)


def informar_fallos(fallos: list[str]) -> None:
    for fallo in fallos:
        print(f"  FALLO: {fallo}", file=sys.stderr)


def parsear_argumentos(argv: list[str] | None = None) -> argparse.Namespace:
    analizador = argparse.ArgumentParser(
        description="Carga el snapshot del ERP en la coleccion 'asientos' de MongoDB.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    analizador.add_argument(
        "--snapshot",
        type=Path,
        default=SNAPSHOT_POR_DEFECTO,
        help="snapshot JSON generado por descargar_erp.py",
    )
    analizador.add_argument("--servicio", default="mongo", help="servicio de docker compose")
    analizador.add_argument("--puerto", type=int, default=27017, help="puerto de mongod")
    analizador.add_argument("--base-datos", default=None, help="base de datos destino")
    analizador.add_argument("--usuario", default=None, help="usuario de Mongo (por defecto, .env)")
    analizador.add_argument("--clave", default=None, help="contrasena de Mongo (por defecto, .env)")
    analizador.add_argument(
        "--sin-retirar-anteriores",
        action="store_true",
        help="no marcar vigente:false en los asientos de snapshots previos",
    )
    analizador.add_argument(
        "--dry-run",
        action="store_true",
        help="solo validar y resumir; no escribe nada en Mongo",
    )
    analizador.add_argument("--quiet", action="store_true", help="menos salida por pantalla")
    return analizador.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parsear_argumentos(argv)
    inicio = time.monotonic()
    ruta = args.snapshot if args.snapshot.is_absolute() else (RAIZ / args.snapshot)

    print(f"snapshot   {ruta}")
    try:
        snapshot = cargar_snapshot(ruta)
    except ErrorImportacion as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    asientos = snapshot[CLAVE_ASIENTOS]
    snapshot_id = str(snapshot.get("snapshot_id", ""))
    print(f"snapshot_id {snapshot_id or '<sin snapshot_id>'}")
    print(f"asientos   {len(asientos)}")

    problemas = revisar_asientos(asientos)
    if problemas:
        print(f"\nERROR: {len(problemas)} asiento(s) no pasarian el validador:", file=sys.stderr)
        for problema in problemas[:20]:
            print(f"  {problema}", file=sys.stderr)
        if len(problemas) > 20:
            print(f"  ... y {len(problemas) - 20} mas", file=sys.stderr)
        return 1
    if not problemas and not args.quiet:
        print("validacion ok  (campos obligatorios, estados e importes)")

    documentos = [convertir_a_bson(asiento) for asiento in asientos]
    metadatos = metadatos_a_bson(snapshot)

    if args.dry_run:
        print(f"\n--dry-run: nada se ha escrito en Mongo ({len(documentos)} documentos listos)")
        print("ejemplo del primero:")
        print(f"  {json.dumps(documentos[0], ensure_ascii=False)}")
        return 0

    try:
        usuario, clave, base = credenciales(args)
    except ErrorImportacion as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    print(f"destino    docker compose exec {args.servicio} -> {base}")
    try:
        resultado = cargar(documentos, metadatos, args, snapshot_id, usuario, clave, base)
    except ErrorImportacion as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        return 2

    print("\nverificacion")
    for etiqueta, valor in resultado.items():
        print(f"  {etiqueta:14} {valor}")

    fallos = informar(resultado, len(asientos), snapshot_id)
    if fallos:
        informar_fallos(fallos)
        return 2

    duracion_ms = (time.monotonic() - inicio) * 1000
    print(
        f"\nOK  {base}.{COLECCION_ASIENTOS}  ({len(asientos)} asientos de '{snapshot_id}', "
        f"{duracion_ms:.0f} ms)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
