"""
Ejecuta OCR sobre documentos reales y genera un informe de texto revisable.

Compara el mismo documento con varios perfiles de reconocimiento (por defecto
server vs mobile) para poder decidir con datos de calidad reales.

Si el PDF tiene capa de texto embebida, la usa como referencia (ground truth)
y calcula la similitud real obtenida por cada modelo.

Uso:
    python scripts/run_docs.py --docs-dir /tmp/docs --out /tmp/review/ocr_report.txt
"""

from __future__ import annotations

import argparse
import difflib
import gc
import io
import os
import statistics
import time
from datetime import datetime
from pathlib import Path

import numpy as np

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
PDF_SUFFIXES = {".pdf"}

# Escala de renderizado. Debe coincidir con OCR_PDF_SCALE del servicio: a 2.0
# (144 dpi) las lineas de un fax degradado miden ~20 px y al reescalarlas a los
# 48 px que espera el reconocedor el texto se destruye.
PDF_SCALE = float(os.getenv("OCR_PDF_SCALE", "4.0"))


# --------------------------------------------------------------------------- #
# Carga de documentos
# --------------------------------------------------------------------------- #


def load_document(path: Path, max_pages: int | None = None, scale: float = PDF_SCALE):
    """
    Devuelve (paginas, textos_embebidos).

    paginas           : [(etiqueta, ndarray RGB)]
    textos_embebidos  : {etiqueta: str} con la capa de texto del PDF, si existe.
    """
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        import pypdfium2 as pdfium
        from PIL import Image as PILImage

        data = path.read_bytes()
        pdf = pdfium.PdfDocument(io.BytesIO(data))

        pages, embedded = [], {}
        total = len(pdf) if max_pages is None else min(max_pages, len(pdf))
        for index in range(total):
            page = pdf[index]
            label = f"pagina {index + 1}/{len(pdf)}"

            bitmap = page.render(scale=scale)
            pages.append((label, np.asarray(bitmap.to_pil().convert("RGB"))))

            # Capa de texto embebida (si la hay) como referencia
            try:
                text = page.get_textpage().get_text_range() or ""
                text = text.strip()
                if len(text) > 20:
                    embedded[label] = text
            except Exception:
                pass

        return pages, embedded

    if suffix in IMAGE_SUFFIXES:
        from PIL import Image as PILImage

        img = np.asarray(PILImage.open(path).convert("RGB"))
        return [("imagen", img)], {}

    raise ValueError(f"Formato no soportado: {path.name}")


# --------------------------------------------------------------------------- #
# Motor
# --------------------------------------------------------------------------- #


def build_engine(rec_tier: str, det_tier: str):
    from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR

    tiers = {"mobile": ModelType.MOBILE, "server": ModelType.SERVER}
    params = {
        "Det.engine_type": EngineType.ONNXRUNTIME,
        "Det.ocr_version": OCRVersion.PPOCRV5,
        "Det.model_type": tiers[det_tier],
        "Det.limit_side_len": 960,
        "Det.limit_type": "min",
        "Rec.engine_type": EngineType.ONNXRUNTIME,
        "Rec.ocr_version": OCRVersion.PPOCRV5,
        "Rec.model_type": tiers[rec_tier],
        "Cls.engine_type": EngineType.ONNXRUNTIME,
        "Cls.ocr_version": OCRVersion.PPOCRV4,
        "Cls.model_type": ModelType.MOBILE,
        "Global.use_cls": True,
        "Global.text_score": 0.5,
    }
    return RapidOCR(params=params)


