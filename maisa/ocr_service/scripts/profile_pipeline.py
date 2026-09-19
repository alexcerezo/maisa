"""
Perfilado del pipeline sobre el corpus real + estructura interna del PDF.

Responde a dos preguntas que se contestan con datos, no con opinion:

A) ¿DONDE SE VA EL TIEMPO?
   Separa el tiempo de RENDERIZADO (PDFium, C++) del de INFERENCIA (ONNX
   Runtime, C++). Si el tiempo esta dentro de esas dos, reescribir el servicio
   en Rust no aporta nada: ya son C++. Si aparece tiempo significativo en
   Python, entonces si hay algo que ganar. La medicion anterior del proyecto
   (Python <1%) era de una imagen suelta, no de este pipeline, asi que hay que
   rehacerla antes de opinar.

B) ¿QUE SON LOS 29 DOCUMENTOS SIN CAPA DE TEXTO?
   La auditoria los marco como "escaneo" por tener tinta sin texto. Pero una
   pagina puede tener tinta y no tener capa de texto por DOS motivos muy
   distintos:
     - IMAGEN RASTER: una foto o un escaneo. El texto esta en los pixeles y
       ningun PDF puede darlo. Hay que pasar OCR, y sin ground truth.
     - TEXTO VECTORIAL: el texto se dibujo como curvas de Bezier en vez de como
       operadores de texto (`Tj`/`TJ`). Es un PDF digital, el texto es perfecto,
       pero no hay capa. Aqui SI hay ground truth: se puede recuperar, o al
       menos cruzar con OCR para detectar trampas.
   Se distinguen contando operadores en los content streams y objetos /Image.

Uso (DENTRO del contenedor):

    python /tmp/profile_pipeline.py /home/ocr/test_files/corpus --limit 30
"""

from __future__ import annotations

import argparse
import base64
import re
import statistics
import sys
import time
import zlib
from pathlib import Path
from typing import Any


sys.path.insert(0, "/app")
sys.path.insert(0, "/tmp")

from app.server import _Source, _alpha_ratio, _run_page  # noqa: E402

# Codificadores cuyo contenido NO es texto: dentro va una imagen comprimida.
# Hay que mirar el /Filter declarado, no los bytes: un JPEG envuelto en
# ASCII85 es puro ASCII imprimible, asi que cualquier heuristica del tipo
# "si es ASCII es un content stream" lo deja pasar y luego encuentra `Tj`
# por casualidad entre 60 KB de ruido.
IMAGE_FILTER = re.compile(
    rb"/(?:DCTDecode|DCT|CCITTFaxDecode|CCF|JPXDecode|JBIG2Decode|RunLengthDecode)"
)


def _declared_filter(data: bytes, pos: int) -> bytes:
    """Devuelve el /Filter declarado en el diccionario que precede al stream."""
    idx = data.rfind(b"/Filter", max(0, pos - 512), pos)
    if idx < 0:
        return b""
    return data[idx : idx + 160]


# Operadores de pintado de texto en un content stream.
#
# Solo Tj/TJ: son los que REALMENTE dibujan texto. `BT`/`ET` NO valen, porque
# son solo "abrir/cerrar bloque de texto" y muchos generadores los emiten
# siempre, incluso vacios. Los escaneos de este corpus llevan literalmente
# `BT /F1 12 Tf 14.4 TL ET` sin un solo Tj: contar BT/ET los marcaba a los 500
# como "con texto" cuando pdfium encuentra texto en 471.
#
# Tampoco se cuentan `'` ni `"` (mostrar texto con salto), que son operadores
# validos pero coinciden con bytes sueltos de cualquier binario.
TEXT_OPS = re.compile(rb"(?:\bTj\b|\bTJ\b)")
# Operadores de curva y de rectangulo (una letra rellena puede ser texto vectorial).
CURVE_OPS = re.compile(rb"(?<![\w/])(?:c|v|y|re)(?![\w])")
# Objetos de imagen referenciados.
IMAGE_OBJ = re.compile(rb"/Subtype\s*/Image")


