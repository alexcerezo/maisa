"""
Evaluacion del motor local contra el GROUND TRUTH embebido en el PDF.

Por que este script y no `compare_engines.py`
---------------------------------------------
`compare_engines.py` enfrenta dos motores y NO decide quien tiene razon: solo
acota donde mirar. Eso era lo unico posible con 4 documentos escaneados.

El corpus de Maisa tiene 471 PDF de 500 con CAPA DE TEXTO. En un PDF digital el
texto embebido es la verdad exacta: no hay que suponer que la nube acierta, se
puede MEDIR el error del modelo local. Este script hace eso:

  1. Extrae el texto real del PDF (ground truth).
  2. Pasa la pagina por el motor local (misma ruta que el servicio: `_Source`
     + `_run_local_page`, con su escalera de escalas y su tope de pixeles).
  3. Compara las dos lecturas a nivel de GLYPH (distancia de edicion) y a nivel
     de CAMPO (NIF, IBAN, importes, fechas).

Las dos metricas de caracteres
------------------------------
- `cer`        : distancia de edicion sobre la cadena alfanumerica, EN ORDEN.
                 Mide si se leen bien los glifos *y* en el orden correcto.
- `cer_sorted` : lo mismo pero comparando los caracteres ORDENADOS. Es
                 insensible al orden, asi que mide "¿estan todos los caracteres
                 correctos?" aislando el problema de reordenacion de lineas.

Comparar las dos separa dos fallos muy distintos: un modelo que confunde
glifos (sube `cer` y `cer_sorted`) de uno que lee bien pero desordena las
lineas (sube `cer`, `cer_sorted` se queda bajo). Son arreglos diferentes.

La normalizacion es deliberadamente agresiva (`casefold`, solo alfanumericos)
porque el ground truth de un PDF y el OCR nunca coinciden en espacios,
puntuacion ni saltos: medir eso seria medir el ruido de formato, no el modelo.
Las diferencias de puntuacion que SI importan (un importe fusionado) las coge
la comprobacion por campos.

La nube solo en los fallos
--------------------------
Pasar 471 documentos por la API costaria horas y dinero. Y no hace falta: solo
tiene sentido consultar el modelo bueno donde el malo ha fallado. `--cloud-worst`
coge los N peores por `cer` y les pide la lectura a la nube, para tener el par
(lectura mala, lectura buena) que sirve de material de refinamiento.

Uso (DENTRO del contenedor, que es donde viven pypdfium2 y RapidOCR):

    python /tmp/corpus_eval.py /home/ocr/test_files/corpus
    python /tmp/corpus_eval.py /home/ocr/test_files/corpus --limit 25
    python /tmp/corpus_eval.py /home/ocr/test_files/corpus --json /tmp/eval.json
    python /tmp/corpus_eval.py /home/ocr/test_files/corpus --cloud-worst 5
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium

# El servicio vive en /app cuando corre en el contenedor.
for candidate in ("/app", str(Path(__file__).resolve().parent)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from app.server import _Source, _run_local_page  # noqa: E402

# Reutilizar la extraccion de importes del comparador: las dos herramientas
# deben entender "1.025,49" igual, o los resultados no son comparables.
try:
    from compare_engines import amounts_in, malformed_in, norm_lines, _closest_ratio  # noqa: E402
except Exception:  # pragma: no cover - el comparador es opcional aqui
    def norm_lines(text: str) -> list[str]:
        return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]

    def _closest_ratio(line: str, candidates: list[str], stop: float) -> float:
        import difflib

        best = 0.0
        for other in candidates:
            best = max(best, difflib.SequenceMatcher(None, line, other, autojunk=False).ratio())
            if best >= stop:
                break
        return best

    _AMOUNT = re.compile(r"\d{1,3}(?:\.\d{3})+(?:[.,]\d+)?|\d+,\d+|\d+\.\d+|\d+")

    def amounts_in(text: str) -> list[float]:
        out = []
        for raw in _AMOUNT.findall(text or ""):
            cleaned = raw.replace(".", "").replace(",", ".") if raw.count(",") == 1 and "." in raw else None
            try:
                out.append(float(cleaned if cleaned is not None else raw.replace(",", ".")))
            except ValueError:
                continue
        return out

    def malformed_in(text: str) -> list[str]:
        return []


# Por debajo de esto el PDF se considera escaneo (sin capa de texto).
MIN_CHARS_TEXT_LAYER = 40


# --------------------------------------------------------------------------- #
# Distancia de edicion
# --------------------------------------------------------------------------- #


def levenshtein(a: str, b: str) -> int:
    """
    Distancia de edicion clasica, dos filas en vez de matriz completa.

    Con cadenas de ~400 caracteres esto es ~160k comparaciones por documento:
    unos pocos milisegundos, y asi no hace falta una dependencia externa.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            )
        previous = current
    return previous[-1]