def run_page(engine, img):
    """
    Devuelve el resultado real del pipeline y su tiempo.

    IMPORTANTE: el resultado se obtiene con engine(img), que es la ruta completa
    e incluye el filtro text_score. Llamar por separado a text_det/text_rec se
    saltaria ese filtro y devolveria lineas basura que la API nunca entrega.

    Aun mas: medir el desglose det/rec por separado con text_det() provocaba
    SIGSEGV intermitentes dentro de cv2.resize (preprocesado de la deteccion,
    opencv 5.0.0 en aarch64). Como ese desglose era puramente diagnostico, se
    elimino: aqui se mide exactamente el mismo camino que ejecuta el servicio.
    """
    started = time.perf_counter()
    out = engine(img)
    total_ms = (time.perf_counter() - started) * 1000

    lines = []
    boxes = getattr(out, "boxes", None)
    if boxes is not None and len(boxes) > 0:
        for box, text, score in zip(boxes, out.txts or [], out.scores or []):
            clean = (text or "").strip()
            if clean:
                lines.append(
                    {
                        "text": clean,
                        "score": float(score),
                        "box": np.asarray(box).astype(int).tolist(),
                    }
                )

    return {
        "lines": lines,
        "total_ms": total_ms,
        "n_lines": len(lines),
        "alpha_ratio": alpha_ratio(lines),
    }


def alpha_ratio(lines) -> float:
    """
    Proporcion de caracteres alfanumericos sobre el total.

    Es el discriminador fiable de texto real: la puntuacion suelta que devuelve
    el modelo sobre un escaneo ilegible ("...", "…", ":") tiene scores altos,
    asi que text_score no sirve para detectarla. Medido: 0-4% en basura frente
    a 85-94% en texto real.
    """
    text = "".join(line["text"] for line in lines)
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    return sum(c.isalnum() for c in chars) / len(chars)


# --------------------------------------------------------------------------- #
# Similitud con la referencia
# --------------------------------------------------------------------------- #


def _normalize(text: str) -> str:
    return "".join(text.lower().split())


def similarity(reference: str, hypothesis: str) -> float:
    """Ratio de similitud 0-1 sobre texto normalizado."""
    a, b = _normalize(reference), _normalize(hypothesis)
    if not a:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def wer(reference: str, hypothesis: str) -> float:
    """Tasa de error por palabra (0 = perfecto)."""
    ref_words = _normalize(reference)
    hyp_words = _normalize(hypothesis)
    if not ref_words:
        return 0.0
    matcher = difflib.SequenceMatcher(None, ref_words, hyp_words)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return max(0.0, 1.0 - matched / len(ref_words))


# --------------------------------------------------------------------------- #
# Informe
# --------------------------------------------------------------------------- #


