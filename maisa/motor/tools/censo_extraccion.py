#!/usr/bin/env python3
"""Censo del error de extraccion que la norma tuvo que reparar.

El CER mide la lectura contra la capa de texto del PDF, pero eso solo existe
para los 471 documentos con texto embebido y no dice si el fallo llego a tocar
una decision. Este censo mide lo complementario: **cuantas facturas traian un
campo mal leido y hubo que repararlo antes de decidir**, con el desglose por
campo y con el denominador explicito (las facturas del lote).

Sale entero de la traza que el motor ya escribe, asi que no vuelve a leer un
solo PDF ni necesita el servicio de vision:

* ``campos.notas``          reparaciones de pedido, NIF e IBAN
                            (``norma.Decisor._resuelve_pedido``, ``_resuelve_nif``,
                            ``_resuelve_iban``).
* ``campos.notas_importe``  importes recompuestos o confirmados por la
                            aritmetica del documento
                            (``norma.Decisor._repara_importes_ocr``).
* ``escalon_lectura``       por donde se leyo cada factura.
* ``calidad_lectura``       la confianza que el lector se da a si mismo.
* ``sospechosos``           campos que el lector marca como dudosos.

Las notas se separan en dos familias, porque no significan lo mismo:

* **reparacion** -- el documento venia mal leido y el motor lo reconstruyo
  (anclado en el ERP o en la aritmetica). Es error de extraccion medido.
* **hueco** -- el dato no esta en el maestro ni en el ERP (o el documento cita
  otro distinto: una cuenta de abono que no es la del proveedor). No es un
  fallo de lectura: es algo que un humano tiene que resolver, y el motor lo
  escala en vez de arreglarlo.

Uso:

    PYTHONPATH=motor/src python motor/tools/censo_extraccion.py
    PYTHONPATH=motor/src python motor/tools/censo_extraccion.py --traza outputs/outcomes_traza.jsonl
    PYTHONPATH=motor/src python motor/tools/censo_extraccion.py --json /tmp/censo.json
    PYTHONPATH=motor/src python motor/tools/censo_extraccion.py --verbose

Codigo de salida: 0 si el censo se calcula, 1 si la traza no se puede leer.
Es una medida, no una puerta: no falla porque el numero sea alto.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parents[1]
TRAZA_POR_DEFECTO = RAIZ.parent / "outputs" / "outcomes_traza.jsonl"

REPARACION = "reparacion"
HUECO = "hueco"


@dataclass(frozen=True)
class Patron:
    """Una clase de nota de correccion: que se arreglo y de que campo."""

    etiqueta: str
    campo: str
    tipo: str
    regex: re.Pattern[str]


#: Los textos son los que escribe `norma.py`; el orden importa (el primero que
#: casa gana) y cualquier nota que no case sale en `sin_clasificar` en vez de
#: desaparecer del censo.
PATRONES: tuple[Patron, ...] = (
    Patron("pedido reparado (venia mal impreso)", "pedido", REPARACION,
           re.compile(r"^pedido \S+ reparado a \S+")),
    Patron("pedido rescatado por estructura (NIF + importe del ERP)", "pedido", REPARACION,
           re.compile(r"^pedido ilegible recuperado por estructura")),
    Patron("NIF corregido", "nif", REPARACION, re.compile(r"^NIF \S+ corregido a \S+")),
    Patron("IBAN corregido", "iban", REPARACION, re.compile(r"^IBAN \S+ corregido a \S+")),
    Patron("total recompuesto (separador decimal desalineado)", "importe", REPARACION,
           re.compile(r"^importe recompuesto a ")),
    Patron("total confirmado por la aritmetica del documento", "importe", REPARACION,
           re.compile(r"^total ilegible o ruidoso ")),
    Patron("base recompuesta", "importe", REPARACION,
           re.compile(r"^base \S+ recompuesta a \S+")),
    Patron("pedido citado que no existe en el ERP", "pedido", HUECO,
           re.compile(r"^pedido \S+ no existe en el ERP")),
    Patron("NIF que no figura en el maestro", "nif", HUECO,
           re.compile(r"^NIF \S+ no figura en el maestro")),
    Patron("IBAN que no figura en el maestro", "iban", HUECO,
           re.compile(r"^IBAN \S+ no figura en el maestro")),
)


def clasifica(nota: str) -> Patron | None:
    """La clase de una nota de correccion, o ``None`` si no la reconocemos."""
    limpia = (nota or "").strip()
    for patron in PATRONES:
        if patron.regex.search(limpia):
            return patron
    return None


def _familia_vacia() -> dict[str, Any]:
    return {"notas": 0, "facturas": set(), "por_campo": Counter(), "por_etiqueta": Counter()}


def _cierra(familia: dict[str, Any], total: int) -> dict[str, Any]:
    facturas = len(familia["facturas"])
    return {
        "notas": familia["notas"],
        "facturas": facturas,
        "tasa": round(facturas / total, 4) if total else None,
        "por_campo": dict(sorted(familia["por_campo"].items())),
        "por_etiqueta": dict(familia["por_etiqueta"].most_common()),
    }


def censo(filas: list[dict[str, Any]]) -> dict[str, Any]:
    """Resume una traza: reparaciones de lectura, huecos de dato y calidad."""
    familias = {REPARACION: _familia_vacia(), HUECO: _familia_vacia()}
    escalones: Counter[str] = Counter()
    sin_clasificar: Counter[str] = Counter()
    calidad: dict[str, list[float]] = {}
    sospechosos = 0
    detalle: list[dict[str, str]] = []

    for fila in filas:
        factura = fila.get("file_id", "?")
        escalones[str(fila.get("escalon_lectura", "?"))] += 1
        if fila.get("sospechosos"):
            sospechosos += 1
        valor = fila.get("calidad_lectura")
        if isinstance(valor, (int, float)):
            calidad.setdefault(str(fila.get("result", "?")), []).append(float(valor))

        campos = fila.get("campos") or {}
        notas = list(campos.get("notas") or []) + list(campos.get("notas_importe") or [])
        for nota in notas:
            patron = clasifica(str(nota))
            if patron is None:
                sin_clasificar[str(nota)] += 1
                continue
            familia = familias[patron.tipo]
            familia["notas"] += 1
            familia["facturas"].add(str(factura))
            familia["por_campo"][patron.campo] += 1
            familia["por_etiqueta"][patron.etiqueta] += 1
            detalle.append({
                "file_id": str(factura), "tipo": patron.tipo,
                "campo": patron.campo, "etiqueta": patron.etiqueta, "nota": str(nota),
            })

    total = len(filas)
    todas = [valor for valores in calidad.values() for valor in valores]
    return {
        "facturas": total,
        "escalones": dict(sorted(escalones.items())),
        "lectura": {
            "sospechosos": sospechosos,
            "calidad_media": round(sum(todas) / len(todas), 4) if todas else None,
            "calidad_por_resultado": {
                resultado: round(sum(valores) / len(valores), 4)
                for resultado, valores in sorted(calidad.items())
            },
        },
        REPARACION: _cierra(familias[REPARACION], total),
        HUECO: _cierra(familias[HUECO], total),
        "sin_clasificar": dict(sin_clasificar.most_common()),
        "detalle": detalle,
    }


def _imprime_familia(titulo: str, nota: str, familia: dict[str, Any], total: int) -> None:
    print(f"  {titulo}: {familia['facturas']}/{total} facturas "
          f"({100 * (familia['tasa'] or 0):.1f}%) con {familia['notas']} notas")
    print(f"    ({nota})")
    for etiqueta, veces in familia["por_etiqueta"].items():
        print(f"      {veces:4d}  {etiqueta}")
    if familia["por_campo"]:
        campos = "  ".join(f"{campo}={veces}" for campo, veces in familia["por_campo"].items())
        print(f"      por campo: {campos}")


def imprime(resumen: dict[str, Any], verbose: bool = False) -> None:
    total = resumen["facturas"]
    print("=" * 78)
    print(" CENSO DEL ERROR DE EXTRACCION (desde la traza)")
    print("=" * 78)
    print(f"  facturas en la traza : {total}")
    escalones = "  ".join(f"{k}={v}" for k, v in resumen["escalones"].items())
    print(f"  escalon de lectura   : {escalones}")
    lectura = resumen["lectura"]
    print(f"  calidad_lectura media: {lectura['calidad_media']}")
    por_resultado = "  ".join(
        f"{k}={v}" for k, v in lectura["calidad_por_resultado"].items()
    )
    print(f"  calidad por resultado: {por_resultado}")
    print(f"  facturas con campos sospechosos: {lectura['sospechosos']}")
    print()
    _imprime_familia("REPARACIONES (error de extraccion reconstruido)",
                     "el documento venia mal leido; el motor lo anclo en el ERP o en la aritmetica",
                     resumen[REPARACION], total)
    print()
    _imprime_familia("HUECOS (el dato no esta en el maestro ni en el ERP)",
                     "no es fallo de lectura: falta el dato",
                     resumen[HUECO], total)
    if resumen["sin_clasificar"]:
        print()
        print("  NOTAS SIN CLASIFICAR (revisar: puede ser un texto nuevo de norma.py)")
        for nota, veces in resumen["sin_clasificar"].items():
            print(f"      {veces:4d}  {nota}")
    if verbose:
        print()
        print("  DETALLE POR FACTURA")
        for fila in resumen["detalle"]:
            print(f"      {fila['file_id']:34s} {fila['tipo']:10s} "
                  f"{fila['campo']:8s} {fila['nota']}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cuenta el error de extraccion que la norma tuvo que reparar."
    )
    parser.add_argument("--traza", default=str(TRAZA_POR_DEFECTO),
                        help="JSONL de traza con los motivos y las notas por factura")
    parser.add_argument("--json", dest="json_out", help="volcar el censo crudo aqui")
    parser.add_argument("--verbose", action="store_true", help="una linea por nota")
    args = parser.parse_args(argv)

    ruta = Path(args.traza)
    if not ruta.exists():
        print(f"no existe la traza: {ruta}", file=sys.stderr)
        print("la traza se escribe junto a la entrega: python -m maisa.procesa", file=sys.stderr)
        return 1

    filas: list[dict[str, Any]] = []
    for numero, linea in enumerate(ruta.read_text(encoding="utf-8").splitlines(), 1):
        linea = linea.strip()
        if not linea:
            continue
        try:
            filas.append(json.loads(linea))
        except json.JSONDecodeError as exc:
            print(f"{ruta}:{numero}: linea ilegible: {exc}", file=sys.stderr)
            return 1

    resumen = censo(filas)
    imprime(resumen, verbose=args.verbose)
    if args.json_out:
        destino = Path(args.json_out)
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(json.dumps(resumen, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"censo crudo: {destino}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