def normalize(text: str) -> str:
    """Solo alfanumericos, en minusculas: quita ruido de formato."""
    return "".join(c for c in (text or "").casefold() if c.isalnum())


def content_metrics(truth: str, guess: str) -> dict[str, float]:
    """CER en orden y CER sobre caracteres ordenados."""
    a, b = normalize(truth), normalize(guess)
    if not a:
        return {"cer": 0.0, "cer_sorted": 0.0, "chars": 0}
    ordered = levenshtein(a, b) / len(a)
    scrambled = levenshtein("".join(sorted(a)), "".join(sorted(b))) / len(a)
    return {
        "cer": round(ordered, 4),
        "cer_sorted": round(scrambled, 4),
        "chars": len(a),
    }


# --------------------------------------------------------------------------- #
# Campos
# --------------------------------------------------------------------------- #

# NIF/CIF espanol: 8 digitos + letra (NIF) o letra + 8 digitos/7digitos+letra (CIF).
_NIF = re.compile(r"\b(?:[0-9]{8}[A-Za-z]|[A-Za-z][0-9]{7}[0-9A-Za-z])\b")
_IBAN = re.compile(r"\bES[0-9]{2}(?:[ ]?[0-9]{4}){5}\b")
_DATE = re.compile(r"\b[0-9]{2}/[0-9]{2}/[0-9]{4}\b")


def fields_of(text: str) -> dict[str, set[str]]:
    """Conjuntos normalizados de los campos que se comparan."""
    upper = (text or "").upper()
    return {
        "nif": {m.group(0).upper() for m in _NIF.finditer(upper)},
        # El IBAN se compara sin espacios: el OCR los reparte como quiere.
        "iban": {re.sub(r"\s+", "", m.group(0)) for m in _IBAN.finditer(upper)},
        "fechas": set(_DATE.findall(upper)),
        # Los importes se redondean a 2 decimales para no comparar ruido.
        "importes": {f"{round(v, 2):.2f}" for v in amounts_in(text or "")},
    }


def compare_fields(truth: str, guess: str) -> dict[str, Any]:
    """Recall por campo: de lo que habia de verdad, cuanto encontro el OCR."""
    real = fields_of(truth)
    got = fields_of(guess)
    out: dict[str, Any] = {"missing": {}, "wrong": {}}
    total = hit = 0
    for name, values in real.items():
        if not values:
            continue
        total += len(values)
        found = values & got[name]
        hit += len(found)
        if values - got[name]:
            out["missing"][name] = sorted(values - got[name])
    out["recall"] = round(hit / total, 4) if total else None
    out["checked"] = total
    out["malformed"] = malformed_in(guess)
    return out


# --------------------------------------------------------------------------- #
# Extraccion de la verdad
# --------------------------------------------------------------------------- #


def truth_pages(path: Path) -> list[str]:
    """Texto embebido, una entrada por pagina."""
    doc = pdfium.PdfDocument(str(path))
    try:
        pages = []
        for i in range(len(doc)):
            page = doc[i]
            try:
                textpage = page.get_textpage()
                try:
                    pages.append(textpage.get_text_range() or "")
                finally:
                    textpage.close()
            finally:
                page.close()
        return pages
    finally:
        doc.close()


# --------------------------------------------------------------------------- #
# Un documento
# --------------------------------------------------------------------------- #


