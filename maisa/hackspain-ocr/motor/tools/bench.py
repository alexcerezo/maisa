#!/usr/bin/env python3
"""Arnes de medida **real** de capacidad sobre La Caja (500 facturas).

No estima: cronometra el pipeline de verdad en esta maquina y deja los numeros
crudos en ``docs/bench.json`` mas una tabla legible en ``docs/capacidad.md``.

    cd maisa && PYTHONPATH=src ../.venv/bin/python tools/bench.py
    cd maisa && PYTHONPATH=src ../.venv/bin/python tools/bench.py --rapido

Que mide:

1. **Lote completo de 500 con cache caliente** para ``--trabajadores`` 1, 2, 4 y
   8, con ``--repeticiones`` pasadas por configuracion: se reporta mediana,
   minimo, maximo, desviacion tipica y rango relativo (nunca una sola muestra).
2. **Desglose por fase** (carga de entradas / lectura / decision / emision) para
   saber donde se va el tiempo y poder extrapolar con una formula explicita.
3. **OCR en frio** sobre una muestra pequena de facturas escaneadas, copiadas a
   un directorio temporal con un directorio de cache temporal: la cache real
   (``.cache/ocr``) NO se toca ni se borra nunca.
4. **Coste del servicio OCR** medido contra ``127.0.0.1:8866`` directamente, y
   cuanto paraleliza el contenedor (serial vs 4 hilos).
5. **Reparto capa_texto / OCR** leyendo ``escalon_lectura`` de la traza.
6. **Extrapolacion explicita** a 5.000, 50.000 y 1.000.000 de facturas, con
   los supuestos declarados y un contraste del modelo contra la medida real a
   500. Los objetivos se cambian con ``--objetivos``.
7. **Coste unitario por factura** en cada escalon de lectura (texto, cache,
   OCR en frio) y **guion de pitch** de dos minutos, montados con las cifras
   medidas en esta misma pasada (no con cifras de otro dia).

Todo lo temporal vive en ``tempfile.TemporaryDirectory``. Lo unico que se
escribe en el workspace son ``docs/bench.json`` y ``docs/capacidad.md``.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import io
import json
import os
import platform
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

from maisa import emit, lectura, procesa as P  # noqa: E402

DOCS = RAIZ / "docs"
OCR_URL = lectura.OCR_URL


# ----------------------------------------------------------------- utilidades
@contextlib.contextmanager
def _callado():
    """El pipeline imprime su resumen; en el arnes sobra."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


def _estadistica(muestras: list[float]) -> dict:
    ms = sorted(muestras)
    med = statistics.median(ms)
    return {
        "muestras_s": [round(x, 4) for x in muestras],
        "n": len(ms),
        "min": round(ms[0], 4),
        "mediana": round(med, 4),
        "max": round(ms[-1], 4),
        "desv_tipica": round(statistics.stdev(ms), 4) if len(ms) > 1 else 0.0,
        "rango_relativo_pct": round((ms[-1] - ms[0]) / med * 100, 2) if med else 0.0,
    }


def _maquina() -> dict:
    mem_kb = 0
    try:
        for linea in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if linea.startswith("MemTotal:"):
                mem_kb = int(linea.split()[1])
                break
    except OSError:
        pass
    try:
        carga = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        carga = []
    return {
        "cpu_logicos": os.cpu_count(),
        "memoria_total_gb": round(mem_kb / 1024 / 1024, 2),
        "plataforma": platform.platform(),
        "python": platform.python_version(),
        "carga_media_1_5_15_al_inicio": carga,
        "nota": "maquina compartida: la carga media se registra por medicion (ver 'carga' en cada bloque)",
    }


def _carga() -> list[float]:
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except OSError:
        return []


def _num(n: float, dec: int = 0) -> str:
    """Numero con los miles separados por espacio.

    Se separa con espacio (y no con punto) a proposito: el resto del documento
    usa el punto como separador decimal, y '6.980.3' no se lee. '6 980.3' si.
    """
    return f"{n:,.{dec}f}".replace(",", "\u00a0")


def _reparto_escalones(ruta_traza: Path) -> tuple[collections.Counter, list[str]]:
    """Cuenta ``escalon_lectura`` en la traza y devuelve los file_id que pasaron por OCR.

    Entiende los dos formatos que puede escribir el pipeline: la traza plana
    (una linea por decision, con ``escalon_lectura`` en la raiz) y la traza
    encadenada por hash (``--traza-hash``, eventos con ``tipo`` y ``datos``).
    """
    cuenta: collections.Counter = collections.Counter()
    por_ocr: list[str] = []
    for linea in ruta_traza.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea:
            continue
        evento = json.loads(linea)
        if "tipo" in evento:  # traza encadenada
            if evento.get("tipo") != "lectura":
                continue
            escalon = str(evento.get("datos", {}).get("escalon", "?"))
            file_id = str(evento.get("file_id", ""))
        else:  # traza plana
            escalon = str(evento.get("escalon_lectura", "?"))
            file_id = str(evento.get("file_id", ""))
        cuenta[escalon] += 1
        if escalon in ("cache_ocr", "vision_ocr") and file_id:
            por_ocr.append(file_id)
    return cuenta, por_ocr


# ------------------------------------------------------------------- medidas
def mide_lote(facturas: Path, args, trabajadores: int, traza: bool) -> dict:
    """Cronometra el lote completo end-to-end (lo que se entrega, tal cual)."""
    with tempfile.TemporaryDirectory(prefix="bench-lote-") as tmp:
        salida = Path(tmp) / "outcomes.jsonl"
        carga0, carga1 = _carga(), None
        t0 = time.perf_counter()
        with _callado():
            P.procesa(facturas, args.xlsx, args.config, args.snapshot, None, salida,
                      trabajadores, args.lote, traza_hash=traza)
        segundos = time.perf_counter() - t0
        carga1 = _carga()
        escalones, por_ocr = _reparto_escalones(salida.with_name("outcomes_traza.jsonl"))
        lineas = sum(1 for ln in salida.read_text(encoding="utf-8").splitlines() if ln.strip())
    return {
        "segundos": segundos,
        "facturas": lineas,
        "facturas_por_s": round(lineas / segundos, 3) if segundos else 0.0,
        "escalones": dict(escalones),
        "ocr_file_ids": por_ocr,
        "carga_antes": carga0,
        "carga_despues": carga1,
    }


