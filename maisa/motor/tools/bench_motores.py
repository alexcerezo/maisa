#!/usr/bin/env python3
"""Que motor de OCR lleva a la **decision** correcta, medido contra el oro.

No compara textos ni puntuaciones de confianza: mete la lectura de cada motor
por la norma y cuenta cuantas de las 500 decisiones de `tests/oro/` se
reproducen. Es la unica comparacion que importa, porque la decision se toma por
reglas sobre los datos extraidos: un motor que escribe mas bonito pero se come
un digito del NIF es peor, no mejor.

    cd maisa && PYTHONPATH=src ../.venv/bin/python tools/bench_motores.py
    cd maisa && PYTHONPATH=src ../.venv/bin/python tools/bench_motores.py --forzar

Que mide:

1. **Cuantas facturas llegan al OCR.** La escalera para en la capa de texto si
   esta basta, asi que el OCR solo decide en las que no la tienen. Ese numero
   es el tamano real del problema, y suele ser una fraccion pequena del corpus.
2. **Cuatro brazos, mismas 500 facturas y mismo decisor:**
   - `local`   — solo el motor local (lo que hace hoy el motor con
                 `MAISA_OCR_NUBE` sin poner).
   - `nube`    — solo la nube.
   - `escalera`— local primero, nube solo si el local queda flojo y solo si
                 mejora la calidad (lo que hace hoy el motor con
                 `MAISA_OCR_NUBE=1`).
   - `hibrido` — **local decide**, y la nube solo rellena los identificadores
                 (NIF, IBAN, pedido) que el local no consigue resolver contra el
                 maestro, aceptando de la nube **solo** lo que el maestro
                 confirme. El importe nunca sale de la nube. Mide si la nube
                 aporta algo como segunda lectura sin darle la decision.
3. **Confusion contra el oro** por brazo, mas las facturas donde los brazos
   discrepan y los campos que extrajo cada uno, para poder ver *por que*.
4. **Latencia** por motor, porque el coste de cambiar de motor es tiempo.

Las lecturas crudas se cachean en `.cache/motores/`, que no esta versionado:
la nube tarda y no tiene sentido pagarla dos veces. `--forzar` las rehace.
"""

from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

from maisa import lectura, norma, procesa as P, segunda_lectura as S, texto  # noqa: E402

CACHE = RAIZ / ".cache" / "motores"
ORO = RAIZ / "tests" / "oro" / "outcomes_oro.jsonl"
CAMPOS = ("nif", "pedido", "total", "iban", "fecha")


# ------------------------------------------------------------------ servicio
def _texto_de_payload(datos: dict) -> tuple[str, int]:
    """Paginas del payload de `/ocr`, unidas. Devuelve tambien cuantas hay."""
    paginas: list[str] = []
    for r in datos.get("results") or []:
        if isinstance(r, str):
            paginas.append(r)
            continue
        if isinstance(r, dict):
            if isinstance(r.get("text"), str):
                paginas.append(r["text"])
            elif isinstance(r.get("lines"), list):
                paginas.append("\n".join(
                    l if isinstance(l, str) else str(l.get("text", ""))
                    for l in r["lines"]
                ))
    if not paginas and isinstance(datos.get("texto"), str):
        paginas = [datos["texto"]]
    return "\n".join(paginas), len(paginas)


def _una_lectura(pdf: Path, motor: str, forzar: bool) -> dict:
    """Lee un PDF con un motor concreto, cacheando el resultado en disco."""
    sha = lectura.sha256_pdf(pdf)
    ruta = CACHE / f"{sha}.{motor}.json"
    if ruta.exists() and not forzar:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        if datos.get("texto"):
            return datos

    import requests

    t0 = time.monotonic()
    with pdf.open("rb") as fh:
        respuesta = requests.post(
            f"{lectura._ocr_url()}/ocr",
            params={"engine": motor},
            files={"file": (pdf.name, fh, "application/pdf")},
            timeout=(60, 900),
        )
    respuesta.raise_for_status()
    cuerpo = respuesta.json()
    texto_leido, paginas = _texto_de_payload(cuerpo)
    datos = {
        "sha256": sha,
        "file_id": pdf.name,
        "motor_pedido": motor,
        "motor_real": cuerpo.get("engine") or motor,
        "segundos": round(time.monotonic() - t0, 2),
        "paginas": paginas,
        "texto": texto_leido,
        "stats": cuerpo.get("stats"),
        "fallback": cuerpo.get("fallback"),
    }
    CACHE.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
    return datos