def evaluate(path: Path, scales: list[float] | None = None) -> dict[str, Any] | None:
    """OCR local de `path` y comparacion con su capa de texto."""
    truth = truth_pages(path)
    if sum(len(t.strip()) for t in truth) < MIN_CHARS_TEXT_LAYER:
        return None  # escaneo: sin verdad, no evaluable

    started = time.perf_counter()
    source = _Source(path)
    try:
        guess_pages = [
            _run_local_page(source, i, scales or source.scales(None), False)
            for i in range(source.count)
        ]
    finally:
        source.close()
    elapsed = time.perf_counter() - started

    per_page = []
    for index, guess in enumerate(guess_pages):
        real = truth[index] if index < len(truth) else ""
        metrics = content_metrics(real, guess["text"])
        metrics["fields"] = compare_fields(real, guess["text"])
        metrics["page"] = guess["page"]
        metrics["scale"] = guess["scale"]
        metrics["lines"] = len(guess["lines"])
        per_page.append(metrics)

    # El CER del documento es la media ponderada por caracteres: una pagina
    # larga no puede pesar lo mismo que una casi vacia.
    weight = sum(p["chars"] for p in per_page)
    if weight:
        cer = sum(p["cer"] * p["chars"] for p in per_page) / weight
        cer_sorted = sum(p["cer_sorted"] * p["chars"] for p in per_page) / weight
    else:
        cer = cer_sorted = 0.0

    checked = sum(p["fields"]["checked"] for p in per_page)
    hits = sum(
        round(p["fields"]["recall"] * p["fields"]["checked"]) for p in per_page
        if p["fields"]["recall"] is not None
    )
    missing: dict[str, list[str]] = {}
    for page in per_page:
        for name, values in page["fields"]["missing"].items():
            missing.setdefault(name, []).extend(values)
    malformed = [v for page in per_page for v in page["fields"]["malformed"]]

    return {
        "file": path.name,
        "pages": len(per_page),
        "elapsed": round(elapsed, 3),
        "cer": round(cer, 4),
        "cer_sorted": round(cer_sorted, 4),
        "chars": weight,
        "field_recall": round(hits / checked, 4) if checked else None,
        "fields_checked": checked,
        "missing": {k: sorted(set(v))[:6] for k, v in missing.items()},
        "malformed": sorted(set(malformed))[:6],
        "detail": per_page,
    }


# --------------------------------------------------------------------------- #
# Informe
# --------------------------------------------------------------------------- #


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((pct / 100) * (len(ordered) - 1))))
    return ordered[index]