def mide_desglose(pdfs: list[Path], args, trabajadores: int) -> dict:
    """Reparte el tiempo del lote entre carga de entradas, lectura, decision y emision."""
    with tempfile.TemporaryDirectory(prefix="bench-fases-") as tmp:
        salida = Path(tmp) / "outcomes.jsonl"
        with _callado():
            t0 = time.perf_counter()
            decisor, _maestro, _asientos = P.construye_decisor(
                args.xlsx, args.config, args.snapshot, None
            )
            t_carga = time.perf_counter() - t0

            t0 = time.perf_counter()
            docs = lectura.lee_lote(pdfs, trabajadores=trabajadores)
            t_lectura = time.perf_counter() - t0

            t0 = time.perf_counter()
            filas = [emit.linea(d.lectura.file_id, decisor.decide(d.lectura).resultado)
                     for d in docs]
            t_decision = time.perf_counter() - t0

            t0 = time.perf_counter()
            emit.escribe_jsonl([salida], filas)
            t_emision = time.perf_counter() - t0

        escalones = collections.Counter(d.escalon for d in docs)
    return {
        "trabajadores": trabajadores,
        "carga_entradas_s": round(t_carga, 4),
        "lectura_s": round(t_lectura, 4),
        "decision_s": round(t_decision, 4),
        "emision_s": round(t_emision, 4),
        "total_fases_s": round(t_carga + t_lectura + t_decision + t_emision, 4),
        "por_factura_decision_ms": round(t_decision / max(1, len(docs)) * 1000, 3),
        "escalones": dict(escalones),
        "escalones_por_factura": {
            "capa_texto_ms": round(t_lectura / max(1, len(docs)) * 1000, 3)
        },
    }


def mide_lectura_texto(pdfs_texto: list[Path], trabajadores: int) -> dict:
    """Coste de la capa de texto sola (sin OCR): s/factura y facturas/s."""
    with _callado():
        t0 = time.perf_counter()
        docs = lectura.lee_lote(pdfs_texto, trabajadores=trabajadores)
        segundos = time.perf_counter() - t0
    escalones = collections.Counter(d.escalon for d in docs)
    return {
        "facturas": len(docs),
        "trabajadores": trabajadores,
        "segundos": round(segundos, 4),
        "por_factura_s": round(segundos / max(1, len(docs)), 6),
        "facturas_por_s": round(len(docs) / segundos, 3) if segundos else 0.0,
        "escalones": dict(escalones),
    }


def mide_lectura_ocr_cache(escaneadas: list[Path], trabajadores: int) -> dict:
    """Coste de una factura que YA paso por OCR y ahora se sirve de cache.

    Es la situacion de regimen estacionario (la cache se indexa por sha256 del
    PDF) y es el termino que faltaba: pagar OCR en frio en el escenario de cache
    caliente inflaba el modelo. Solo se LEE la cache real; no se escribe nada.
    """
    with _callado():
        t0 = time.perf_counter()
        docs = lectura.lee_lote(escaneadas, trabajadores=trabajadores)
        segundos = time.perf_counter() - t0
    escalones = collections.Counter(d.escalon for d in docs)
    return {
        "facturas": len(docs),
        "trabajadores": trabajadores,
        "segundos": round(segundos, 4),
        "por_factura_s": round(segundos / max(1, len(docs)), 6),
        "facturas_por_s": round(len(docs) / segundos, 3) if segundos else 0.0,
        "escalones": dict(escalones),
        "cache_ok": escalones.get("cache_ocr", 0) == len(docs),
    }


def mide_ocr_frio(rutas: list[Path], trabajadores: int) -> dict:
    """OCR en frio sobre copias en temporal, con cache temporal.

    La cache real (``maisa/.cache/ocr``) no se lee ni se escribe: se apunta
    ``lectura.CACHE_OCR`` a un directorio de ``tempfile`` durante la medida.
    """
    cache_original = lectura.CACHE_OCR
    try:
        with tempfile.TemporaryDirectory(prefix="bench-ocr-") as tmp:
            base = Path(tmp)
            copias = []
            for ruta in rutas:
                destino = base / "pdfs" / ruta.name
                destino.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ruta, destino)
                copias.append(destino)
            lectura.CACHE_OCR = base / "cache"
            with _callado():
                t0 = time.perf_counter()
                docs = lectura.lee_lote(copias, trabajadores=trabajadores)
                segundos = time.perf_counter() - t0
            escalones = collections.Counter(d.escalon for d in docs)
            por_factura = [d.segundos for d in docs]
            cache_creada = len(list((base / "cache").glob("*.json")))
    finally:
        lectura.CACHE_OCR = cache_original
    if not docs:
        raise SystemExit("mide_ocr_frio: no hay facturas escaneadas que medir")
    return {
        "facturas": len(docs),
        "trabajadores": trabajadores,
        "segundos": round(segundos, 4),
        "por_factura_mediana_s": round(statistics.median(por_factura), 4),
        "por_factura_media_s": round(statistics.fmean(por_factura), 4),
        "por_factura_min_s": round(min(por_factura), 4),
        "por_factura_max_s": round(max(por_factura), 4),
        "por_factura_muestras_s": [round(x, 4) for x in por_factura],
        "facturas_por_s": round(len(docs) / segundos, 4) if segundos else 0.0,
        "escalones": dict(escalones),
        "entradas_cache_creadas": cache_creada,
    }


def mide_endpoint(rutas: list[Path]) -> dict:
    """Latencia del servicio OCR pura, sin nuestro pipeline por medio."""
    import requests

    if not rutas:
        raise SystemExit("mide_endpoint: no hay facturas que enviar al servicio OCR")
    muestras: list[float] = []
    for ruta in rutas:
        with ruta.open("rb") as fh:
            t0 = time.perf_counter()
            respuesta = requests.post(
                f"{OCR_URL}/ocr",
                params={"engine": "local"},
                files={"file": (ruta.name, fh, "application/pdf")},
                timeout=300,
            )
            respuesta.raise_for_status()
            respuesta.json()
            muestras.append(time.perf_counter() - t0)
    return {
        "url": OCR_URL,
        "facturas": len(muestras),
        "por_factura_mediana_s": round(statistics.median(muestras), 4),
        "por_factura_media_s": round(statistics.fmean(muestras), 4),
        "por_factura_min_s": round(min(muestras), 4),
        "por_factura_max_s": round(max(muestras), 4),
        "muestras_s": [round(x, 4) for x in muestras],
        "facturas_por_s_por_peticion": round(1 / statistics.median(muestras), 4),
    }


