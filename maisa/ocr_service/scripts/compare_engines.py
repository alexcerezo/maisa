#!/usr/bin/env python3
"""
Compara los dos motores (nube PaddleOCR-VL vs local PP-OCRv5) sobre el MISMO
documento y enseña exactamente donde discrepan.

El objetivo del proyecto es refinar el modelo local usando el bueno como
referencia, asi que lo que interesa no es un "ganador" sino la LISTA DE
DIFERENCIAS. Este script no decide quien tiene razon: acota donde hay que mirar.

Lo que hace:

  1. Pide el documento a la nube con `compare=true`, que devuelve en la misma
     respuesta el OCR local (`local_reference`). Una sola subida, dos lecturas.
  2. Enfrenta el texto pagina a pagina y marca las lineas que solo aparecen en
     uno de los dos motores (candidatas a error de lectura).
  3. Extrae los importes de cada texto y comprueba su coherencia aritmetica
     (subtotal + IVA = total). Una lectura incoherente es casi siempre errónea.
  4. Marca las alucinaciones: alfabeto extrano respecto al resto de la pagina
     (mismo guardia que usa `app/cloud.py`).

Uso (desde el host, contra el contenedor):

    python3 scripts/compare_engines.py                      # todo docs/
    python3 scripts/compare_engines.py docs/scan_001.pdf
    python3 scripts/compare_engines.py --engine local docs/ # solo local
    python3 scripts/compare_engines.py --json out.json docs/
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests

# El guardia de escritura tiene que ser EXACTAMENTE el mismo que usa el servidor:
# si aqui se reimplementara, dejaría de valer como comprobacion.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    from app.cloud import char_script, dominant_script
except Exception:  # pragma: no cover - el script sigue siendo util sin el

    def dominant_script(text: str) -> str | None:  # type: ignore[misc]
        return None

    def char_script(ch: str) -> str:  # type: ignore[misc]
        return "?"


# --------------------------------------------------------------------------- #
# Importes
# --------------------------------------------------------------------------- #

# Orden pensado, de mas especifico a mas generico:
#   "1.025,49" -> miles + decimal   (el decimal puede ser '.' o ',')
#   "1.240.84" -> el total que leyo el motor local, con el punto decimal mal
#   "460,00"   -> decimal con coma
#   "460.00"   -> decimal con punto
#   "460"      -> entero suelto
# El primer grupo DEBE aceptar '.', no solo ',': sin eso "1.240.84" se cortaba
# en "1.240" y el total salia mal (1240.0 en vez de 1240.84), lo que falseaba
# justo la comprobacion aritmetica para la que existe el script.
_AMOUNT = re.compile(
    r"\d{1,3}(?:\.\d{3})+(?:[.,]\d+)?|\d+,\d+|\d+\.\d+|\d+"
)

# Tope de importes por pagina: la comprobacion es cuadratica en `sums` y no
# aporta nada seguir a partir de aqui.
_MAX_AMOUNTS = 24

# Se exige separador decimal. Sin esto, "11/04/2026" aporta 11, 04 y 2026 como
# si fueran importes y el ruido tapa la señal. Contrapartida: un importe escrito
# sin decimales ("Total 460 EUR") no se detecta. Para facturas es buen cambio.
_HAS_DECIMAL = re.compile(r"[.,]\d+$")

# Por debajo de esto un numero casi nunca es un importe de factura (IVA 21, etc.)
_MIN_AMOUNT = 1.0
_MAX_AMOUNT = 10_000_000.0


def parse_amount(raw: str) -> tuple[float | None, int]:
    """
    Convierte un importe en formato espanol a float.

    Devuelve (valor, decimales). `decimales` es -1 si el numero es ambiguo o
    esta mal formado. Reglas:
      - Con `.` y `,` juntos: `.` es de miles y `,` decimal.
      - Solo `.`: el ULTIMO punto es decimal si le siguen 1-2 digitos
        ("1.240.84" -> 1240.84, que es como leyo el motor local el total).
      - Solo `,`: decimal siempre.
    """
    if "," in raw and "." in raw:
        value = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        value = raw.replace(",", ".")
    elif "." in raw:
        head, _, tail = raw.rpartition(".")
        if len(tail) <= 2:
            value = f"{head.replace('.', '')}.{tail}"
        else:
            value = raw.replace(".", "")
    else:
        value = raw

    try:
        number = float(value)
    except ValueError:
        return None, -1

    decimals = len(value.split(".")[1]) if "." in value else 0
    if decimals > 2:
        # "1.025,49112121535": el modelo habia fusionado base e IVA en un solo
        # numero. Es el sintoma mas claro de lectura mala, asi que no se
        # descarta: se devuelve marcado.
        return number, -1
    return number, decimals


def amounts_in(text: str) -> list[float]:
    """
    Importes plausibles de un texto, ORDENADOS PERO SIN DEDUPLICAR.

    La repeticion es la prueba, no el ruido: en el fax, la evidencia de que la
    nube acerto fue ver `460.00` DOS veces y que su suma daba la base imponible.
    Un `set()` se comia la segunda aparicion y con ella la comprobacion.
    """
    found: list[float] = []
    for raw in _AMOUNT.findall(text or ""):
        if not _HAS_DECIMAL.search(raw):
            continue
        value, decimals = parse_amount(raw)
        if value is None or decimals < 0:
            continue
        if _MIN_AMOUNT <= value <= _MAX_AMOUNT:
            found.append(round(value, 2))
    return sorted(found)[:_MAX_AMOUNTS]


def malformed_in(text: str) -> list[str]:
    """Numeros con mas de dos decimales: casi siempre fusion de dos importes."""
    out = []
    for raw in _AMOUNT.findall(text or ""):
        if not _HAS_DECIMAL.search(raw):
            continue
        _, decimals = parse_amount(raw)
        if decimals < 0:
            out.append(raw)
    return sorted(set(out))


def check_arithmetic(values: list[float], tolerance: float = 0.02) -> dict[str, Any]:
    """
    Busca coherencia contable dentro de una lista de importes.

    Dos relaciones tipicas de una factura espanola:
      - suma   : a + b == c          (varios conceptos que dan un subtotal)
      - iva    : a * 1.21 == b       (base imponible + 21% = total)

    No se busca un resultado unico: se informa de todas las relaciones que
    cuadran. Dos lecturas pueden ser coherentes cada una por su lado y aun asi
    discrepar entre si (nos paso: nube 460+460=920, local 460+480=940); en ese
    caso el veredicto necesita una persona y el script lo dice.
    """
    sums: list[dict[str, Any]] = []
    ratios: list[dict[str, Any]] = []
    n = len(values)
    for i in range(n):
        for j in range(i + 1, n):
            pair = values[i] + values[j]
            for k in range(n):
                if k in (i, j):
                    continue
                if abs(pair - values[k]) <= tolerance:
                    sums.append({"a": values[i], "b": values[j], "c": values[k]})
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            if abs(values[i] * 1.21 - values[j]) <= tolerance:
                ratios.append({"base": values[i], "total": values[j], "iva": round(values[j] - values[i], 2)})
    return {
        "amounts": values,
        "sums": sums,
        "iva21": ratios,
        "coherent": bool(sums or ratios),
    }


# --------------------------------------------------------------------------- #
# Texto
# --------------------------------------------------------------------------- #


def norm_lines(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _closest_ratio(line: str, candidates: list[str], stop: float) -> float:
    """Mejor parecido de `line` contra una lista, cortando al alcanzar `stop`."""
    best = 0.0
    for other in candidates:
        best = max(best, difflib.SequenceMatcher(None, line, other, autojunk=False).ratio())
        if best >= stop:
            break
    return best


# Separadores y espacios: indiferentes al comparar contenido.
_SEP = re.compile(r"[\s.,:%|;·\-–—_/\\()\[\]{}'\"»«]+")
# Un fragmento de uno o dos caracteres (resto de una linea partida) nunca
# identifica un error de lectura.
_MIN_FRAGMENT = 3


def _squash(text: str) -> str:
    """Forma canonica de un texto: sin espacios, sin separadores, en minusculas."""
    return _SEP.sub("", (text or "").casefold())


def compare_texts(cloud_text: str, local_text: str, threshold: float = 0.6) -> dict[str, Any]:
    """
    Lineas exclusivas de cada motor + parecido global.

    El emparejamiento es DIFUSO a proposito. Comparando linea exacta contra linea
    exacta salia todo como exclusivo: los dos motores leen las mismas 13 lineas
    del fax con uno o dos caracteres de diferencia, y esa lista de 26 entradas no
    dice nada. Con umbral, "Servicio mensual 460.00" empareja con cualquiera de
    las dos lecturas parecidas y deja de aparecer; lo que queda es contenido que
    SOLO esta en un motor, que es justo lo que hay que revisar (una linea
    alucinada por la nube, un importe que el local no vio).

    El emparejamiento difuso por linea tampoco basta, porque los dos motores
    SEGMENTAN distinto: la nube es un LLM y reescribe la tabla como "Servicio
    mensual 935,00" en una sola linea, mientras que el local parte etiqueta y
    valor en dos. Contra `scan_028.pdf` eso ponia seis importes como "exclusivos
    del local" cuando estaban en las dos lecturas. Por eso hay una segunda
    oportunidad: si el contenido de la linea, despojado de formato, aparece en
    CUALQUIER parte del texto del otro motor, la linea no es exclusiva.

    Las diferencias caracter a caracter no se pierden: estan en el texto
    enfrentado de arriba, que es la vista principal.
    """
    cloud_lines = norm_lines(cloud_text)
    local_lines = norm_lines(local_text)
    cloud_blob = _squash("\n".join(cloud_lines))
    local_blob = _squash("\n".join(local_lines))

    def exclusive(lines: list[str], others: list[str], other_blob: str) -> list[str]:
        out = []
        for line in lines:
            if _closest_ratio(line, others, threshold) >= threshold:
                continue
            key = _squash(line)
            if len(key) < _MIN_FRAGMENT:
                continue
            if key in other_blob:
                continue
            out.append(line)
        return out

    matcher = difflib.SequenceMatcher(None, "\n".join(cloud_lines), "\n".join(local_lines), autojunk=False)
    return {
        "ratio": round(matcher.ratio(), 4),
        "cloud_only": exclusive(cloud_lines, local_lines, local_blob),
        "local_only": exclusive(local_lines, cloud_lines, cloud_blob),
    }


def hallucinations(text: str) -> list[str]:
    """Lineas con alfabeto distinto al dominante del documento."""
    page_script = dominant_script(text)
    if not page_script:
        return []
    out = []
    for line in norm_lines(text):
        letters = [c for c in line if c.isalpha()]
        if not letters:
            continue
        scripts = [char_script(c) for c in letters]
        odd = next((s for s in scripts if s and s != page_script), None)
        if odd and scripts.count(odd) == len(scripts):
            out.append(f"{line}  [escritura {odd!r} en documento {page_script!r}]")
    return out


# --------------------------------------------------------------------------- #
# Llamadas al servicio
# --------------------------------------------------------------------------- #


def call(url: str, path: Path, engine: str, compare: bool, timeout: float) -> dict[str, Any]:
    params = {"engine": engine, "include_boxes": "false"}
    if compare:
        params["compare"] = "true"
    with path.open("rb") as fh:
        resp = requests.post(
            f"{url.rstrip('/')}/ocr",
            params=params,
            files={"file": (path.name, fh)},
            timeout=timeout,
        )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:400]}")
    return resp.json()


def measure(url: str, path: Path, engine: str, compare: bool, timeout: float) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    payload = call(url, path, engine, compare, timeout)
    return payload, time.perf_counter() - started


# --------------------------------------------------------------------------- #
# Informe
# --------------------------------------------------------------------------- #


def _line(width: int = 78) -> str:
    return "=" * width


def _readings(engine: str, cloud_text: str, local_text: str) -> list[tuple[str, str]]:
    """
    Pares (etiqueta, texto) que hay que analizar.

    Con `--engine local` no existe lectura de nube: analizar el mismo texto dos
    veces bajo dos etiquetas solo duplicaria la salida.
    """
    if engine == "local":
        return [("LOCAL", local_text or cloud_text)]
    pairs = [("NUBE ", cloud_text)]
    if local_text:
        pairs.append(("LOCAL", local_text))
    return pairs


def report(path: Path, payload: dict[str, Any], wall: float, remote: dict[str, Any] | None) -> None:
    print()
    print(_line())
    print(f" {path.name}")
    print(_line())

    engine = payload.get("engine", "?")
    stats = payload.get("stats", {}) or {}
    fallback = payload.get("fallback")

    if engine == "cloud":
        print(
            f"  NUBE   job={payload.get('job_id')}  paginas={payload.get('pages')}  "
            f"{wall:.2f}s (api {payload.get('elapsed')}s)"
        )
        print(
            f"         bloques={stats.get('blocks')}  regiones={stats.get('regions')}  "
            f"score region min={stats.get('min_region_score')} medio={stats.get('mean_region_score')}"
        )
        # Se dice explicitamente para que nadie lea "score" y crea que es de texto.
        print(f"         confianza de texto: NO DISPONIBLE ({stats.get('text_confidence_note')})")
        if stats.get("suspect_blocks"):
            print(f"         !! {stats['suspect_blocks']} bloque(s) sospechoso(s): {stats.get('suspect_reason')}")
    else:
        if engine == "local":
            print("  NUBE   no ejecutada (se pidio --engine local)")
        else:
            print("  NUBE   no disponible en esta peticion")
            if fallback:
                print(f"         motivo: [{fallback.get('kind')}] {fallback.get('reason')}")

    if remote:
        rst = remote.get("stats", {}) or {}
        print(
            f"  LOCAL  {rst.get('lines')} lineas  score medio={rst.get('mean_score')} "
            f"min={rst.get('min_score')}  {remote.get('elapsed')}s"
        )

    cloud_text = payload.get("text") or _join_pages(payload, "cloud")
    local_text = (remote or {}).get("text") or _join_pages(remote or {}, "local")

    if engine == "cloud" and not remote:
        cloud_text = cloud_text or _join_pages(payload, "cloud")

    print()
    print("-- texto ----------------------------------------------------------")
    if engine == "local" or not local_text:
        # Una sola lectura: repetirla bajo dos etiquetas confundiria.
        print(_side("LOCAL" if engine == "local" else "NUBE ", cloud_text))
    else:
        print(_side("NUBE ", cloud_text))
        print()
        print(_side("LOCAL", local_text))

    diff = compare_texts(cloud_text, local_text)
    if engine != "local":
        print()
        print(f"-- discrepancias (parecido global {diff['ratio'] * 100:.1f}%) --------------")
        if not local_text:
            print("  no hay segunda lectura con la que comparar")
        elif not diff["cloud_only"] and not diff["local_only"]:
            print("  los dos motores leen exactamente lo mismo")
        for ln in diff["cloud_only"]:
            print(f"  solo NUBE : {ln}")
        for ln in diff["local_only"]:
            print(f"  solo LOCAL: {ln}")

    print()
    print("-- coherencia aritmetica ------------------------------------------")
    for label, text in _readings(engine, cloud_text, local_text):
        values = amounts_in(text)
        check = check_arithmetic(values)
        bad = malformed_in(text)
        print(f"  {label} importes: {values if values else '(ninguno)'}")
        if bad:
            print(f"         !! numeros mal formados (fusion de importes?): {bad}")
        if not check["coherent"]:
            print("         sin relaciones contables que cuadren")
        for rel in check["sums"][:3]:
            print(f"         suma  {rel['a']} + {rel['b']} = {rel['c']}  OK")
        for rel in check["iva21"][:3]:
            print(f"         iva21 {rel['base']} + {rel['iva']} = {rel['total']}  OK")
        if len(check["sums"]) > 3:
            print(f"         (+{len(check['sums']) - 3} sumas mas)")

    print()
    print("-- alucinaciones --------------------------------------------------")
    for label, text in _readings(engine, cloud_text, local_text):
        found = hallucinations(text)
        if found:
            for item in found:
                print(f"  {label}: {item}")
        else:
            print(f"  {label}: ninguna")

    if engine == "cloud" and remote and remote is not payload:
        print()
        print("  Nota: los dos motores pueden ser coherentes cada uno por su lado")
        print("        y aun asi discrepar. Ese caso necesita revision humana;")
        print("        el script solo lo acota.")
    print()


def _join_pages(payload: dict[str, Any], engine: str) -> str:
    parts = []
    for page in payload.get("results", []) or []:
        if engine == "cloud":
            parts.append(page.get("text") or "")
        else:
            parts.append(page.get("text") or "")
    return "\n\n".join(p for p in parts if p).strip()


def _side(label: str, text: str, width: int = 96) -> str:
    lines = norm_lines(text)
    if not lines:
        return f"  {label} | (vacio)"
    out = []
    for i, ln in enumerate(lines):
        tag = label if i == 0 else " " * len(label)
        out.append(f"  {tag} | {ln[:width]}")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def collect(targets: list[str]) -> list[Path]:
    files: list[Path] = []
    for target in targets:
        path = Path(target)
        if path.is_dir():
            files.extend(sorted(p for p in path.iterdir() if p.suffix.lower() in (".pdf", ".png", ".jpg", ".jpeg")))
        elif path.exists():
            files.append(path)
        else:
            print(f"aviso: no existe {path}", file=sys.stderr)
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("targets", nargs="*", default=[str(ROOT / "docs")], help="ficheros o directorios")
    parser.add_argument("--url", default="http://localhost:8866", help="base del servicio OCR")
    parser.add_argument("--engine", default="cloud", choices=["cloud", "local", "auto"], help="motor principal")
    parser.add_argument("--timeout", type=float, default=900.0, help="timeout HTTP en segundos")
    parser.add_argument("--json", dest="json_out", help="volcar tambien el JSON crudo aqui")
    args = parser.parse_args()

    files = collect(args.targets or [str(ROOT / "docs")])
    if not files:
        print("no hay ficheros que comparar", file=sys.stderr)
        return 2

    dump: list[dict[str, Any]] = []
    for path in files:
        try:
            payload, wall = measure(args.url, path, args.engine, True, args.timeout)
        except Exception as exc:
            print(f"\n{path.name}: FALLO -> {exc}", file=sys.stderr)
            continue

        remote = payload.pop("local_reference", None)
        if remote is None and payload.get("engine") == "local":
            # Se pidio solo el motor local: no hay nada que enfrentar, y volver
            # a llamar gastaria el doble de CPU para el mismo texto.
            remote = payload
        elif remote is None:
            # La nube cayo: aun asi se puede enseñar la lectura local sola.
            try:
                remote, _ = measure(args.url, path, "local", False, args.timeout)
            except Exception as exc:
                print(f"  (tampoco se pudo ejecutar el motor local: {exc})", file=sys.stderr)

        report(path, payload, wall, remote)
        dump.append({"file": str(path), "cloud": payload, "local": remote})

    if args.json_out:
        out = Path(args.json_out)
        # `scripts/out/` no existe en un clon recien hecho: sin este `mkdir` el
        # script terminaba bien el trabajo y moria al volcar el JSON.
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON crudo: {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