def write_report(out_path: Path, entries, profiles, docs_root: Path, args):
    out: list[str] = []
    W = 96

    out.append("=" * W)
    out.append("INFORME OCR - DOCUMENTOS REALES")
    out.append("=" * W)
    out.append(f"Fecha          : {datetime.now():%Y-%m-%d %H:%M:%S}")
    out.append(f"Directorio     : {docs_root}")
    out.append(f"Perfiles       : {', '.join('rec=' + p for p in profiles)}")
    out.append("Deteccion      : mobile (960px, limit_type=min)")
    out.append(f"Renderizado    : escala {args.scale} ({args.scale * 72:.0f} dpi)")
    out.append("Hardware       : ARM64 Neoverse-N1, 2 nucleos, sin GPU")
    out.append("Motor          : RapidOCR + ONNX Runtime (CPUExecutionProvider)")
    out.append("Filtro         : text_score >= 0.5 (aplicado por el pipeline completo)")
    out.append(f"Documentos     : {len(entries)}")
    out.append("")
    out.append("NOTA: el texto de este informe es exactamente lo que devolveria la API")
    out.append("      en /ocr (el pipeline completo aplica el filtro text_score).")
    out.append("")
    out.append("POR QUE ESCALA 4: el reconocedor reescala cada recorte a 48 px de alto.")
    out.append("      A escala 2 (144 dpi) las lineas de un fax degradado miden ~20 px y")
    out.append("      se amplian 2.4x por interpolacion, destruyendo el texto: el modelo")
    out.append("      devuelve puntuacion suelta con scores altos. A escala 4 (288 dpi)")
    out.append("      el fax pasa de 0% a ~92% de caracteres alfanumericos. Los escaneos")
    out.append("      limpios son insensibles a la escala y solo pagan ~20% mas de tiempo.")
    out.append("")

    # ---- Resumen global ---- #
    out.append("=" * W)
    out.append("RESUMEN")
    out.append("=" * W)
    out.append("")

    header = f"{'Documento':<34} {'Pag':>8} "
    for profile in profiles:
        header += f"{'t(' + profile + ')':>12} "
    header += f"{'lineas':>7}"
    out.append(header)
    out.append("-" * len(header))

    agg = {p: {"time": [], "lines": [], "scores": [], "sim": [], "wer": [], "alpha": []} for p in profiles}

    for entry in entries:
        for page in entry["pages"]:
            # page['label'] es del tipo "pagina 1/1"; en el resumen solo interesa
            # el "1/1", no la palabra (que truncada quedaba como "pagi").
            short = page["label"].replace("pagina ", "")[:8]
            row = f"{entry['name'][:33]:<34} {short:>8} "
            for profile in profiles:
                result = page["results"][profile]
                row += f"{result['total_ms'] / 1000:>11.2f}s "
                agg[profile]["time"].append(result["total_ms"])
                agg[profile]["lines"].append(result["n_lines"])
                agg[profile]["scores"].extend(l["score"] for l in result["lines"])
                agg[profile]["alpha"].append(result["alpha_ratio"])
                if page.get("similarity"):
                    agg[profile]["sim"].append(page["similarity"][profile])
                    agg[profile]["wer"].append(page["wer"][profile])
            row += f"{page['results'][profiles[0]]['n_lines']:>7}"
            out.append(row)

    out.append("-" * len(header))
    out.append("")
    out.append("TOTALES")
    for profile in profiles:
        a = agg[profile]
        total_lines = sum(a["lines"])
        out.append(
            f"  rec={profile:<7} tiempo total {sum(a['time']) / 1000:8.2f}s   "
            f"media {statistics.mean(a['time']) / 1000:5.2f}s/pagina   "
            f"{total_lines:4d} lineas   "
            f"score medio {statistics.mean(a['scores']):.4f}   "
            f"alnum medio {statistics.mean(a['alpha']):.4f}"
        )
        if a["sim"]:
            out.append(
                f"  {' ' * 12} fidelidad vs texto embebido: {statistics.mean(a['sim']) * 100:5.1f}%   "
                f"error por palabra: {statistics.mean(a['wer']) * 100:5.1f}%"
            )
    out.append("")

    if len(profiles) > 1:
        times = {p: sum(agg[p]["time"]) / 1000 for p in profiles}
        fast = min(times, key=lambda p: times[p])
        slow = max(times, key=lambda p: times[p])
        factor = times[slow] / max(times[fast], 1e-6)
        if factor < 1.05:
            out.append(f"  -> {fast} y {slow} tardan practicamente lo mismo ({factor:.2f}x)")
        else:
            out.append(
                f"  -> {fast} es {factor:.2f}x mas rapido que {slow} "
                f"({times[fast]:.2f}s vs {times[slow]:.2f}s)"
            )
        if agg[profiles[0]]["sim"] and agg[profiles[1]]["sim"]:
            s0 = statistics.mean(agg[profiles[0]]["sim"]) * 100
            s1 = statistics.mean(agg[profiles[1]]["sim"]) * 100
            out.append(f"  -> fidelidad: {profiles[0]} {s0:.1f}%  vs  {profiles[1]} {s1:.1f}%  ({s1 - s0:+.1f} pts)")
        out.append("")

    # ---- Detalle por documento ---- #
    for entry in entries:
        out.append("")
        out.append("=" * W)
        out.append(f"ARCHIVO: {entry['name']}")
        if entry.get("note"):
            out.append(f"  ({entry['note']})")
        out.append("=" * W)

        for page in entry["pages"]:
            out.append("")
            h, w = page["image_shape"]
            out.append("-" * W)
            out.append(f"{page['label']}  -  {w}x{h} px")
            out.append("-" * W)

            for profile in profiles:
                r = page["results"][profile]
                out.append(
                    f"  rec={profile:<7} {r['total_ms']:8.1f} ms   "
                    f"{r['n_lines']:3d} lineas   "
                    f"alnum {r['alpha_ratio'] * 100:5.1f}%"
                )

            if page.get("similarity"):
                out.append("")
                out.append("  REFERENCIA (capa de texto embebida del PDF):")
                for profile in profiles:
                    sim = page["similarity"][profile]
                    we = page["wer"][profile]
                    marker = " <-- mejor" if sim == max(page["similarity"].values()) else ""
                    out.append(
                        f"    rec={profile:<7} fidelidad {sim * 100:5.1f}%   "
                        f"error/palabra {we * 100:5.1f}%{marker}"
                    )
                ref = page["embedded"]
                preview = ref[:300].replace("\n", " | ")
                out.append(f"    texto embebido: {preview}{'...' if len(ref) > 300 else ''}")

            # Texto completo del primer perfil
            primary = profiles[0]
            out.append("")
            out.append(f"  --- TEXTO COMPLETO (rec={primary}) ---")
            for i, line in enumerate(page["results"][primary]["lines"], 1):
                out.append(f"   {i:4d} [{line['score']:.3f}] {line['text']}")

            # Comparativa entre perfiles
            if len(profiles) > 1:
                base = profiles[0]
                other = profiles[1]
                bt = [l["text"] for l in page["results"][base]["lines"]]
                ot = [l["text"] for l in page["results"][other]["lines"]]
                # Alineacion real de secuencias: comparar por indice es enganoso
                # en cuanto un modelo inserta o pierde una linea, porque todo lo
                # siguiente aparece como "distinto".
                matcher = difflib.SequenceMatcher(None, bt, ot, autojunk=False)
                same = sum(b.size for b in matcher.get_matching_blocks())

                out.append("")
                out.append(f"  --- DIFERENCIAS {base} vs {other} ---")
                out.append(
                    f"  Lineas equivalentes: {same}/{max(len(bt), len(ot))} "
                    f"({same / max(max(len(bt), len(ot)), 1) * 100:.0f}%)"
                )
                if len(bt) != len(ot):
                    out.append(f"  [!] Numero de lineas distinto: {len(bt)} vs {len(ot)}")

                changes = [
                    op for op in matcher.get_opcodes() if op[0] != "equal"
                ]
                if not changes:
                    out.append("  (sin diferencias: ambos modelos leyeron lo mismo)")
                shown = 0
                for tag, i1, i2, j1, j2 in changes:
                    if shown >= 15:
                        out.append(f"   ... y {len(changes) - shown} bloques mas")
                        break
                    if tag == "replace" and (i2 - i1) == (j2 - j1):
                        # Mismo numero de lineas: enfrentarlas una a una.
                        for k in range(i2 - i1):
                            sb = page["results"][base]["lines"][i1 + k]["score"]
                            so = page["results"][other]["lines"][j1 + k]["score"]
                            out.append(f"   {base}(L{i1 + k + 1}) [{sb:.3f}] {bt[i1 + k]}")
                            out.append(f"   {other}(L{j1 + k + 1}) [{so:.3f}] {ot[j1 + k]}")
                            out.append("")
                        shown += 1
                    else:
                        if i1 != i2:
                            for k in range(i1, i2):
                                sb = page["results"][base]["lines"][k]["score"]
                                out.append(f"   - {base}(L{k + 1}) [{sb:.3f}] {bt[k]}")
                        if j1 != j2:
                            for k in range(j1, j2):
                                so = page["results"][other]["lines"][k]["score"]
                                out.append(f"   + {other}(L{k + 1}) [{so:.3f}] {ot[k]}")
                        out.append("")
                        shown += 1

            # Cajas opcionales
            if args.include_boxes:
                out.append("")
                out.append(f"  --- CAJAS (rec={primary}) ---")
                for i, line in enumerate(page["results"][primary]["lines"], 1):
                    box = line["box"]
                    xs = [p[0] for p in box]
                    ys = [p[1] for p in box]
                    out.append(
                        f"   {i:4d} x[{min(xs):5d},{max(xs):5d}] "
                        f"y[{min(ys):5d},{max(ys):5d}]  {line['text']}"
                    )

    out.append("")
    out.append("=" * W)
    out.append("FIN DEL INFORME")
    out.append("=" * W)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return len(out)