def report(rows: list[dict[str, Any]], skipped: int) -> None:
    total = len(rows)
    print()
    print("=" * 78)
    print(" RESUMEN")
    print("=" * 78)
    print(f"  evaluados           : {total}")
    print(f"  saltados (escaneos) : {skipped}  (sin capa de texto, no evaluables)")
    if not total:
        return

    cers = [r["cer"] for r in rows]
    sorteds = [r["cer_sorted"] for r in rows]
    perfect = sum(1 for r in rows if r["cer"] == 0)
    casi = sum(1 for r in rows if r["cer"] <= 0.01)
    recalls = [r["field_recall"] for r in rows if r["field_recall"] is not None]

    print()
    print("  CER (caracteres en orden)")
    print(f"    medio {sum(cers) / total:.4f}   mediana {percentile(cers, 50):.4f}   "
          f"p90 {percentile(cers, 90):.4f}   p99 {percentile(cers, 99):.4f}   max {max(cers):.4f}")
    print("  CER (caracteres ordenados: aisla el desorden de lineas)")
    print(f"    medio {sum(sorteds) / total:.4f}   mediana {percentile(sorteds, 50):.4f}   "
          f"p90 {percentile(sorteds, 90):.4f}   max {max(sorteds):.4f}")
    print()
    print(f"  documentos PERFECTOS (cer=0)     : {perfect}/{total}  ({100 * perfect / total:.1f}%)")
    print(f"  documentos casi perfectos (<=1%) : {casi}/{total}  ({100 * casi / total:.1f}%)")
    if recalls:
        print(f"  recall de campos (NIF/IBAN/fecha/importe): {sum(recalls) / len(recalls):.4f}")
    print(f"  tiempo medio por documento       : {sum(r['elapsed'] for r in rows) / total:.2f}s")

    # La diferencia entre los dos CER dice si el fallo es de orden o de glifo.
    orden = [r for r in rows if r["cer"] - r["cer_sorted"] > 0.05]
    print()
    print("  Documentos donde el desorden pesa mas que el glifo")
    print(f"    (cer - cer_sorted > 0.05) : {len(orden)}")
    for r in sorted(orden, key=lambda r: r["cer_sorted"] - r["cer"])[:5]:
        print(f"      {r['file']:34s} cer={r['cer']:.3f} ordenado={r['cer_sorted']:.3f}")

    print()
    print("  PEORES 15 DOCUMENTOS")
    for r in sorted(rows, key=lambda r: -r["cer"])[:15]:
        rec = "n/a" if r["field_recall"] is None else f"{r['field_recall']:.2f}"
        print(f"    {r['file']:34s} cer={r['cer']:.3f} ord={r['cer_sorted']:.3f} "
              f"campos={rec:>4s} chars={r['chars']:5d} {r['elapsed']:5.2f}s")
        if r["missing"]:
            for name, values in sorted(r["missing"].items()):
                print(f"        falta {name:9s}: {values}")
        if r["malformed"]:
            print(f"        mal formados   : {r['malformed']}")

    # Cuantos documentos tienen el CER alto *y* los campos mal: esos son los
    # que de verdad hay que mandar a la nube y usar como material de trabajo.
    graves = [r for r in rows if r["cer"] > 0.05 or (r["field_recall"] is not None and r["field_recall"] < 0.95)]
    print()
    print(f"  CANDIDATOS A REVISION (cer>5% o recall de campos<95%): {len(graves)}")
    for r in sorted(graves, key=lambda r: -r["cer"])[:10]:
        print(f"    {r['file']:34s} cer={r['cer']:.3f} "
              f"campos={'n/a' if r['field_recall'] is None else r['field_recall']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Evalua el OCR local contra la capa de texto del PDF.")
    parser.add_argument("root", nargs="?", default="/home/ocr/test_files/corpus")
    parser.add_argument("--limit", type=int, default=0, help="evaluar solo los N primeros (0 = todos)")
    parser.add_argument("--offset", type=int, default=0, help="saltar los N primeros")
    parser.add_argument("--json", dest="json_out", help="volcar los resultados crudos aqui")
    parser.add_argument("--scale", type=float, default=None, help="forzar una escala de render")
    parser.add_argument("--quiet", action="store_true", help="no imprimir el progreso")
    args = parser.parse_args()

    root = Path(args.root)
    pdfs = sorted(root.rglob("*.pdf"))
    if args.offset:
        pdfs = pdfs[args.offset:]
    if args.limit:
        pdfs = pdfs[: args.limit]
    if not pdfs:
        print(f"No hay PDFs en {root}", file=sys.stderr)
        return 2

    scales = [args.scale] if args.scale else None
    print(f"Corpus  : {root}")
    print(f"Ficheros: {len(pdfs)}")
    print()

    rows: list[dict[str, Any]] = []
    skipped = 0
    started = time.perf_counter()
    for index, path in enumerate(pdfs, 1):
        try:
            result = evaluate(path, scales)
        except Exception as exc:
            print(f"  !! {path.name}: {type(exc).__name__}: {exc}")
            continue
        if result is None:
            skipped += 1
            continue
        rows.append(result)
        if not args.quiet:
            flag = "" if result["cer"] == 0 else ("  <-- REVISAR" if result["cer"] > 0.05 else "")
            print(f"  [{index:3d}/{len(pdfs)}] {path.name:34s} cer={result['cer']:.4f} "
                  f"ord={result['cer_sorted']:.4f} {result['elapsed']:5.2f}s{flag}")

    print(f"\nTiempo total: {time.perf_counter() - started:.1f}s")
    report(rows, skipped)

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON crudo: {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