# -------------------------------------------------------------------- brazos
def _escalera(local: dict, nube: dict, paginas: int) -> dict:
    """Reproduce el peldano 4 del motor sobre lecturas ya hechas.

    El motor no manda a la nube salvo que el local quede por debajo del umbral
    o el texto sea ilegible, y solo sustituye si la nube **mejora** la calidad.
    Aqui se aplica la misma regla a posteriori, para poder comparar los tres
    brazos sobre exactamente las mismas lecturas.
    """
    calidad_local = lectura.calidad_texto(local["texto"], paginas)
    crudo = texto.extrae(local["texto"], local["file_id"], paginas, "vision_ocr")
    flojo = calidad_local < 0.6 or crudo.texto_ilegible
    if not flojo:
        return {**local, "via": "local"}
    calidad_nube = lectura.calidad_texto(nube["texto"], paginas)
    if calidad_nube > calidad_local:
        return {**nube, "via": "nube"}
    return {**local, "via": "local (la nube no mejoro)"}


# --------------------------------------------------------- brazo hibrido
# La logica vive en `maisa.segunda_lectura`, que es tambien la que alimenta la
# cola de revision: una sola implementacion para las dos, o la medicion y la
# produccion se separan sin que nadie lo note.
def _hibrido(
    local: texto.Lectura, nube: texto.Lectura, maestro, vocab: dict
) -> tuple[texto.Lectura, list[str]]:
    """Local decide; la nube solo rellena los identificadores que el local no lee.

    Ver `maisa.segunda_lectura`: la regla del IBAN (nunca se tapa si el local
    leyo uno) y el importe (nunca sale de la nube) estan documentadas alli.
    """
    return S.rellena_identificadores(local, nube, maestro, vocab)


def _decide_todo(decisor: norma.Decisor, lecturas: list[texto.Lectura]) -> list[norma.Decision]:
    """Decide el lote entero, con la regla de pedido repetido que ve el lote."""
    P.marca_pedidos_repetidos(decisor, lecturas)
    return [decisor.decide(leida) for leida in lecturas]