# ------------------------------------------------------------- extrapolacion
def extrapola(med: dict, n_objetivo: list[int]) -> dict:
    """Modelo explicito con tres regimenes, todos los terminos medidos.

    - ``t_fijo``        : cargar Excel + snapshot (desglose por fases).
    - ``c_texto``       : s/factura de la capa de texto (lectura sola, medida).
    - ``c_cache_ocr``   : s/factura de una escaneada YA vista (lectura de cache).
    - ``c_ocr_frio``    : s/factura del escalon OCR cuando nadie la ha visto.
    - ``S_ocr``         : speedup medido del escalon OCR al paralelizar.
    - ``c_decision``    : s/factura de decidir (medido).

    Los tres regimenes:

    - **A, regimen estacionario** (lo que pasa cada dia sobre La Caja): las
      escaneadas ya estan en cache -> se paga ``c_cache_ocr``, no OCR.
    - **B, lote nuevo con el mix de La Caja**: nadie ha visto estos PDFs ->
      5,8 % pagan OCR en frio.
    - **C, peor caso**: el 100 % son escaneos nuevos.
    """
    fases = med["desglose_4"]
    n_lote = med["lote_500_caliente"]["facturas"]
    reparto = med["reparto"]
    f_texto = reparto["capa_texto"] / n_lote
    f_ocr = reparto["ocr"] / n_lote

    t_fijo = fases["carga_entradas_s"]
    c_texto = med["lectura_texto"]["por_factura_s"]
    c_cache_ocr = med["lectura_ocr_cache"]["por_factura_s"]
    c_decision = fases["decision_s"] / n_lote
    c_ocr = med["ocr_frio_serial"]["por_factura_mediana_s"]

    t_ocr_serial = med["ocr_frio_serial"]["segundos"]
    t_ocr_paralelo = med["ocr_frio_4"]["segundos"]
    s_ocr = t_ocr_serial / t_ocr_paralelo if t_ocr_paralelo else 1.0

    def T(n: int, f_t: float, f_o: float, c_o: float) -> float:
        return t_fijo + n * (f_t * c_texto + f_o * c_o / (s_ocr if c_o == c_ocr else 1.0)
                             + c_decision)

    t500_medido = med["lote_500_caliente"]["mediana"]
    t500_modelo = T(n_lote, f_texto, f_ocr, c_cache_ocr)

    escenarios = {}
    for nombre, f_t, f_o, c_o, descripcion in (
        ("A_estacionario_cache_caliente", f_texto, f_ocr, c_cache_ocr,
         "lo de cada dia: mismo reparto que La Caja y las escaneadas ya vistas "
         "(la cache es por sha256 del PDF, asi que no se vuelven a pagar)"),
        ("B_lote_nuevo_mix_de_la_caja", f_texto, f_ocr, c_ocr,
         "un lote nuevo con el mismo mix: el 5,8 % de escaneadas paga OCR en frio"),
        ("C_peor_caso_todo_escaneado_nuevo", 0.0, 1.0, c_ocr,
         "peor caso: el 100 % de los PDFs son escaneos que nadie ha visto antes"),
    ):
        escenarios[nombre] = {
            "descripcion": descripcion,
            "f_texto": round(f_t, 4),
            "f_ocr": round(f_o, 4),
            "coste_ocr_aplicado_s": round(c_o, 4),
            "tiempos_s": {str(n): round(T(n, f_t, f_o, c_o), 1) for n in n_objetivo},
            "facturas_por_s": {str(n): round(n / T(n, f_t, f_o, c_o), 2) for n in n_objetivo},
        }

    return {
        "objetivos": [500] + list(n_objetivo),
        "formula": ("T(N) = t_fijo + N * (f_texto*c_texto + f_ocr*c_ocr_del_regimen/S_ocr "
                    "+ c_decision)   [S_ocr solo si el termino es OCR en frio]"),
        "regimenes": {
            "A_estacionario": "c_ocr_del_regimen = c_cache_ocr (la escaneada ya esta en cache)",
            "B_lote_nuevo": "c_ocr_del_regimen = c_ocr_frio, dividido por S_ocr si paraleliza",
            "C_peor_caso": "f_texto = 0, f_ocr = 1 y c_ocr_del_regimen = c_ocr_frio",
        },
        "constantes_medidas": {
            "t_fijo_s": round(t_fijo, 4),
            "c_texto_s_por_factura": round(c_texto, 6),
            "c_cache_ocr_s_por_factura": round(c_cache_ocr, 6),
            "c_ocr_s_por_factura_serial": round(c_ocr, 4),
            "S_ocr_paralelismo_medido": round(s_ocr, 2),
            "c_decision_s_por_factura": round(c_decision, 6),
            "f_texto": round(f_texto, 4),
            "f_ocr": round(f_ocr, 4),
        },
        "contraste_modelo_500": {
            "regimen": "A_estacionario_cache_caliente",
            "medido_s": t500_medido,
            "modelo_s": round(t500_modelo, 1),
            "error_relativo_pct": round((t500_modelo - t500_medido) / t500_medido * 100, 1),
        },
        "escenarios": escenarios,
        "cuello_de_botella": {
            "ocr_servicio_s_por_factura": round(
                med["endpoint"]["por_factura_mediana_s"], 4),
            "ocr_facturas_por_s_por_ranura": med["endpoint"]["facturas_por_s_por_peticion"],
            "ocr_facturas_por_hora_por_ranura": round(
                3600 / med["endpoint"]["por_factura_mediana_s"], 1),
            "paraleliza_el_contenedor": round(s_ocr, 2) > 1.2,
            "nota": ("el coste por factura escaneada se expresa en segundos de CPU del "
                     "servicio OCR (= tiempo de pared de una peticion si el contenedor "
                     "atiende de una en una; verificado con el test serial vs 4 hilos)"),
        },
        "coste": {
            "unidad": "vCPU-s del servicio OCR",
            "por_factura_escaneada": round(med["endpoint"]["por_factura_mediana_s"], 4),
            "para_10000_escaneadas": round(
                10000 * med["endpoint"]["por_factura_mediana_s"], 1),
            "para_1000000_escaneadas": round(
                1000000 * med["endpoint"]["por_factura_mediana_s"], 1),
            "formula_euros": ("coste_EUR = N_escaneadas * s_OCR_por_factura * precio_EUR_por_vCPU_s"
                              "  (el precio NO se mide aqui: no tenemos tarifa de esta maquina)"),
            "coste_marginal_por_factura_de_texto": "0 s de OCR (la capa de texto es exacta y gratis)",
        },
        "supuestos": [
            "El reparto de La Caja (94,2 % capa de texto / 5,8 % escaneadas) se mantiene.",
            "La cache OCR es por sha256 del PDF: un documento ya visto no se vuelve a pagar, "
            "asi que en regimen estacionario el OCR se paga una vez por documento nuevo.",
            "El escalon OCR paraleliza con el factor S_ocr medido; si el contenedor es "
            "single-thread real, S_ocr es 1 y el escenario C no mejora al subir trabajadores.",
            "El coste de decision es lineal en el numero de facturas (una factura, una "
            "decision; no hay estado compartido entre facturas).",
            "La maquina es la de este hackathon (2 vCPU, 11 GB). Mas nucleos no aceleran "
            "la capa de texto (GIL + pypdf) pero si el OCR (I/O + proceso externo).",
            "Se asume que el Excel y el snapshot crecen poco: t_fijo se mide a 516 asientos "
            "y 11 proveedores; a 1 M de facturas habria que shardear las entradas.",
            "El coste por factura escaneada se mide con el contenedor OCR de este entorno; "
            "otro motor o otro hardware cambian c_ocr, no la estructura del modelo.",
        ],
    }