# --------------------------------------------------------------------------- #


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs-dir", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--profiles", default="server,mobile")
    ap.add_argument("--det", default="mobile")
    ap.add_argument("--max-pages", default=None, type=int)
    ap.add_argument(
        "--scale",
        default=PDF_SCALE,
        type=float,
        help=f"Escala de renderizado de PDFs (por defecto {PDF_SCALE}, = OCR_PDF_SCALE)",
    )
    ap.add_argument("--include-boxes", action="store_true")
    args = ap.parse_args()

    profiles = [p.strip() for p in args.profiles.split(",") if p.strip()]

    docs = sorted(
        p
        for p in args.docs_dir.iterdir()
        if p.suffix.lower() in IMAGE_SUFFIXES | PDF_SUFFIXES
    )
    if not docs:
        print(f"No hay documentos en {args.docs_dir}")
        return 1

    print(f"Documentos: {len(docs)}   perfiles: {profiles}   escala: {args.scale}")

    # Se renderizan las paginas UNA sola vez y se guardan en memoria, para poder
    # procesar cada perfil con un unico motor cargado a la vez. Mantener varios
    # RapidOCR vivos a la vez (uno por perfil) provoca SIGSEGV intermitentes en
    # ONNX Runtime aarch64: cada motor abre sus propias sesiones y pools de
    # hilos. Secuenciar los perfiles evita el problema y reduce el pico de RAM.
    loaded: list[tuple[str, list[tuple[str, "np.ndarray"]], dict[str, str]]] = []
    for doc in docs:
        try:
            pages, embedded = load_document(doc, args.max_pages, args.scale)
        except Exception as exc:
            print(f"[!] {doc.name}: no se pudo abrir: {exc}")
            loaded.append((doc.name, [], {"__error__": f"ERROR: {exc}"}))
            continue
        if embedded:
            note = (
                f"PDF con capa de texto embebida ({len(embedded)}/{len(pages)} "
                f"paginas) - usada como referencia"
            )
        else:
            note = "PDF sin capa de texto (escaneo puro) - sin referencia automatica"
        embedded = dict(embedded)
        embedded["__note__"] = note
        loaded.append((doc.name, pages, embedded))

    # entries[clave_documento][perfil] = resultados por pagina
    entries: dict[str, dict] = {}

    for profile in profiles:
        print(f"\n### Perfil rec={profile}")
        print(f"Cargando motor rec={profile} ...")
        engine = build_engine(profile, args.det)
        try:
            for name, pages, meta in loaded:
                doc_entry = entries.setdefault(name, {"meta": meta, "pages": {}})
                print(f"\n>>> {name}")
                if not pages:
                    continue
                for label, img in pages:
                    print(
                        f"    {label} ({img.shape[1]}x{img.shape[0]}) ...",
                        end="",
                        flush=True,
                    )
                    result = run_page(engine, img)
                    print(f" {profile}={result['total_ms'] / 1000:.1f}s")
                    doc_entry["pages"].setdefault(label, {"image_shape": img.shape[:2]})
                    page = doc_entry["pages"][label]
                    page["results"] = {**page.get("results", {}), profile: result}
        finally:
            del engine
            gc.collect()

    # Referencias (capa de texto embebida) y fusion de resultados
    report_entries = []
    for name, _, meta in loaded:
        doc_entry = entries[name]
        note = meta.get("__note__", "")
        pages_out = []
        for label, page in doc_entry["pages"].items():
            entry = {
                "label": label,
                "image_shape": page["image_shape"],
                "results": page["results"],
            }
            ref = meta.get(label)
            if ref:
                entry["embedded"] = ref
                entry["similarity"] = {}
                entry["wer"] = {}
                for profile, result in page["results"].items():
                    text = "\n".join(l["text"] for l in result["lines"])
                    entry["similarity"][profile] = similarity(ref, text)
                    entry["wer"][profile] = wer(ref, text)
            pages_out.append(entry)
        report_entries.append({"name": name, "pages": pages_out, "note": note})

    lines_written = write_report(args.out, report_entries, profiles, args.docs_dir, args)
    print(f"\n[+] Informe escrito: {args.out}  ({lines_written} lineas)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