def _campos(leida: texto.Lectura) -> str:
    partes = []
    for campo in CAMPOS:
        valores = leida.valores(campo)
        if valores:
            partes.append(f"{campo}={valores[0]}")
    return " ".join(partes) or "(sin campos)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--forzar", action="store_true", help="rehace las lecturas cacheadas")
    ap.add_argument("--trabajadores", type=int, default=4)
    ap.add_argument("--json", type=Path, help="escribe el informe crudo aqui")
    args = ap.parse_args()

    pdfs = sorted(Path(P.FACTURAS_POR_DEFECTO).glob("*.pdf"))
    if not pdfs:
        print(f"no hay corpus en {P.FACTURAS_POR_DEFECTO}", file=sys.stderr)
        return 1
    oro = {json.loads(l)["file_id"]: json.loads(l)["result"] for l in ORO.read_text().splitlines()}

    # --- 1. Cuantas facturas llegan de verdad al OCR -------------------------
    con_capa, sin_capa = [], []
    for pdf in pdfs:
        crudo, paginas = lectura.capa_texto(pdf)
        (con_capa if lectura.calidad_texto(crudo, paginas) >= 0.6 else sin_capa).append(pdf)

    print(f"corpus:                {len(pdfs)} facturas")
    print(f"  con capa de texto:   {len(con_capa)}  (el OCR no las toca)")
    print(f"  necesitan OCR:       {len(sin_capa)}")
    if not sin_capa:
        print("nada que medir: el OCR no decide en ninguna factura.")
        return 0
    reparto = collections.Counter(oro[p.name] for p in sin_capa)
    print(f"  oro de esas {len(sin_capa)}:  " + "  ".join(f"{k} {v}" for k, v in sorted(reparto.items())))
    print()

    # --- 2. Leer esas facturas con los dos motores ---------------------------
    def lee(par: tuple[Path, str]) -> dict:
        return _una_lectura(par[0], par[1], args.forzar)

    trabajos = [(p, m) for p in sin_capa for m in ("local", "cloud")]
    pendientes = sum(1 for p, m in trabajos if not (CACHE / f"{lectura.sha256_pdf(p)}.{m}.json").exists())
    print(f"lecturas: {len(trabajos)} ({pendientes} por hacer, {len(trabajos) - pendientes} en cache)")
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.trabajadores) as pool:
        hechas = list(pool.map(lee, trabajos))
    print(f"  {time.monotonic() - t0:.0f} s de reloj\n")

    por_clave = {(d["file_id"], d["motor_pedido"]): d for d in hechas}

    # --- 3. Un decisor por brazo, sobre las MISMAS lecturas ------------------
    brazos: dict[str, list[dict]] = {}
    for brazo in ("local", "nube", "escalera"):
        lecturas, filas = [], []
        for pdf in sin_capa:
            local = por_clave[(pdf.name, "local")]
            nube = por_clave[(pdf.name, "cloud")]
            paginas = local["paginas"] or 1
            if brazo == "local":
                elegida = {**local, "via": "local"}
            elif brazo == "nube":
                elegida = {**nube, "via": "nube"}
            else:
                elegida = _escalera(local, nube, paginas)
            leida = texto.extrae(elegida["texto"], pdf.name, paginas, "vision_ocr")
            lecturas.append(leida)
            filas.append({"pdf": pdf, "elegida": elegida, "leida": leida})

        # `construye_decisor` es barato pero el decisor acumula estado (pedidos
        # repetidos), asi que cada brazo necesita el suyo.
        decisor, maestro, _ = P.construye_decisor(
            Path(P.XLSX_POR_DEFECTO), P.CONFIG_POR_DEFECTO, Path(P.SNAPSHOT_POR_DEFECTO), None
        )
        for fila, decision in zip(filas, _decide_todo(decisor, lecturas)):
            fila["decision"] = decision.resultado
            fila["motivos"] = decision.motivos
        brazos[brazo] = filas

    # --- 3b. Brazo hibrido: el local decide, la nube rellena identificadores --
    vocab = S.vocabulario(maestro)
    lecturas_h, cambios_h = [], []
    for i in range(len(sin_capa)):
        leida, cambios = _hibrido(
            brazos["local"][i]["leida"], brazos["nube"][i]["leida"], maestro, vocab
        )
        lecturas_h.append(leida)
        cambios_h.append(cambios)

    decisor_h, _, _ = P.construye_decisor(
        Path(P.XLSX_POR_DEFECTO), P.CONFIG_POR_DEFECTO, Path(P.SNAPSHOT_POR_DEFECTO), None
    )
    filas_h = []
    for i, pdf in enumerate(sin_capa):
        # La latencia honesta: el local siempre, y la nube solo si hubo que
        # preguntarle por algun identificador.
        segundos = brazos["local"][i]["elegida"]["segundos"]
        if cambios_h[i]:
            segundos += brazos["nube"][i]["elegida"]["segundos"]
        filas_h.append({
            "pdf": pdf,
            "elegida": {**brazos["local"][i]["elegida"], "via": "local", "segundos": segundos},
            "leida": lecturas_h[i],
            "cambios": cambios_h[i],
        })
    for fila, decision in zip(filas_h, _decide_todo(decisor_h, lecturas_h)):
        fila["decision"] = decision.resultado
        fila["motivos"] = decision.motivos
    brazos["hibrido"] = filas_h

    # --- 4. Confusion contra el oro -----------------------------------------
    print("=" * 78)
    print("DECISIONES CONTRA EL ORO (solo las facturas que pasan por OCR)")
    print("=" * 78)
    for brazo, filas in brazos.items():
        aciertos = sum(1 for f in filas if f["decision"] == oro[f["pdf"].name])
        confusion = collections.Counter(
            (oro[f["pdf"].name], f["decision"]) for f in filas if f["decision"] != oro[f["pdf"].name]
        )
        segundos = [f["elegida"]["segundos"] for f in filas]
        print(f"\n{brazo:9} aciertos {aciertos}/{len(filas)}  "
              f"({100 * aciertos / len(filas):.0f}%)   "
              f"mediana {statistics.median(segundos):.1f} s/factura")
        if confusion:
            for (esperado, obtenido), n in sorted(confusion.items(), key=lambda x: -x[1]):
                print(f"            oro {esperado:9} -> {obtenido:9}  x{n}")
        else:
            print("            sin discrepancias")

    # --- 5. Donde discrepan los brazos y por que ---------------------------
    print()
    print("=" * 78)
    print("SUSTITUCIONES DEL BRAZO HIBRIDO (nube -> identificadores del local)")
    print("=" * 78)
    con_cambio = [(i, f) for i, f in enumerate(brazos["hibrido"]) if f["cambios"]]
    if not con_cambio:
        print("\nninguna: el local resolvio todos los identificadores por si solo.")
    for i, fila in con_cambio:
        local = brazos["local"][i]["leida"]
        antes = "  ".join(f"{c}={local.valores(c)[:2]}" for c in S.IDENTIFICADORES if local.valores(c))
        print(f"\n{fila['pdf'].name}   oro={oro[fila['pdf'].name]}")
        print(f"    local decia: {antes or '(nada)'}")
        print(f"    {'; '.join(fila['cambios'])}")
        print(f"    decision  local={brazos['local'][i]['decision']}"
              f"  hibrido={fila['decision']}")

    print()
    print("=" * 78)
    print("FACTURAS DONDE LOS BRAZOS NO COINCIDEN")
    print("=" * 78)
    for i, pdf in enumerate(sin_capa):
        decisiones = {b: brazos[b][i]["decision"] for b in brazos}
        if len(set(decisiones.values())) == 1:
            continue
        print(f"\n{pdf.name}   oro={oro[pdf.name]}   " +
              "  ".join(f"{b}={d}" for b, d in decisiones.items()))
        for brazo in ("local", "nube", "hibrido"):
            f = brazos[brazo][i]
            print(f"    {brazo:7} {_campos(f['leida'])}")
            if f["motivos"]:
                print(f"            motivos: {'; '.join(f['motivos'])[:150]}")
        via = brazos["escalera"][i]["elegida"].get("via")
        print(f"    escalera eligio: {via}")

    # --- 6. Cuanto se parecen los dos motores -----------------------------
    print()
    print("=" * 78)
    print("ACUERDO ENTRE MOTORES (sobre el texto)")
    print("=" * 78)
    identicos = sum(1 for pdf in sin_capa
                    if por_clave[(pdf.name, "local")]["texto"] == por_clave[(pdf.name, "cloud")]["texto"])
    campos_iguales = 0
    for i, pdf in enumerate(sin_capa):
        l = brazos["local"][i]["leida"]
        n = brazos["nube"][i]["leida"]
        if all(l.valores(c) == n.valores(c) for c in CAMPOS):
            campos_iguales += 1
    print(f"  texto byte-identico:        {identicos}/{len(sin_capa)}")
    print(f"  mismos campos extraidos:    {campos_iguales}/{len(sin_capa)}")

    if args.json:
        args.json.write_text(json.dumps({
            "corpus": len(pdfs),
            "necesitan_ocr": [p.name for p in sin_capa],
            "oro": {p.name: oro[p.name] for p in sin_capa},
            "brazos": {
                b: [{"file_id": f["pdf"].name, "via": f["elegida"].get("via"),
                     "motor_real": f["elegida"].get("motor_real"),
                     "segundos": f["elegida"].get("segundos"),
                     "decision": f["decision"], "oro": oro[f["pdf"].name],
                     "motivos": f["motivos"], "campos": _campos(f["leida"]),
                     "cambios": f.get("cambios")}
                    for f in filas]
                for b, filas in brazos.items()
            },
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\ninforme crudo: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