def _inflate(chunk: bytes) -> bytes | None:
    """Prueba zlib y DEFLATE crudo (sin cabecera)."""
    for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
        try:
            return zlib.decompress(chunk, wbits)
        except Exception:
            continue
    return None


def _ascii85(chunk: bytes) -> bytes | None:
    """Decodifica ASCII85 (con o sin delimitadores <~ ~>)."""
    for adobe in (True, False):
        try:
            return base64.a85decode(chunk, adobe=adobe)
        except Exception:
            continue
    return None


def decode_streams(data: bytes) -> tuple[bytes, int, int]:
    """
    Descomprime todos los streams y devuelve la union.

    Hicieron falta tres intentos para que esto funcionara, y conviene dejar
    escrito por que, porque los dos fallos producian el MISMO sintoma (casi
    ningun stream decodificado) y era facil quedarse en el primero:

      1. `re.finditer(rb"stream\r?\n")` casaba tambien con la COLA de
         `endstream\n`. Hay que excluir el prefijo con `(?<!end)`.
      2. El fin de linea tras `stream` puede ser `\n`, `\r\n` o `\r` a secas,
         y el byte final hay que quitarlo antes de descomprimir.
      3. Muchos PDFs generados por ReportLab declaran
         `/Filter [ /ASCII85Decode /FlateDecode ]`: primero ASCII85 y LUEGO
         Flate. Se aplican los filtros EN CADENA, del primero al ultimo.
         Aplicar zlib sobre el texto ASCII85 falla siempre.

    Devuelve (bytes, descomprimidos, total_encontrados) para poder VERIFICAR
    que el decodificador funciona y no devuelve casi nada en silencio.
    """
    out = bytearray()
    found = 0
    decoded = 0
    for match in re.finditer(rb"(?<!end)stream(\r\n|\r|\n)", data):
        start = match.end()
        end = data.find(b"endstream", start)
        if end < 0:
            continue
        found += 1
        # Si el /Filter dice que es una imagen comprimida, se descarta sin
        # intentar leerla: no puede contener operadores de texto.
        if IMAGE_FILTER.search(_declared_filter(data, match.start())):
            continue
        chunk = data[start:end]
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        elif chunk.endswith((b"\n", b"\r")):
            chunk = chunk[:-1]

        payload = _inflate(chunk)
        if payload is None:
            a85 = _ascii85(chunk)
            if a85 is not None:
                payload = _inflate(a85)
                if payload is None:
                    payload = a85
        if payload is None:
            # Ni Flate ni ASCII85: o va en claro, o es un stream binario que no
            # sabemos leer (JPEG/CCITT dentro de /DCTDecode o /CCITTFaxDecode).
            # Meter esos bytes al recuento convertiria cada imagen en un falso
            # "documento con texto", porque el binario esta lleno de bytes que
            # coinciden con operadores sueltos. Solo se acepta si es ASCII.
            if any(b > 127 for b in chunk[:256]):
                found -= 1  # binario opaco: no lo contamos como stream legible
                continue
            payload = chunk
        out.extend(payload)
        decoded += 1
    return bytes(out), decoded, found


def structure(path: Path) -> dict[str, Any]:
    """Cuenta operadores de texto, curvas e imagenes en el PDF crudo."""
    data = path.read_bytes()
    decoded, streams, found = decode_streams(data)
    images = len(IMAGE_OBJ.findall(data))
    text_ops = len(TEXT_OPS.findall(decoded))
    curve_ops = len(CURVE_OPS.findall(decoded))
    return {
        "streams": streams,
        "streams_found": found,
        "decoded_bytes": len(decoded),
        "text_ops": text_ops,
        "curve_ops": curve_ops,
        "images": images,
        "vector_text": text_ops == 0 and curve_ops > 50,
        "raster": images > 0 and text_ops == 0,
    }