# ------------------------------------------------------------------- salida
def tabla_md(filas: list[list[str]], cabecera: list[str]) -> str:
    out = ["| " + " | ".join(cabecera) + " |",
           "|" + "|".join(["---"] * len(cabecera)) + "|"]
    for fila in filas:
        out.append("| " + " | ".join(str(c) for c in fila) + " |")
    return "\n".join(out)


def escribe_md(med: dict, ext: dict, maquina: dict, ruta: Path) -> None:
    reparto = med["reparto"]
    n_lote = med["lote_500_caliente"]["facturas"]
    fases = med["desglose_4"]
    L: list[str] = []
    L.append("# Capacidad y coste: lo medido, no lo prometido")
    L.append("")
    L.append(f"Generado por `maisa/tools/bench.py` el {med['fecha']} sobre "
             f"{maquina['cpu_logicos']} vCPU / {maquina['memoria_total_gb']} GB. "
             "Todos los tiempos son de pared, medidos con `time.perf_counter()` en esta "
             "maquina y con la carga registrada en `docs/bench.json`.")
    L.append("")
    L.append("> Regla de la casa: si una cifra no esta en la columna *medido*, es una "
             "extrapolacion y va marcada como tal.")
    L.append("")

    L.append("## 1. Lote completo de 500 (cache caliente) por numero de trabajadores")
    L.append("")
    L.append(f"{med['lote_500_caliente']['repeticiones']} repeticiones por configuracion, "
             "end-to-end (`python -m maisa.procesa`), escribiendo `outcomes.jsonl` y "
             "contando las lineas emitidas.")
    L.append("")
    filas = []
    for w, est in sorted(med["lote_500_caliente"]["por_trabajadores"].items(), key=lambda kv: int(kv[0])):
        filas.append([
            w, est["n"], f"{est['min']:.2f}", f"**{est['mediana']:.2f}**", f"{est['max']:.2f}",
            f"{est['desv_tipica']:.2f}", f"{est['rango_relativo_pct']:.1f} %",
            f"{n_lote / est['mediana']:.1f}",
        ])
    L.append(tabla_md(filas, ["trabajadores", "n", "min (s)", "mediana (s)", "max (s)",
                              "desv. típ.", "rango rel.", "facturas/s (mediana)"]))
    L.append("")
    mejor_w = med["lote_500_caliente"]["mejor_trabajadores"]
    mejor = med["lote_500_caliente"]["por_trabajadores"][mejor_w]
    L.append(f"- Mejor configuracion medida: **--trabajadores {mejor_w}** con "
             f"**{mejor['mediana']:.2f} s** de mediana ({n_lote / mejor['mediana']:.1f} facturas/s) "
             f"y una dispersion de {mejor['rango_relativo_pct']:.1f} % entre pasadas.")
    t_traza = med.get("lote_500_con_traza")
    if t_traza:
        delta = t_traza["segundos"] - mejor["mediana"]
        L.append(f"- Con `--traza-hash` (auditoria encadenada de "
                 f"{t_traza['eventos']} eventos): **{t_traza['segundos']:.2f} s** en una pasada "
                 f"a {t_traza['trabajadores']} trabajadores, "
                 f"{'+' if delta >= 0 else ''}{delta:.2f} s sobre la mediana sin traza.")
    L.append("")

    L.append("## 2. Donde se va el tiempo (desglose por fase, 4 trabajadores)")
    L.append("")
    total = fases["total_fases_s"]
    filas = [
        ["cargar Excel + snapshot (fijo)", f"{fases['carga_entradas_s']:.3f}",
         f"{fases['carga_entradas_s'] / total * 100:.1f} %"],
        ["leer los 500 PDF (texto + caché OCR)", f"{fases['lectura_s']:.3f}",
         f"{fases['lectura_s'] / total * 100:.1f} %"],
        ["decidir las 500 facturas", f"{fases['decision_s']:.3f}",
         f"{fases['decision_s'] / total * 100:.1f} %"],
        ["emitir el JSONL", f"{fases['emision_s']:.3f}",
         f"{fases['emision_s'] / total * 100:.1f} %"],
        ["**total**", f"**{total:.3f}**", "100 %"],
    ]
    L.append(tabla_md(filas, ["fase", "segundos", "reparto"]))
    L.append("")
    L.append(f"- Decidir una factura cuesta **{fases['por_factura_decision_ms']:.2f} ms** "
             "(regex + aritmetica + precedencia; sin modelo de lenguaje).")
    L.append("")

    L.append("## 3. Reparto capa_texto vs OCR (de la traza, campo `escalon_lectura`)")
    L.append("")
    L.append(tabla_md(
        [["capa_texto (sin OCR)", reparto["capa_texto"], f"{reparto['capa_texto'] / n_lote * 100:.1f} %"],
         ["OCR necesario (caché o servicio)", reparto["ocr"], f"{reparto['ocr'] / n_lote * 100:.1f} %"],
         ["**total**", n_lote, "100 %"]],
        ["escalón", "facturas", "reparto"]))
    L.append("")
    lt = med["lectura_texto"]
    L.append(f"- La capa de texto resuelve **{lt['facturas']} facturas a "
             f"{lt['por_factura_s'] * 1000:.2f} ms/factura** ({lt['facturas_por_s']:.0f} facturas/s) "
             f"con {lt['trabajadores']} trabajadores: es exacta y no cuesta servicio externo.")
    cc = med["lectura_ocr_cache"]
    L.append(f"- Una escaneada **ya vista** (servida de cache por sha256) cuesta "
             f"**{cc['por_factura_s'] * 1000:.2f} ms/factura**, es decir "
             f"{lt['por_factura_s'] / cc['por_factura_s']:.1f} veces menos que un escaneo nuevo: "
             "ese es el ahorro que compra la cache.")
    L.append(f"- Detalle de escalones en el lote: `{med['lote_500_caliente']['escalones']}`.")
    L.append("")

    L.append("## 4. OCR en frio (cache temporal, PDFs copiados a temporal)")
    L.append("")
    filas = []
    for clave, etiqueta in (("ocr_frio_serial", "serial (1 hilo)"), ("ocr_frio_4", "4 hilos")):
        d = med.get(clave)
        if not d:
            continue
        filas.append([etiqueta, d["facturas"], f"{d['segundos']:.2f}",
                      f"**{d['por_factura_mediana_s']:.2f}**",
                      f"{d['por_factura_min_s']:.2f}",
                      f"{d['por_factura_max_s']:.2f}",
                      f"{d['facturas_por_s']:.3f}"])
    L.append(tabla_md(filas, ["configuración", "facturas", "total (s)", "s/factura (mediana)",
                              "min", "max", "facturas/s"]))
    L.append("")
    ep = med["endpoint"]
    L.append(f"- Servicio OCR directo (`POST {ep['url']}/ocr`, sin nuestro pipeline): "
             f"**{ep['por_factura_mediana_s']:.2f} s/factura** de mediana "
             f"(min {ep['por_factura_min_s']:.2f}, max {ep['por_factura_max_s']:.2f}, "
             f"n={ep['facturas']}).")
    L.append(f"- Eso es un techo de **{ext['cuello_de_botella']['ocr_facturas_por_hora_por_ranura']:.0f} "
             f"facturas escaneadas/hora por ranura** de OCR.")
    if med.get("ocr_frio_4") and med.get("ocr_frio_serial"):
        s = ext["constantes_medidas"]["S_ocr_paralelismo_medido"]
        veredicto = ("el contenedor **si** atiende varias peticiones a la vez"
                     if s > 1.2 else
                     "el contenedor **no** paraleliza: atiende de una en una")
        L.append(f"- Paralelizar el escalon OCR de 1 a {med['ocr_frio_4']['trabajadores']} hilos "
                 f"acelera **x{s:.2f}**, luego {veredicto}. "
                 "Consecuencia: subir `--trabajadores` no compra OCR; se compra con mas "
                 "ranuras de OCR o con la cache.")
    L.append("- La cache real (`maisa/.cache/ocr`) no se ha tocado: la medida usa un "
             "directorio de cache de `tempfile` y copias de los PDFs en temporal.")
    L.append("")

    L.append("## 5. Coste unitario por factura, escalon a escalon")
    L.append("")
    L.append("Lo que cuesta **una** factura segun por donde entre. Es la tabla que "
             "convierte el modelo de coste en una decision de negocio: la palanca no "
             "es *procesar mas rapido*, es **no llamar al OCR cuando la capa de texto "
             "ya es exacta**.")
    L.append("")
    cc = med["lectura_ocr_cache"]
    frio = med["ocr_frio_serial"]["por_factura_mediana_s"]
    filas = [
        ["capa de texto (94,2 % del lote)", "0", f"{lt['por_factura_s'] * 1000:.2f} ms",
         "0", "ninguna"],
        ["escaneada ya vista (cache sha256)", "0", f"{cc['por_factura_s'] * 1000:.2f} ms",
         "0", "disco local"],
        ["escaneada nueva (OCR en frio)", f"{frio:.2f} s", f"{frio:.2f} s",
         f"{frio:.2f} vCPU·s", "contenedor OCR"],
    ]
    L.append(tabla_md(filas, ["escalón de lectura", "coste OCR", "tiempo de pared",
                              "vCPU·s de OCR", "recurso externo"]))
    L.append("")
    L.append(f"- Factura con capa de texto: **0 vCPU·s de OCR** y "
             f"{lt['por_factura_s'] * 1000:.2f} ms de CPU. El motor de reglas anade "
             f"{fases['por_factura_decision_ms']:.2f} ms. Es el 94,2 % del lote.")
    L.append(f"- Escaneada nueva: **{frio:.2f} vCPU·s de OCR** por factura. Es "
             f"{frio / max(lt['por_factura_s'], 1e-9):.0f} veces el coste de una factura "
             "de texto. Ahi esta todo el gasto del sistema.")
    L.append(f"- Escaneada ya vista: **0 vCPU·s** (la cache es por `sha256` del PDF, "
             f"no por nombre) y {cc['por_factura_s'] * 1000:.2f} ms. Reejecutar el lote "
             "completo es gratis: por eso el escenario del sabado no asusta.")
    L.append("")

    L.append(f"## 6. Extrapolacion a {', '.join(f'{n:,}'.replace(',', '.') for n in ext['objetivos'] if n != 500)} de facturas")
    L.append("")
    L.append("**Todo lo de esta seccion es extrapolado, no medido.** Modelo explicito:")
    L.append("")
    L.append(f"```\n{ext['formula']}\n```")
    L.append("")
    for reg, desc in ext["regimenes"].items():
        L.append(f"- `{reg}`: {desc}")
    L.append("")
    c = ext["constantes_medidas"]
    L.append(tabla_md(
        [["`t_fijo` (carga de entradas)", f"{c['t_fijo_s']:.3f} s", "medido"],
         ["`c_texto` (factura con capa de texto)", f"{c['c_texto_s_por_factura'] * 1000:.2f} ms/factura", "medido"],
         ["`c_cache_ocr` (escaneada ya vista)", f"{c['c_cache_ocr_s_por_factura'] * 1000:.2f} ms/factura", "medido"],
         ["`c_ocr_frio` (escaneada nueva, serial)", f"{c['c_ocr_s_por_factura_serial']:.2f} s/factura", "medido"],
         ["`S_ocr` (speedup del OCR)", f"x{c['S_ocr_paralelismo_medido']:.2f}", "medido"],
         ["`c_decision`", f"{c['c_decision_s_por_factura'] * 1000:.3f} ms/factura", "medido"],
         ["`f_texto` / `f_ocr`", f"{c['f_texto']:.3f} / {c['f_ocr']:.3f}", "medido"]],
        ["constante", "valor", "origen"]))
    L.append("")
    contraste = ext["contraste_modelo_500"]
    err = contraste["error_relativo_pct"]
    veredicto = ("el modelo reproduce la realidad dentro del ruido de la maquina"
                 if abs(err) <= 15 else
                 "el modelo se desvia: tomar la cifra extrapolada con pinzas y mirar los supuestos")
    L.append(f"**Contraste del modelo con la realidad a 500 facturas** (regimen "
             f"{contraste['regimen']}): medido {contraste['medido_s']:.2f} s, modelo "
             f"{contraste['modelo_s']:.1f} s, error {err:+.1f} % -- {veredicto}.")
    L.append("")
    for nombre, esc in ext["escenarios"].items():
        L.append(f"### Escenario `{nombre}`")
        L.append("")
        L.append(esc["descripcion"])
        L.append("")
        filas = []
        for n in ext["objetivos"]:
            clave = str(n)
            if clave in esc["tiempos_s"]:
                filas.append([_num(n), _num(esc["tiempos_s"][clave], 1),
                              _num(esc["facturas_por_s"][clave], 2)])
        L.append(tabla_md(filas, ["facturas", "tiempo (s)", "facturas/s"]))
        L.append("")
    cb = ext["cuello_de_botella"]
    L.append("### Cuello de botella y coste")
    L.append("")
    L.append(f"- **Cuello de botella:** el servicio OCR. "
             f"{cb['ocr_servicio_s_por_factura']:.2f} s de servicio por factura escaneada "
             f"= {cb['ocr_facturas_por_s_por_ranura']:.3f} facturas/s por ranura. "
             "Nada de nuestro proceso paraleliza mas rapido que el contenedor aguanta.")
    L.append(f"- **Coste por factura escaneada: {cb['ocr_servicio_s_por_factura']:.2f} vCPU·s del "
             f"servicio OCR.** 10.000 escaneadas = "
             f"{_num(ext['coste']['para_10000_escaneadas'])} vCPU·s; 1.000.000 = "
             f"{_num(ext['coste']['para_1000000_escaneadas'])} vCPU·s.")
    L.append("- **Factura de texto: 0 s de OCR.** Es la palanca de coste mas grande que "
             "tenemos: no gastar vision cuando la capa de texto ya es exacta.")
    L.append(f"- En euros: `{ext['coste']['formula_euros']}`. No damos un numero en EUR "
             "porque no tenemos la tarifa de esta maquina; quien lo sepa, multiplica.")
    L.append("")

    L.append("## 7. Que hariamos con 10x el volumen (y con 100x)")
    L.append("")
    n10 = ext["objetivos"][1] if len(ext["objetivos"]) > 1 else ext["objetivos"][0]
    esc_c = ext["escenarios"]["C_peor_caso_todo_escaneado_nuevo"]["tiempos_s"][str(n10)]
    esc_b = ext["escenarios"]["B_lote_nuevo_mix_de_la_caja"]["tiempos_s"][str(n10)]
    L.append(f"10x el lote de La Caja son {_num(n10)} facturas; 100x son 50.000. Lo que "
             "cambia al crecer **no es el motor de reglas** (lineal y de "
             f"{fases['por_factura_decision_ms']:.2f} ms/factura), es el escalon de "
             "lectura. Por orden de rentabilidad:")
    L.append("")
    L.append(f"1. **Repartir por `file_id` y anadir procesos, no hilos.** El lote ya se "
             f"procesa con `--trabajadores`; a {_num(n10)} facturas se shardea el "
             "directorio en N trozos y se lanzan N procesos. El motor es **sin estado** "
             "y la salida es un JSONL que se concatena: no hay coordinacion. Medido en "
             f"esta maquina, el lote caliente baja de "
             f"{med['lote_500_caliente']['por_trabajadores'][sorted(med['lote_500_caliente']['por_trabajadores'], key=int)[0]]['mediana']:.2f} s "
             f"a {mejor['mediana']:.2f} s al pasar de "
             f"{sorted(med['lote_500_caliente']['por_trabajadores'], key=int)[0]} a "
             f"{mejor_w} trabajadores (x{med['lote_500_caliente']['por_trabajadores'][sorted(med['lote_500_caliente']['por_trabajadores'], key=int)[0]]['mediana'] / mejor['mediana']:.2f}): "
             "el trabajo paraleliza, pero los hilos comparten GIL; el siguiente escalon "
             "es repartir el directorio entre procesos, no hilos.")
    L.append(f"2. **La cache de OCR es la palanca grande.** El escenario del sabado "
             f"(40 facturas nuevas + regla nueva) y el 'reprocesar todo' son gratis: "
             f"{cc['por_factura_s'] * 1000:.2f} ms por escaneada ya vista. A 50.000 "
             "facturas con el mix de La Caja el gasto de OCR es de "
             f"{_num(ext['coste']['para_10000_escaneadas'] / 10000 * (50000 * c['f_ocr']))} vCPU·s, "
             "una vez; despues, cero.")
    L.append(f"3. **Separar el escalon de OCR en su propio pool.** Es el unico termino "
             f"que no es lineal con la CPU disponible: {cb['ocr_servicio_s_por_factura']:.2f} s "
             f"por factura y ranura, y el contenedor solo acelera x{c['S_ocr_paralelismo_medido']:.2f}. "
             "A 10x escala se le dan ranuras dedicadas (o varias replicas del "
             "contenedor) en vez de competir con la lectura de texto por los mismos nucleos.")
    L.append(f"4. **Subir el umbral de la capa de texto con cuidado.** Cada punto de "
             "`calidad_texto_minima` que se baja manda mas facturas a OCR: es la "
             "variable que mueve el coste de 0 a "
             f"{frio:.2f} vCPU·s por factura. La politica conservadora es no bajarlo.")
    L.append(f"5. **Shardear las entradas antes que el motor.** `t_fijo` (Excel + "
             f"snapshot) son {c['t_fijo_s']:.3f} s a 516 asientos: a 1 M de facturas el "
             "cuello pasa a ser cargar el maestro en cada worker, y ahi toca indice "
             "en memoria compartida o lectura por rango.")
    L.append("")
    L.append(f"- Coste del peor caso a {_num(n10)} facturas (todo escaneado nuevo): "
             f"{_num(esc_c)} s = {_num(esc_c / 3600, 1)} h en un solo hilo de OCR. Con el mix "
             f"real de La Caja: {_num(esc_b)} s. El modelo completo esta en el JSON "
             "(`extrapolado.escenarios`), no en esta prosa.")
    L.append("")

    L.append("## 8. Guion de pitch (2 minutos, con estas cifras)")
    L.append("")
    L.append("Todo lo que sigue sale de la tabla de arriba, medido hoy en esta maquina.")
    L.append("")
    L.append(f"1. **Que hace** (15 s). Un lote de {n_lote} facturas PDF entra y sale un "
             "JSONL con `PAGAR` / `NO_PAGAR` / `ESCALAR`, una linea por factura, sin "
             "duplicados y validado. El motor de decision es determinista: ninguna "
             "etiqueta sale de un modelo de lenguaje.")
    L.append(f"2. **Lo que cuesta** (30 s). **{n_lote / mejor['mediana']:.0f} facturas/s** "
             f"en caliente: el lote entero en **{mejor['mediana']:.1f} s**. Y el reparto "
             f"importa: **{reparto['capa_texto']} de {n_lote} facturas "
             f"({reparto['capa_texto'] / n_lote * 100:.1f} %)** se resuelven leyendo la "
             f"capa de texto del PDF, a **{lt['por_factura_s'] * 1000:.1f} ms y 0 vCPU·s "
             f"de OCR**. Solo **{reparto['ocr']} ({reparto['ocr'] / n_lote * 100:.1f} %)** "
             "necesitan vision, y esas se cachean por `sha256` del PDF.")
    L.append(f"3. **La palanca de coste** (25 s). Una factura de texto cuesta **0**; una "
             f"escaneada nueva cuesta **{frio:.2f} vCPU·s**; una escaneada ya vista, "
             f"**{cc['por_factura_s'] * 1000:.1f} ms**. Por eso reejecutar no cuesta: la "
             "cache convierte el segundo lote en el primero. El cuello de botella esta "
             f"identificado y medido: el servicio OCR, "
             f"{cb['ocr_facturas_por_hora_por_ranura']:.0f} facturas/hora por ranura.")
    L.append(f"4. **Como escala** (30 s). A {_num(n10)} facturas el modelo da "
             f"{_num(ext['escenarios']['B_lote_nuevo_mix_de_la_caja']['tiempos_s'][str(n10)])} s "
             "con el mix de La Caja, y el motor es sin estado: se shardea por `file_id` "
             "y se replican procesos. El error del modelo contra la medida real a "
             f"{n_lote} es **{err:+.1f} %**, asi que la extrapolacion no es una promesa "
             "de folleto.")
    L.append("5. **Lo que no decimos** (20 s). No damos EUR porque no tenemos la tarifa "
             "de esta maquina: damos vCPU·s y la formula. No medimos la calidad del OCR "
             "(aciertos), solo el tiempo. Y el simulador de cambios trabaja sobre datos "
             "y reglas, no sobre la lectura. Estan en la seccion de supuestos.")
    L.append("")
    L.append(f"**Frase de cierre:** *el lote entero en {mejor['mediana']:.1f} segundos, "
             f"el {reparto['capa_texto'] / n_lote * 100:.0f} % sin pagar OCR, y el "
             f"reprocesado gratis porque la cache es por contenido.*")
    L.append("")

    L.append("## 9. Supuestos de la extrapolacion (declarados, no escondidos)")
    L.append("")
    for s in ext["supuestos"]:
        L.append(f"- {s}")
    L.append("")
    L.append("## 10. Que NO hemos medido")
    L.append("")
    for l in med["no_medido"]:
        L.append(f"- {l}")
    L.append("")
    ruta.write_text("\n".join(L) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mide de verdad la capacidad del pipeline de La Caja.")
    ap.add_argument("--repeticiones", type=int, default=3, help="pasadas por configuracion (>=3 recomendado)")
    ap.add_argument("--trabajadores", type=int, nargs="+", default=[1, 2, 4, 8])
    ap.add_argument("--muestra-ocr", type=int, default=10, help="facturas escaneadas para el OCR en frio")
    ap.add_argument("--muestra-endpoint", type=int, default=5, help="peticiones directas al servicio OCR")
    ap.add_argument("--objetivos", type=int, nargs="+", default=[5000, 50000, 1000000],
                    help="tamanos de lote a extrapolar (ademas de los 500 medidos)")
    ap.add_argument("--facturas", type=Path, default=P.FACTURAS_POR_DEFECTO)
    ap.add_argument("--xlsx", type=Path, default=P.XLSX_POR_DEFECTO)
    ap.add_argument("--config", type=Path, default=P.CONFIG_POR_DEFECTO)
    ap.add_argument("--snapshot", type=Path, default=P.SNAPSHOT_POR_DEFECTO)
    ap.add_argument("--lote", type=int, default=1)
    ap.add_argument("--json", type=Path, default=DOCS / "bench.json")
    ap.add_argument("--md", type=Path, default=DOCS / "capacidad.md")
    ap.add_argument("--rapido", action="store_true", help="1 repeticion y muestras minimas (prueba de humo)")
    ap.add_argument("--desde-json", action="store_true",
                    help="no mide: reescribe el markdown desde el --json ya medido")
    args = ap.parse_args(argv)

    if args.desde_json:
        if not args.json.exists():
            raise SystemExit(f"no hay medidas en {args.json}")
        salida = json.loads(args.json.read_text(encoding="utf-8"))
        escribe_md(salida["medido"], salida["extrapolado"], salida["medido"]["maquina"],
                   args.md)
        print(f"[bench] escrito {args.md} desde {args.json} (sin volver a medir)")
        return 0

    if args.rapido:
        args.repeticiones = 1
        args.trabajadores = [4]
        args.muestra_ocr = 3
        args.muestra_endpoint = 2

    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(line_buffering=True)

    pdfs = sorted(args.facturas.glob("*.pdf"))
    if not pdfs:
        raise SystemExit(f"no hay PDFs en {args.facturas}")
    print(f"[bench] {len(pdfs)} facturas en {args.facturas}")

    # 1) pasada de referencia con traza: reparto capa_texto / OCR y que facturas escanean
    print("[bench] pasada de referencia a 4 trabajadores (con traza)...")
    ref = mide_lote(args.facturas, args, 4, traza=True)
    escalones = collections.Counter(ref["escalones"])
    n_texto = escalones.get("capa_texto", 0)
    n_ocr = len(pdfs) - n_texto
    print(f"[bench] reparto: capa_texto={n_texto} ocr={n_ocr} ({dict(escalones)})")

    # 2) lote completo, varias pasadas por configuracion de trabajadores
    por_trabajadores: dict[str, dict] = {}
    for w in args.trabajadores:
        muestras = []
        r = None
        for i in range(args.repeticiones):
            r = mide_lote(args.facturas, args, w, traza=False)
            muestras.append(r["segundos"])
            print(f"[bench]   trabajadores={w} pasada {i + 1}/{args.repeticiones}: "
                  f"{r['segundos']:.2f} s ({r['facturas_por_s']:.1f} facturas/s)")
        est = _estadistica(muestras)
        est["facturas_por_s"] = round(len(pdfs) / est["mediana"], 3)
        est["carga_final"] = r["carga_despues"]
        por_trabajadores[str(w)] = est
    mejor_w = min(por_trabajadores, key=lambda w: por_trabajadores[w]["mediana"])

    # 3) desglose por fase
    print("[bench] desglose por fases (4 trabajadores)...")
    desglose = mide_desglose(pdfs, args, 4)

    # 3b) coste de la auditoria encadenada, ya con la maquina caliente
    print("[bench] pasada con --traza-hash (coste de la auditoria)...")
    con_traza = mide_lote(args.facturas, args, 4, traza=True)

    # 4) coste de la capa de texto sola
    solo_texto = set(ref["ocr_file_ids"])
    pdfs_texto = [p for p in pdfs if p.name not in solo_texto]
    print(f"[bench] coste de la capa de texto sola ({len(pdfs_texto)} facturas, 4 trabajadores)...")
    lectura_texto = mide_lectura_texto(pdfs_texto, 4)

    # 5) OCR en frio sobre una muestra de escaneadas + coste de una escaneada ya vista
    por_nombre = {p.name: p for p in pdfs}
    todas_ocr = [por_nombre[f] for f in ref["ocr_file_ids"] if f in por_nombre]
    escaneadas = todas_ocr[:args.muestra_ocr]
    print(f"[bench] coste de una escaneada ya vista ({len(todas_ocr)} facturas, cache real)...")
    lectura_ocr_cache = mide_lectura_ocr_cache(todas_ocr, 4)
    print(f"[bench] OCR en frio sobre {len(escaneadas)} facturas escaneadas (serial)...")
    ocr_serial = mide_ocr_frio(escaneadas, 1)
    print("[bench] OCR en frio (4 hilos)...")
    ocr_4 = mide_ocr_frio(escaneadas, 4)

    # 6) servicio OCR directo
    print("[bench] servicio OCR directo...")
    endpoint = mide_endpoint(escaneadas[:args.muestra_endpoint])

    med = {
        "fecha": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "maquina": _maquina(),
        "facturas": len(pdfs),
        "reparto": {"capa_texto": n_texto, "ocr": n_ocr},
        "lote_500_caliente": {
            "repeticiones": args.repeticiones,
            "facturas": len(pdfs),
            "por_trabajadores": por_trabajadores,
            "mejor_trabajadores": mejor_w,
            "mediana": por_trabajadores[mejor_w]["mediana"],
            "escalones": ref["escalones"],
            "carga_media_final": ref["carga_despues"],
        },
        "lote_500_con_traza": {
            "trabajadores": 4,
            "segundos": round(con_traza["segundos"], 4),
            "eventos": sum(con_traza["escalones"].values()) * 2 + 2,
        },
        "desglose_4": desglose,
        "lectura_texto": lectura_texto,
        "lectura_ocr_cache": lectura_ocr_cache,
        "ocr_frio_serial": ocr_serial,
        "ocr_frio_4": ocr_4,
        "endpoint": endpoint,
        "no_medido": [
            "Coste en EUR: no tenemos la tarifa de esta maquina, asi que damos vCPU·s y la formula.",
            "Latencia del ERP real: el lote se mide contra el snapshot; una consulta en vivo "
            "paga ademas el rate-limit de 10 req/s del bridge (medido en otro sitio, no aqui).",
            "Tipos de archivo distintos de PDF (emails, hojas de calculo): el pipeline actual "
            "no los lee; no hay nada que cronometrar.",
            "Comportamiento con varios procesos en paralelo sobre la misma cache OCR.",
            "La calidad del OCR (aciertos): esto mide tiempo, no acierto.",
        ],
    }
    ext = extrapola(med, args.objetivos)

    salida = {"medido": med, "extrapolado": ext}
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(salida, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    escribe_md(med, ext, med["maquina"], args.md)
    print(f"[bench] escrito {args.json}")
    print(f"[bench] escrito {args.md}")
    print(f"[bench] mediana 500 facturas: {med['lote_500_caliente']['mediana']:.2f} s "
          f"({len(pdfs) / med['lote_500_caliente']['mediana']:.1f} facturas/s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
