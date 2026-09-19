"""Emision y validacion de `outcomes.jsonl`.

La organizacion valida de forma **binaria**: exactamente un outcome por cada
fichero entregado y `result` dentro del enum esperado. Todo lo demas (traza,
ADRs, pitch) suma, pero esto es lo que aprueba o suspende. Por eso el
`file_id` se emite **tal cual sale del fichero**, sin normalizar: si el
validador del jurado compara cadenas, cualquier `strip()`/`casefold()` nuestro
seria un fallo silencioso e irrecuperable.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

RESULTADOS = ("PAGAR", "NO_PAGAR", "ESCALAR")


def clave_orden(file_id: str) -> tuple:
    """Orden estable y legible: primero por nombre, con numeros comparados como numeros."""
    partes = []
    for trozo in file_id.replace("-", "_").split("_"):
        partes.append((0, int(trozo), "") if trozo.isdigit() else (1, 0, trozo))
    return (len(partes), partes)


def normaliza_file_id(valor: str) -> str:
    """Solo para *comparar*; nunca se emite el resultado de esta funcion."""
    return unicodedata.normalize("NFKC", valor).strip().casefold()


def linea(file_id: str, resultado: str, motivos: list[str] | None = None, **extra) -> dict:
    if resultado not in RESULTADOS:
        raise ValueError(f"resultado fuera del enum: {resultado!r}")
    if not file_id or file_id != file_id.strip():
        raise ValueError(f"file_id sospechoso (vacio o con espacios): {file_id!r}")
    fila = {"file_id": file_id, "result": resultado}
    if motivos:
        fila["motivos"] = motivos
    fila.update(extra)
    return fila


def escribe_jsonl(rutas: list[Path], filas: list[dict]) -> Path:
    """Escribe el JSONL ordenado y devuelve la ruta."""
    orden = {normaliza_file_id(f["file_id"]): i for i, f in enumerate(filas)}
    filas = sorted(filas, key=lambda f: orden[normaliza_file_id(f["file_id"])])
    for ruta in rutas:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        with ruta.open("w", encoding="utf-8") as fh:
            for fila in filas:
                fh.write(json.dumps(fila, ensure_ascii=False) + "\n")
    return rutas[0]


def valida_jsonl(ruta: Path, pdfs: list[Path]) -> list[str]:
    """Devuelve la lista de problemas. Vacia == entrega valida."""
    problemas: list[str] = []
    if not ruta.exists():
        return [f"no existe {ruta}"]
    vistos: dict[str, str] = {}
    n = 0
    for num, linea_txt in enumerate(ruta.read_text(encoding="utf-8").splitlines(), 1):
        if not linea_txt.strip():
            problemas.append(f"{ruta.name}:{num} linea vacia")
            continue
        n += 1
        try:
            fila = json.loads(linea_txt)
        except json.JSONDecodeError as exc:
            problemas.append(f"{ruta.name}:{num} JSON invalido: {exc}")
            continue
        fid = fila.get("file_id")
        if not isinstance(fid, str) or not fid:
            problemas.append(f"{ruta.name}:{num} file_id ausente o no textual")
            continue
        if fila.get("result") not in RESULTADOS:
            problemas.append(f"{ruta.name}:{num} result fuera del enum: {fila.get('result')!r}")
        if fid != fid.strip():
            problemas.append(f"{ruta.name}:{num} file_id con espacios alrededor: {fid!r}")
        clave = normaliza_file_id(fid)
        if clave in vistos:
            problemas.append(f"{ruta.name}:{num} file_id duplicado: {fid!r} (ya en linea {vistos[clave]})")
        vistos[clave] = num
    esperados = {normaliza_file_id(p.name): p.name for p in pdfs}
    faltan = sorted(esperados[k] for k in set(esperados) - set(vistos))
    sobran = sorted(k for k in set(vistos) - set(esperados))
    if faltan:
        problemas.append(f"faltan {len(faltan)} ficheros: {faltan[:10]}")
    if sobran:
        problemas.append(f"sobran {len(sobran)} entradas: {sobran[:10]}")
    if n != len(pdfs):
        problemas.append(f"hay {n} lineas y {len(pdfs)} ficheros")
    return problemas