def profile(path: Path) -> dict[str, Any] | None:
    """
    Separa render de inferencia DENTRO DE UNA SOLA PASADA.

    La primera version medía el render en un momento, la inferencia en otro y
    el total en un tercero, y luego restaba. Eso no mide nada: son ejecuciones
    distintas (y la escalera puede parar en una escala diferente en cada una),
    asi que el 'resto' salia negativo y el informe llegaba a imprimir "DENTRO
    DE C++: 103.5%". Una medicion que se contradice a si misma no sirve.

    Aqui se reproduce el bucle de la escalera de `_run_local_page` y se
    cronometra cada fase por separado en la misma iteracion. El tiempo de
    Python es lo que queda hasta el total, y por construccion no puede ser
    negativo.
    """
    try:
        source = _Source(path)
    except Exception:
        return None
    try:
        index = 0
        scales = source.scales(None)
        render_ms = infer_ms = 0.0
        lines: list[dict[str, Any]] = []
        used_scale = scales[0]
        attempts = 0

        total_start = time.perf_counter()
        # Mismo bucle y misma condicion de parada que el servidor.
        for current in scales:
            attempts += 1
            t0 = time.perf_counter()
            img = source.render(index, current)
            render_ms += (time.perf_counter() - t0) * 1000

            t0 = time.perf_counter()
            lines, _ = _run_page(img)
            infer_ms += (time.perf_counter() - t0) * 1000
            del img

            used_scale = current
            alpha = _alpha_ratio(lines)
            if alpha >= 0.5:  # equivalente a _looks_like_text()
                break

        # Cierre del bucle: montar el texto y serializar (coste de Python puro).
        text = "\n".join(line["text"] for line in lines)
        total_ms = (time.perf_counter() - total_start) * 1000

        return {
            "file": path.name,
            "pages": source.count,
            "render_ms": round(render_ms, 1),
            "infer_ms": round(infer_ms, 1),
            "python_ms": round(max(total_ms - render_ms - infer_ms, 0.0), 1),
            "total_ms": round(total_ms, 1),
            "lines": len(lines),
            "scale": used_scale,
            "attempts": attempts,
            "chars": len(text),
        }
    finally:
        source.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Perfilado del pipeline OCR.")
    parser.add_argument("root", nargs="?", default="/home/ocr/test_files/corpus")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--json", dest="json_out")
    parser.add_argument("--all-structure", action="store_true",
                        help="clasificar los 500 ficheros por estructura")
    args = parser.parse_args()

    root = Path(args.root)
    pdfs = sorted(root.rglob("*.pdf"))
    if not pdfs:
        print(f"No hay PDFs en {root}", file=sys.stderr)
        return 2

    # ------------------------------------------------------------------ #
    # B) estructura de los 500
    # ------------------------------------------------------------------ #
    print("=" * 78)
    print(" B) ESTRUCTURA INTERNA (que se dibuja de verdad)")
    print("=" * 78)
    target = pdfs if args.all_structure else pdfs[: args.limit]
    rows = []
    for path in target:
        try:
            info = structure(path)
        except Exception as exc:
            print(f"  !! {path.name}: {type(exc).__name__}")
            continue
        info["file"] = path.name
        info["size"] = path.stat().st_size
        rows.append(info)

    raster = [r for r in rows if r["raster"]]
    vector = [r for r in rows if r["vector_text"]]
    texto = [r for r in rows if r["text_ops"] > 0]
    print(f"  analizados              : {len(rows)}")
    print(f"  con operadores de texto : {len(texto)}   <- capa de texto real")
    print(f"  sin texto, con curvas   : {len(vector)}   <- POSIBLE texto vectorial")
    print(f"  sin texto, con imagenes : {len(raster)}   <- raster: escaneo/foto")
    print()
    # Verificacion del decodificador: si casi ningun stream descomprime, los
    # conteos de operadores no valen y hay que arreglar esto antes de sacar
    # conclusiones sobre la estructura del corpus.
    if rows:
        med = sorted(r["decoded_bytes"] for r in rows)[len(rows) // 2]
        sin_dec = sum(1 for r in rows if r["streams"] == 0 and r["streams_found"] > 0)
        print("  [verificacion del decodificador]")
        print(f"    streams encontrados (mediana) : {sorted(r['streams_found'] for r in rows)[len(rows) // 2]}")
        print(f"    streams descomprimidos (mediana): {sorted(r['streams'] for r in rows)[len(rows) // 2]}")
        print(f"    bytes decodificados (mediana)  : {med}")
        print(f"    ficheros con streams y 0 decodificados: {sin_dec}")
        if med < 200:
            print("    !! SOSPECHOSO: apenas se decodifica nada -> los conteos de abajo NO son fiables")
    print()
    if vector:
        print("  Candidatos a texto vectorial (no tienen capa, pero son digitales):")
        for r in sorted(vector, key=lambda r: -r["curve_ops"])[:15]:
            print(f"    {r['file']:36s} curvas={r['curve_ops']:6d} textops={r['text_ops']:3d} "
                  f"imgs={r['images']:2d} {r['size']:9d} B")
    if raster:
        print()
        print("  Raster autentico (escaneo/foto, sin ground truth posible):")
        for r in sorted(raster, key=lambda r: -r["size"])[:15]:
            print(f"    {r['file']:36s} imgs={r['images']:2d} curvas={r['curve_ops']:6d} "
                  f"{r['size']:9d} B")

    # ------------------------------------------------------------------ #
    # A) perfil de tiempos
    # ------------------------------------------------------------------ #
    print()
    print("=" * 78)
    print(" A) PERFIL DE TIEMPOS (¿donde se va el tiempo?)")
    print("=" * 78)
    sample = [p for p in pdfs if structure(p)["text_ops"] > 0][: args.limit]
    if not sample:
        print("  !! ningun PDF con operadores de texto: el decodificador sigue mal")
        return 1

    perf = []
    for path in sample:
        try:
            result = profile(path)
        except Exception as exc:
            print(f"  !! {path.name}: {type(exc).__name__}: {exc}")
            continue
        if result:
            perf.append(result)
            print(f"  {path.name:36s} render={result['render_ms']:7.1f}ms "
                  f"infer={result['infer_ms']:7.1f}ms python={result['python_ms']:7.1f}ms "
                  f"total={result['total_ms']:7.1f}ms intentos={result['attempts']} "
                  f"escala={result['scale']}")

    if perf:
        print()
        render = statistics.median(r["render_ms"] for r in perf)
        infer = statistics.median(r["infer_ms"] for r in perf)
        total = statistics.median(r["total_ms"] for r in perf)
        python = statistics.median(r["python_ms"] for r in perf)
        intentos = statistics.median(r["attempts"] for r in perf)
        print(f"  MEDIANA sobre {len(perf)} documentos (escalera: {intentos} intento(s) de media)")
        # Por construccion los tres sumandos salen de la MISMA pasada, asi que
        # los porcentajes suman 100. Si no sumaran, la medicion estaria mal.
        print(f"    renderizado (PDFium, C++)     : {render:8.1f} ms  {100 * render / total:5.1f}%")
        print(f"    inferencia (ONNX RT, C++)     : {infer:8.1f} ms  {100 * infer / total:5.1f}%")
        print(f"    resto (Python: escalera, I/O) : {python:8.1f} ms  {100 * python / total:5.1f}%")
        print(f"    TOTAL                         : {total:8.1f} ms")
        check = (render + infer + python) / total * 100
        print(f"    [cuadre] suma de fases = {check:.1f}% del total (debe ser ~100%)")
        print()
        cpp = render + infer
        print(f"  DENTRO DE CODIGO C++: {100 * cpp / total:.1f}%")
        print(f"  REESCRIBIBLE EN RUST: {100 * python / total:.1f}%  (el 'resto')")

    if args.json_out:
        import json

        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"structure": rows, "perf": perf},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  JSON: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
