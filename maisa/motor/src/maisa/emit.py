"""Emision y validacion de `outcomes.jsonl`.

La organizacion valida de forma **binaria**: exactamente un outcome por cada
fichero entregado y `result` dentro del enum esperado. Todo lo demas (traza,
ADRs, pitch) suma, pero esto es lo que aprueba o suspende. Por eso el
`file_id` se emite **tal cual sale del fichero**, sin normalizar: si el
validador del jurado compara cadenas, cualquier `strip()`/`casefold()` nuestro
seria un fallo silencioso e irrecuperable.

La linea de entrega lleva **exactamente dos claves**: `file_id` y `result`. La
explicabilidad (los motivos, los hechos, los campos leidos) no va aqui: vive en
la traza (`outcomes_traza.jsonl` / `trace`), que es material de auditoria y no
de entrega. `linea()` no admite campos extra a proposito, para que la entrega
no vuelva a mezclar formas por descuido.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

RESULTADOS = ("PAGAR", "NO_PAGAR", "ESCALAR")


def normaliza_file_id(valor: str) -> str:
    """Solo para *comparar*; nunca se emite el resultado de esta funcion."""
    return unicodedata.normalize("NFKC", valor).strip().casefold()


def clave_orden(file_id: str) -> str:
    """Clave de orden canonica de una linea del JSONL: NFKC **sin** plegar caja.

    Hace falta porque `sorted()` sobre `Path` no es portable: `PurePath.__lt__`
    pliega mayusculas en Windows y no en POSIX, asi que el mismo lote se ordenaba
    distinto en cada sistema y la entrega salia con las lineas en otro orden
    (mismos datos, otros bytes). Comprobado: `copia_2026_0518.pdf` va antes de
    `F26-2163_...pdf` en Windows y despues en Linux, 129 lineas de diferencia.

    Va sin `casefold()` a proposito: es el orden del `outcomes.jsonl` ya
    publicado y de `tests/oro/outcomes_oro.jsonl`, que la CI compara por md5.
    Ordenar plegando caja daria el orden de Windows y cambiaria la entrega.

    Antes habia aqui una version que ordenaba los trozos numericos como numeros
    (natural sort). Nunca llego a llamarse, y de haberse llamado habria dado otro
    orden (`pagina_2` antes que `pagina_10`) y roto el md5 de la entrega.
    """
    return unicodedata.normalize("NFKC", file_id).strip()


def linea(file_id: str, resultado: str) -> dict:
    """La linea de entrega: **solo** `file_id` y `result`.

    Los motivos y demas explicabilidad no se emiten aqui; van a la traza. La
    firma no acepta nada mas para que la entrega no recupere la mezcla de
    formas que tenia (439 lineas de dos claves y 61 de tres).
    """
    if resultado not in RESULTADOS:
        raise ValueError(f"resultado fuera del enum: {resultado!r}")
    if not file_id or file_id != file_id.strip():
        raise ValueError(f"file_id sospechoso (vacio o con espacios): {file_id!r}")
    return {"file_id": file_id, "result": resultado}


def escribe_jsonl(rutas: list[Path], filas: list[dict]) -> Path:
    """Escribe el JSONL ordenado y devuelve la ruta.

    `newline="\\n"` es obligatorio: sin el, en Windows `open` traduce cada `\\n` a
    CRLF y `tools/valida_entrega.py` marca la entrega como no publicable. Que el
    fichero tenga los mismos bytes en cualquier maquina no es cosmetico: la CI
    compara md5 del resultado.

    El orden es `clave_orden` (NFKC, sensible a caja) y no el de entrada: asi el
    fichero es el mismo aunque el lote llegue en otro orden, y no depende de como
    ordene `Path` el sistema.
    """
    filas = sorted(filas, key=lambda f: clave_orden(f["file_id"]))
    for ruta in rutas:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        with ruta.open("w", encoding="utf-8", newline="\n") as fh:
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
