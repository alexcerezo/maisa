#!/usr/bin/env python3
"""Congela la API viva de Albertitos en `public/data/` — los fixtures del panel.

Por que existe
--------------
El panel tiene que seguir ensenando la conciliacion aunque la API no responda:
wifi de hackathon, VM apagada, tunel caido, alguien abriendo el enlace a las 3 de
la manana. Cuando `/api/estadisticas` no contesta, el frontend cae a estos
ficheros y la demo sigue contando lo mismo.

Los ficheros NO se inventan aqui: se copian tal cual de la API. Si hay que
ensenar el sistema a alguien, esto es la prueba de que el JSON que pinta la
pantalla es el que devuelve el motor de verdad.

Que escribe (todo bajo `--out`, por defecto `public/data/`)
-----------------------------------------------------------
    manifiesto.json          cuando se congelo, de donde y cuantos. Trazabilidad.
    estadisticas.json        GET /api/estadisticas      -> los contadores
    facturas.json            GET /api/facturas?limit=500 -> la tabla entera
    facturas/<slug>.json     GET /api/facturas/{file_id} -> 500 detalles
    pdfs/<slug>              unos pocos PDFs, para el <iframe> sin API

Por que los detalles van en ficheros sueltos: el detalle solo se pide cuando
alguien abre UNA factura. Un unico `detalles.json` de 1,5 MB obligaria a
descargarlo entero para ver la primera, y el congelado existe justo para cuando
la red va mal.

El `<slug>` es el `file_id` con todo lo que no sea `[A-Za-z0-9._-]` escapado como
`~XXXX` (el code point en hexa). Hace falta porque 65 de las 500 facturas traen
acentos (`FA-2116_mensajeria.pdf`) y un nombre con acentos en la URL se codifica,
se descodifica y acaba sin encontrar el fichero. El escapado es reversible y no
colisiona, porque `~` nunca aparece tal cual. La version TypeScript de esta misma
funcion vive en `src/api/fixtures.ts` y tiene que dar el mismo resultado.

Los PDF congelados se guardan con el slug TAL CUAL, sin anadirle extension: el
slug ya conserva la del original (`.` y los alfanumericos no se escapan), asi que
`scan_002.pdf` queda como `pdfs/scan_002.pdf` y no como un `scan_002.pdf.pdf`
que no hay quien mire. A los detalles si se les anade `.json`, y queda
`facturas/scan_002.pdf.json`, que se lee bien.

Uso
---
    python3 tools/descargar_fixtures.py                       # contra la VM
    python3 tools/descargar_fixtures.py --base http://127.0.0.1:8010
    python3 tools/descargar_fixtures.py --no-pdf              # solo JSON

Al terminar imprime el inventario del esquema (claves de `campos`, combinaciones
de `hechos`, enums vistos). Si al regenerarlo cambia algo, la API se ha movido y
los tipos de `src/api/types.ts` hay que revisarlos.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# La misma URL que `public/config.json`. Es el nombre canonico del servicio
# (`82.70.78.22.sslip.io` es un nombre DNS publico que resuelve al IP de la VM,
# asi que hay TLS con certificado valido y no hace falta un dominio propio).
# Ojo: deriva del IP, asi que si Oracle le cambia el IP a la VM, este nombre y su
# certificado dejan de valer a la vez y hay que corregir los dos sitios.
BASE_POR_DEFECTO = "https://82.70.78.22.sslip.io"

# Los PDF que se descargan para que el <iframe> funcione sin API. No son "los
# mejores": son uno de cada caso raro, que es lo que se ensena en la demo.
PDFS = [
    "2026-01-08_P001.pdf",       # PAGAR limpio: el caso aburrido, para contrastar
    "2026-03-28_P002.pdf",       # NO_PAGAR con hecho `duro`: pago duplicado
    "2026-04-08_P007.pdf",       # NO_PAGAR con `sospechosos`: "el erp miente"
    "2026-0233-A_catering.pdf",  # ESCALAR: pedido repetido en el lote
    "2026-06-04_P006.pdf",       # ESCALAR con texto sospechoso en el documento
    "copia_2026_0518.pdf",       # vision_ocr: escaneo ilegible, identidad heredada
    "FA-2508_consultoría.pdf",   # ESCALAR con medio listado a null
    "2026-07-09_P010.pdf",       # `ordenes_resultado`: instrucciones en el PDF
    "scan_002.pdf",              # `identidad_heredada`
]


def slug(file_id: str) -> str:
    """`file_id` -> nombre de fichero seguro en ASCII. Espejo de `slugFileId` (TS)."""
    salida = []
    for ch in file_id:
        if ch.isascii() and (ch.isalnum() or ch in "._-"):
            salida.append(ch)
        else:
            salida.append("~%04x" % ord(ch))
    return "".join(salida)


def pedir(base: str, ruta: str, timeout: float) -> bytes:
    peticion = urllib.request.Request(base + ruta, headers={"Accept": "application/json, */*"})
    with urllib.request.urlopen(peticion, timeout=timeout) as respuesta:
        return respuesta.read()


def escribir(destino: Path, datos: bytes) -> int:
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(datos)
    return len(datos)


def compacto(obj) -> bytes:
    """JSON minificado y con las claves ordenadas: asi el `git diff` no miente.

    Sin ordenar, dos congelados del mismo dato salen distintos solo porque el
    diccionario de Python cambio de orden, y parece que el motor se ha movido.
    """
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def inventario(detalles: list[dict]) -> None:
    """Imprime la forma real del corpus. Es una alarma de deriva del contrato."""
    claves: Counter = Counter()
    combos: Counter = Counter()
    escalones: Counter = Counter()
    metodos: Counter = Counter()
    estados: Counter = Counter()
    reglas: Counter = Counter()
    for detalle in detalles:
        campos = detalle.get("campos", {})
        # `.keys()` no es opcional: `Counter.update` con un dict interpreta los
        # VALORES como recuentos, y aqui los valores son textos y listas.
        claves.update(campos.keys())
        combos[len(campos)] += 1
        lectura = detalle.get("lectura", {})
        escalones[lectura.get("escalon_lectura")] += 1
        metodos[lectura.get("metodo_lectura")] += 1
        estados[campos.get("estado_erp")] += 1
        for hecho in detalle.get("hechos", []):
            reglas[(hecho["regla"], hecho["ok"], hecho["duro"], hecho["informativo"])] += 1

    print("\n--- inventario del esquema ---")
    print("claves de `campos` (union: %d): %s" % (len(claves), ", ".join(sorted(claves))))
    print("tamano de `campos` por factura: %s" % dict(sorted(combos.items())))
    print("escalon_lectura: %s" % dict(escalones))
    print("metodo_lectura : %s" % dict(metodos))
    print("estado_erp     : %s" % dict(estados))
    print("hechos (regla, ok, duro, informativo):")
    for clave in sorted(reglas, key=lambda c: (c[0], c[1], c[2], c[3])):
        print("    %-14s ok=%-5s duro=%-5s info=%-5s -> %d" % (*clave, reglas[clave]))
    sin_pdf = [d["file_id"] for d in detalles if not d.get("campos", {}).get("fecha")]
    if sin_pdf:
        print("facturas sin `fecha` (listado con null): %d, p.ej. %s" % (len(sin_pdf), sin_pdf[0]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=BASE_POR_DEFECTO, help="URL de la API (sin barra final)")
    parser.add_argument("--out", default="public/data", type=Path, help="directorio destino")
    parser.add_argument("--timeout", default=30.0, type=float)
    parser.add_argument("--concurrency", default=8, type=int)
    parser.add_argument("--no-pdf", action="store_true", help="no descargar PDFs")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    out: Path = args.out

    print("API   : %s" % base)
    print("destino: %s" % out.resolve())

    try:
        estadisticas = json.loads(pedir(base, "/api/estadisticas", args.timeout))
    except (urllib.error.URLError, TimeoutError) as exc:
        print("\nLa API no responde en %s: %s" % (base, exc), file=sys.stderr)
        print("Arranca el sistema o pasa --base con la URL buena.", file=sys.stderr)
        return 1

    listado = json.loads(pedir(base, "/api/facturas?limit=500", args.timeout))
    items = listado["items"]
    print("\nlistado: %d facturas (total=%d, limit=%d)" % (len(items), listado["total"], listado["limit"]))

    escribir(out / "estadisticas.json", compacto(estadisticas))
    escribir(out / "facturas.json", compacto(listado))

    def descargar_detalle(file_id: str) -> dict | None:
        try:
            crudo = pedir(base, "/api/facturas/" + urllib.parse.quote(file_id), args.timeout)
        except urllib.error.HTTPError as exc:
            print("  detalle %s -> HTTP %d, se omite" % (file_id, exc.code), file=sys.stderr)
            return None
        escribir(out / "facturas" / (slug(file_id) + ".json"), crudo)
        return json.loads(crudo)

    with futures.ThreadPoolExecutor(args.concurrency) as pool:
        detalles = [d for d in pool.map(descargar_detalle, [i["file_id"] for i in items]) if d]

    print("detalles: %d ficheros" % len(detalles))

    pdfs_ok: list[str] = []
    if not args.no_pdf:
        for file_id in PDFS:
            try:
                crudo = pedir(base, "/api/facturas/" + urllib.parse.quote(file_id) + "/pdf", args.timeout)
            except urllib.error.HTTPError as exc:
                print("  pdf %s -> HTTP %d, se omite" % (file_id, exc.code), file=sys.stderr)
                continue
            escribir(out / "pdfs" / slug(file_id), crudo)
            pdfs_ok.append(file_id)
        print("pdfs: %d de %d" % (len(pdfs_ok), len(PDFS)))

    manifiesto = {
        "congelado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "origen": base,
        "facturas_en_el_listado": len(items),
        "detalles_guardados": len(detalles),
        "pdfs": pdfs_ok,
        "por_resultado": estadisticas.get("por_resultado"),
        "asientos_vigentes": estadisticas.get("asientos_vigentes"),
        "entrega": estadisticas.get("entrega"),
        "generado_por": "tools/descargar_fixtures.py",
    }
    (out / "manifiesto.json").write_text(
        json.dumps(manifiesto, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    inventario(detalles)
    print("\nlisto. Recuerda: si el inventario cambio, revisa src/api/types.ts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
