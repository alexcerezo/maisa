#!/usr/bin/env python3
"""Verificador externo de conformidad: contraste con un conjunto de referencia.

Nuestro motor decide PAGAR / ESCALAR / NO_PAGAR y se valida a si mismo con
`tools/oro.py` (banco de oro propio) y `tools/valida_entrega.py` (contrato de
formato). Los dos miran *nuestro* criterio. Este script mira desde fuera:
contrasta nuestras decisiones con una **referencia externa** que declara, por
factura, el conjunto de resultados que consideraria admisibles.

La referencia **no se versiona**: se pasa con `--referencia` y se trata como un
dato de entrada, nunca como parte del motor. Asi el repositorio no arrastra
artefactos de terceros y la herramienta vale con cualquier referencia que
respeten los dos formatos de abajo.

Fuentes admitidas (autodetecta por estructura, no por extension):

* JSON con envoltorio: `{"meta": ..., "files": {file_id: {...}}}`, con
  `verdict.acceptable` (conjunto admisible), `verdict.primary` (preferido) y
  `findings` (regla a regla, con `soft`).
* JSONL plano de decisiones (`result`, `expected`, `decision`...): se toma la
  decision de cada linea como conjunto admisible de un solo elemento.

Severidades de cada desacuerdo:

    FUERA_ALTO             nosotros PAGAR y la referencia no admite PAGAR: estamos
                           pagando algo que incumple la Norma (error real).
    FUERA_MEDIO            nosotros ESCALAR y la referencia no admite ESCALAR (solo
                           PAGAR/NO_PAGAR): nos pasamos de prudentes y bloqueamos
                           un pago que la referencia considera legitimo.
    FUERA_BAJO             el resto de discrepancias (p. ej. NO_PAGAR donde la
                           referencia prefiere ESCALAR).
    FALTA_EN_NUESTRO       factura que la referencia conoce y nosotros no decidimos
                           (incumple "un outcome por archivo"); cuenta como ALTO.
    FALTA_EN_REFERENCIA    decision nuestra sin contrapartida en la referencia
                           (fichero sobrante o de otro lote); cuenta como BAJO.
    ACEPTABLE_NO_PRIMARIO  estamos dentro de `acceptable` pero no coincidimos
                           con `primary` (informativo, no es un fallo).

Uso:
    python3 tools/conformidad.py --referencia <ruta.json|ruta.jsonl>
    python3 tools/conformidad.py --referencia <ruta> --outcomes outputs/outcomes.jsonl --verbose
    python3 tools/conformidad.py --referencia <ruta> --json

Codigo de salida:
    0  conformidad estricta: cero desacuerdos FUERA_* (y ningun fichero faltante
       en nuestro lado).
    2  solo desacuerdos de severidad baja o no-primarios: publicable, con matices.
    1  algun FUERA_ALTO o FUERA_MEDIO (o falta una decision nuestra): la referencia
       externa no admite lo que hacemos.

Funciona sin red y es determinista: siempre ordena por `file_id`.
"""

from __future__ import annotations

import argparse
import json
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

OUTCOMES_POR_DEFECTO = Path("/tmp/out/outcomes.jsonl")

RESULTADOS = ("PAGAR", "ESCALAR", "NO_PAGAR")
ALIAS_RESULTADO = {
    "PAGAR": "PAGAR", "PAGO": "PAGAR", "PAGARSE": "PAGAR", "SI": "PAGAR", "OK": "PAGAR",
    "NO PAGAR": "NO_PAGAR", "NOPAGAR": "NO_PAGAR", "NO": "NO_PAGAR",
    "RECHAZAR": "NO_PAGAR", "RECHAZO": "NO_PAGAR", "DENEGAR": "NO_PAGAR",
    "ESCALAR": "ESCALAR", "ESCALADO": "ESCALAR", "ESCALATE": "ESCALAR",
    "REVISAR": "ESCALAR", "REVISION": "ESCALAR", "REVIEW": "ESCALAR",
}
CLAVES_RESULTADO = ("result", "expected", "decision", "resultado", "outcome",
                    "verdict", "estado", "label")
CLAVES_FILE_ID = ("file_id", "filename", "archivo", "fichero", "pdf", "id")

# Etiquetas de clasificacion (el orden de ORDEN es el orden de la tabla).
OK = "OK"
FUERA_ALTO = "FUERA_ALTO"
FUERA_MEDIO = "FUERA_MEDIO"
FUERA_BAJO = "FUERA_BAJO"
NO_PRIMARIO = "ACEPTABLE_NO_PRIMARIO"
FALTA = "FALTA_EN_NUESTRO"
EXTRA = "FALTA_EN_REFERENCIA"
ORDEN = (FUERA_ALTO, FUERA_MEDIO, FUERA_BAJO, FALTA, EXTRA, NO_PRIMARIO, OK)

ALTO, MEDIO, BAJO, INFO, NADA = "ALTO", "MEDIO", "BAJO", "INFO", "OK"
SEVERIDAD = {
    FUERA_ALTO: ALTO, FALTA: ALTO,
    FUERA_MEDIO: MEDIO,
    FUERA_BAJO: BAJO, EXTRA: BAJO,
    NO_PRIMARIO: INFO,
    OK: NADA,
}
TITULO = {
    FUERA_ALTO: "FUERA_ALTO  (pagamos lo que la referencia no admite: error real)",
    FUERA_MEDIO: "FUERA_MEDIO (escalamos un pago que la referencia admite)",
    FUERA_BAJO: "FUERA_BAJO  (discrepancia de politica, no de Norma)",
    FALTA: "FALTA_EN_NUESTRO (la referencia conoce la factura y nosotros no la decidimos)",
    EXTRA: "FALTA_EN_REFERENCIA (decidimos una factura que la referencia no cubre)",
    NO_PRIMARIO: "ACEPTABLE_NO_PRIMARIO (dentro de lo admisible, no es el preferido)",
    OK: "OK (coincidencia exacta con el primario)",
}


@dataclass
class Opinion:
    """Lo que la referencia opina de una factura."""

    file_id: str
    primary: str | None
    acceptable: tuple[str, ...]
    fallos: tuple[str, ...] = ()
    blandos: tuple[str, ...] = ()
    mensajes: tuple[str, ...] = ()
    rationale: str = ""
    confianza: str = ""


@dataclass
class Fila:
    file_id: str
    clase: str
    nuestro: str | None
    primario: str | None
    acceptable: tuple[str, ...]
    fallos: tuple[str, ...] = ()
    blandos: tuple[str, ...] = ()
    mensajes: tuple[str, ...] = ()
    rationale: str = ""
    confianza: str = ""

    @property
    def severidad(self) -> str:
        return SEVERIDAD[self.clase]

    def reglas(self) -> str:
        if not self.fallos:
            return "-"
        return ", ".join(f"{r} (soft)" if r in self.blandos else r for r in self.fallos)


@dataclass
class Informe:
    filas: list[Fila] = field(default_factory=list)
    problemas: list[str] = field(default_factory=list)
    datos: list[str] = field(default_factory=list)
    notas: list[str] = field(default_factory=list)


def _sin_tildes(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto)
                   if unicodedata.category(c) != "Mn")


def normaliza_resultado(valor) -> str | None:
    """Devuelve PAGAR / ESCALAR / NO_PAGAR, o None si el valor no es del enum."""
    if not isinstance(valor, str):
        return None
    texto = " ".join(_sin_tildes(valor).strip().upper().replace("_", " ").split())
    return ALIAS_RESULTADO.get(texto)


def _fallos_de_findings(findings) -> tuple[list[str], list[str], list[str]]:
    """(reglas falladas, subconjunto blando, mensajes) a partir de `findings`."""
    fallos: list[str] = []
    blandos: list[str] = []
    mensajes: list[str] = []
    for f in findings or []:
        if not isinstance(f, dict):
            continue
        regla = f.get("rule") or f.get("regla")
        if not regla or f.get("passed", True):
            continue
        if regla not in fallos:
            fallos.append(regla)
        if f.get("soft") and regla not in blandos:
            blandos.append(regla)
        mensaje = f.get("message") or f.get("mensaje")
        if mensaje:
            mensajes.append(f"{regla}: {mensaje}")
    return fallos, blandos, mensajes


def _reglas_de_traza(rec: dict) -> tuple[list[str], list[str], list[str]]:
    """Reglas falladas de un JSONL de referencia (findings o listas tipo `REGLA:FAIL`)."""
    fallos, blandos, mensajes = _fallos_de_findings(rec.get("findings"))
    for clave in ("rule_ids", "rules", "reglas"):
        for item in rec.get(clave) or []:
            if not isinstance(item, str) or ":" not in item:
                continue
            nombre, _, estado = item.rpartition(":")
            estado = estado.strip().upper()
            if estado in ("FAIL", "FALLO", "FALSE", "KO", "NO") and nombre.strip():
                if nombre.strip() not in fallos:
                    fallos.append(nombre.strip())
    return fallos, blandos, mensajes


def _opinion_de_oracle(file_id: str, entrada: dict) -> Opinion:
    verdict = entrada.get("verdict") or {}
    fallos, blandos, mensajes = _fallos_de_findings(entrada.get("findings"))
    # El veredicto manda: si nombra fallos que no estan en findings, se anaden.
    for regla in verdict.get("hard_failures") or []:
        if regla not in fallos:
            fallos.append(regla)
    for regla in verdict.get("soft_flags") or []:
        if regla not in fallos:
            fallos.append(regla)
        if regla not in blandos:
            blandos.append(regla)
    primary = normaliza_resultado(verdict.get("primary"))
    acceptable = tuple(a for a in (normaliza_resultado(x)
                                   for x in verdict.get("acceptable") or []) if a)
    if not acceptable and primary:
        acceptable = (primary,)
    return Opinion(file_id=file_id, primary=primary, acceptable=acceptable,
                   fallos=tuple(fallos), blandos=tuple(blandos),
                   mensajes=tuple(mensajes),
                   rationale=str(verdict.get("rationale") or ""),
                   confianza=str(verdict.get("confidence") or ""))


def _opiniones_de_jsonl(texto: str, problemas: list[str]) -> dict[str, Opinion]:
    opiniones: dict[str, Opinion] = {}
    clave_resultado: str | None = None
    malas = 0
    repetidas = 0
    for numero, linea in enumerate(texto.splitlines(), start=1):
        linea = linea.strip()
        if not linea:
            continue
        try:
            rec = json.loads(linea)
        except json.JSONDecodeError:
            malas += 1
            continue
        if not isinstance(rec, dict):
            malas += 1
            continue
        file_id = next((rec[c] for c in CLAVES_FILE_ID
                        if isinstance(rec.get(c), str) and rec.get(c)), None)
        if not file_id:
            malas += 1
            continue
        claves = [clave_resultado] if clave_resultado else list(CLAVES_RESULTADO)
        resultado = None
        for clave in claves:
            if clave is None:
                continue
            resultado = normaliza_resultado(rec.get(clave))
            if resultado:
                clave_resultado = clave
                break
        if not resultado:
            malas += 1
            continue
        if file_id in opiniones:
            repetidas += 1
        fallos, blandos, mensajes = _reglas_de_traza(rec)
        opiniones[file_id] = Opinion(
            file_id=file_id, primary=resultado, acceptable=(resultado,),
            fallos=tuple(fallos), blandos=tuple(blandos), mensajes=tuple(mensajes),
            rationale=str(rec.get("why") or rec.get("rationale") or rec.get("motivo") or ""),
            confianza=str(rec.get("confidence") or rec.get("confianza") or ""))
    if malas:
        problemas.append(f"referencia JSONL: {malas} linea(s) sin file_id/resultado reconocible")
    if repetidas:
        problemas.append(f"referencia JSONL: {repetidas} file_id repetido(s); gana la ultima linea")
    return opiniones


def lee_referencia(ruta: Path, inf: Informe) -> tuple[dict[str, Opinion], dict, str]:
    """Lee `oracle.json` o un JSONL de referencia. Devuelve (opiniones, meta, tipo)."""
    texto = ruta.read_text(encoding="utf-8-sig")
    try:
        obj = json.loads(texto)
    except json.JSONDecodeError:
        obj = None
    if isinstance(obj, dict) and isinstance(obj.get("files"), dict):
        opiniones = {}
        for file_id, entrada in obj["files"].items():
            if not isinstance(entrada, dict):
                inf.problemas.append(f"referencia: entrada no valida para {file_id}")
                continue
            opiniones[file_id] = _opinion_de_oracle(file_id, entrada)
        sin_veredicto = [f for f, o in opiniones.items() if not o.acceptable]
        if sin_veredicto:
            inf.problemas.append(
                f"referencia: {len(sin_veredicto)} factura(s) sin `acceptable` ni `primary` "
                f"(se tratan como sin opinion): {', '.join(sorted(sin_veredicto)[:5])}")
            for file_id in sin_veredicto:
                del opiniones[file_id]
        return opiniones, obj.get("meta") or {}, "referencia"
    return _opiniones_de_jsonl(texto, inf.problemas), {}, "jsonl"


def lee_outcomes(ruta: Path, inf: Informe) -> dict[str, str]:
    """`file_id -> result` de nuestras decisiones (o de cualquier JSONL)."""
    decisiones: dict[str, str] = {}
    malas = 0
    repetidas = 0
    for linea in ruta.read_text(encoding="utf-8-sig").splitlines():
        linea = linea.strip()
        if not linea:
            continue
        try:
            rec = json.loads(linea)
        except json.JSONDecodeError:
            malas += 1
            continue
        if not isinstance(rec, dict):
            malas += 1
            continue
        file_id = next((rec[c] for c in CLAVES_FILE_ID
                        if isinstance(rec.get(c), str) and rec.get(c)), None)
        resultado = None
        for clave in CLAVES_RESULTADO:
            resultado = normaliza_resultado(rec.get(clave))
            if resultado:
                break
        if not file_id or not resultado:
            malas += 1
            continue
        if file_id in decisiones:
            repetidas += 1
        decisiones[file_id] = resultado
    if malas:
        inf.problemas.append(f"outcomes: {malas} linea(s) sin file_id/resultado reconocible")
    if repetidas:
        inf.problemas.append(f"outcomes: {repetidas} file_id repetido(s); gana la ultima linea")
    return decisiones


def clasifica(nuestro: str | None, opinion: Opinion | None) -> str:
    """Etiqueta de severidad de un par (nuestra decision, opinion de la referencia)."""
    if nuestro is None:
        return FALTA
    if opinion is None:
        return EXTRA
    if nuestro in opinion.acceptable:
        return OK if nuestro == opinion.primary else NO_PRIMARIO
    if nuestro == "PAGAR":
        return FUERA_ALTO
    if nuestro == "ESCALAR":
        return FUERA_MEDIO
    return FUERA_BAJO


def _fila(file_id: str, nuestro: str | None, opinion: Opinion | None) -> Fila:
    return Fila(
        file_id=file_id, clase=clasifica(nuestro, opinion), nuestro=nuestro,
        primario=opinion.primary if opinion else None,
        acceptable=opinion.acceptable if opinion else (),
        fallos=opinion.fallos if opinion else (),
        blandos=opinion.blandos if opinion else (),
        mensajes=opinion.mensajes if opinion else (),
        rationale=opinion.rationale if opinion else "",
        confianza=opinion.confianza if opinion else "")


def _recorta(texto: str, ancho: int) -> str:
    return texto if len(texto) <= ancho else texto[:ancho - 1] + "\u2026"


def _tabla(filas: list[Fila], columnas: int = 118) -> list[str]:
    cabecera = ("file_id", "nuestro", "primario", "admisibles", "reglas N*")
    ancho_id = min(max([len(cabecera[0])] + [len(f.file_id) for f in filas]), 34)
    ancho_adm = min(max([len(cabecera[3])] + [len("/".join(f.acceptable)) for f in filas]), 24)
    resto = max(columnas - (ancho_id + 7 + 8 + ancho_adm + 4 + 6), 20)
    lineas = ["   " + "  ".join((
        cabecera[0].ljust(ancho_id), cabecera[1].ljust(7), cabecera[2].ljust(8),
        cabecera[3].ljust(ancho_adm), cabecera[4]))]
    lineas.append("   " + "  ".join((
        "-" * ancho_id, "-" * 7, "-" * 8, "-" * ancho_adm, "-" * 6)))
    for f in filas:
        lineas.append("   " + "  ".join((
            _recorta(f.file_id, ancho_id).ljust(ancho_id),
            (f.nuestro or "-").ljust(7),
            (f.primario or "-").ljust(8),
            _recorta("/".join(f.acceptable) or "-", ancho_adm).ljust(ancho_adm),
            _recorta(f.reglas(), resto))))
    return lineas


def _cuenta_por_clase(filas: list[Fila]) -> Counter:
    return Counter(f.clase for f in filas)


def _veredicto(conteo: Counter) -> tuple[str, str]:
    altos = conteo[FUERA_ALTO]
    medios = conteo[FUERA_MEDIO]
    faltas = conteo[FALTA]
    bajos = conteo[FUERA_BAJO] + conteo[EXTRA]
    no_primarios = conteo[NO_PRIMARIO]
    if altos or medios or faltas:
        partes = []
        if altos:
            partes.append(f"{altos} decision(es) de riesgo alto (FUERA_ALTO)")
        if medios:
            partes.append(f"{medios} de riesgo medio (FUERA_MEDIO)")
        if faltas:
            partes.append(f"{faltas} factura(s) sin decision nuestra")
        return "NO CONFORME", ("la referencia externa no admite nuestro criterio en "
                               + " y ".join(partes))
    if bajos or no_primarios:
        return "CONFORME CON MATICES", (f"cero FUERA_ALTO/MEDIO; {bajos} discrepancia(s) de politica "
                                        f"y {no_primarios} decision(es) admisible(s) no preferida(s)")
    return "CONFORME (ESTRICTO)", "coincidimos con la referencia en todas las facturas"


def _salida_codigo(conteo: Counter) -> int:
    if conteo[FUERA_ALTO] or conteo[FUERA_MEDIO] or conteo[FALTA]:
        return 1
    if conteo[FUERA_BAJO] or conteo[EXTRA] or conteo[NO_PRIMARIO]:
        return 2
    return 0


def _imprime_texto(inf: Informe, meta: dict, tipo: str, ruta_out: Path, ruta_ora: Path,
                   decisiones: dict[str, str], opiniones: dict[str, Opinion],
                   verbose: bool) -> int:
    filas = inf.filas
    conteo = _cuenta_por_clase(filas)
    codigo = _salida_codigo(conteo)
    veredicto, motivo = _veredicto(conteo)

    print("=" * 78)
    print("REFERENCIA EXTERNA -- conformidad de nuestras decisiones")
    print("=" * 78)

    print("\n-- datos")
    dist_nuestra = Counter(decisiones.values())
    dist_referencia = Counter(o.primary for o in opiniones.values() if o.primary)
    print(f"   · outcomes : {ruta_out} ({len(decisiones)} decisiones: "
          f"{_formato_dist(dist_nuestra)})")
    etiqueta = ("referencia con conjunto admisible" if tipo == "referencia"
                else "JSONL de referencia (conjunto admisible = su unica decision)")
    print(f"   · referencia  : {ruta_ora} [{etiqueta}] ({len(opiniones)} facturas: "
          f"{_formato_dist(dist_referencia)})")
    if meta:
        rutas = meta.get("routes") or {}
        print(f"   · meta     : generado {meta.get('generated_at')} · hoy {meta.get('today')} "
              f"· rutas {_formato_dist(Counter(rutas))} · ilegibles {len(meta.get('unreadable') or [])}")
    comunes = sorted(set(decisiones) & set(opiniones))
    exactos = sum(1 for f in comunes if decisiones[f] == opiniones[f].primary)
    admisibles = sum(1 for f in comunes if decisiones[f] in opiniones[f].acceptable)
    pct = (100.0 * admisibles / len(comunes)) if comunes else 0.0
    print(f"   · facturas en ambos: {len(comunes)} · coincidencia con el primario: "
          f"{exactos}/{len(comunes)} · dentro de `acceptable`: {admisibles}/{len(comunes)} "
          f"({pct:.1f}%)")
    for dato in inf.datos:
        print(f"   · {dato}")

    for clase in ORDEN:
        grupo = [f for f in filas if f.clase == clase]
        if not grupo or (clase == OK and not verbose):
            continue
        print(f"\n-- {TITULO[clase]} [{len(grupo)}]")
        print("\n".join(_tabla(grupo)))
        if verbose:
            for f in grupo:
                if not f.mensajes and not f.rationale:
                    continue
                print(f"   · {f.file_id}")
                for mensaje in f.mensajes:
                    print(f"       - {mensaje}")
                if f.rationale:
                    print(f"       = {f.rationale}")

    print("\n-- contador")
    for clase in ORDEN:
        print(f"   {clase.ljust(24)} {conteo[clase]:>4}   ({SEVERIDAD[clase]})")
    print(f"   {'TOTAL':<24} {len(filas):>4}")
    print(f"   severidades: ALTO {conteo[FUERA_ALTO] + conteo[FALTA]} · "
          f"MEDIO {conteo[FUERA_MEDIO]} · BAJO {conteo[FUERA_BAJO] + conteo[EXTRA]} · "
          f"INFO {conteo[NO_PRIMARIO]}")
    reglas = Counter()
    for f in filas:
        if f.clase == OK:
            continue
        for regla in f.fallos:
            reglas[regla] += 1
    if reglas:
        top = " · ".join(f"{r} x{n}" for r, n in sorted(reglas.items(),
                                                        key=lambda kv: (-kv[1], kv[0]))[:8])
        print(f"   reglas que fallaron en los desacuerdos: {top}")

    if inf.problemas:
        print("\n-- problemas")
        for problema in inf.problemas:
            print(f"   ! {problema}")
    if inf.notas:
        print("\n-- notas")
        for nota in inf.notas:
            print(f"   · {nota}")

    print(f"\nVEREDICTO: {veredicto} -- {motivo}")
    print(f"   exit {codigo} (0 estricto · 2 solo bajo/no primario · 1 algun ALTO/MEDIO)")
    return codigo


def _formato_dist(contador: Counter) -> str:
    if not contador:
        return "vacio"
    return " ".join(f"{k} {contador[k]}" for k in sorted(contador))


def _imprime_json(inf: Informe, meta: dict, tipo: str, ruta_out: Path, ruta_ora: Path,
                  decisiones: dict[str, str], opiniones: dict[str, Opinion]) -> int:
    filas = inf.filas
    conteo = _cuenta_por_clase(filas)
    codigo = _salida_codigo(conteo)
    veredicto, motivo = _veredicto(conteo)
    comunes = sorted(set(decisiones) & set(opiniones))
    salida = {
        "origen": {
            "outcomes": str(ruta_out),
            "referencia": str(ruta_ora),
            "tipo_referencia": tipo,
        },
        "resumen": {
            "nuestras_decisiones": len(decisiones),
            "facturas_referencia": len(opiniones),
            "facturas_en_ambos": len(comunes),
            "distribucion_nuestra": dict(sorted(Counter(decisiones.values()).items())),
            "distribucion_referencia_primario": dict(sorted(
                Counter(o.primary for o in opiniones.values() if o.primary).items())),
            "coincidencia_primario": sum(1 for f in comunes
                                         if decisiones[f] == opiniones[f].primary),
            "dentro_acceptable": sum(1 for f in comunes
                                     if decisiones[f] in opiniones[f].acceptable),
            "conteo_clase": {clase: conteo[clase] for clase in ORDEN},
            "conteo_severidad": {
                "ALTO": conteo[FUERA_ALTO] + conteo[FALTA],
                "MEDIO": conteo[FUERA_MEDIO],
                "BAJO": conteo[FUERA_BAJO] + conteo[EXTRA],
                "INFO": conteo[NO_PRIMARIO],
            },
            "reglas_falladas_desacuerdos": dict(sorted(Counter(
                r for f in filas if f.clase != OK for r in f.fallos).items())),
        },
        "desacuerdos": [
            {
                "file_id": f.file_id,
                "clase": f.clase,
                "severidad": f.severidad,
                "nuestro": f.nuestro,
                "primario": f.primario,
                "acceptable": list(f.acceptable),
                "reglas": list(f.fallos),
                "reglas_soft": list(f.blandos),
                "rationale": f.rationale,
                "confianza": f.confianza,
            }
            for f in filas if f.clase != OK
        ],
        "veredicto": veredicto,
        "motivo": motivo,
        "salida": codigo,
    }
    if meta:
        salida["origen"]["meta_referencia"] = meta
    if inf.problemas:
        salida["problemas"] = inf.problemas
    print(json.dumps(salida, ensure_ascii=False, indent=2, sort_keys=False))
    return codigo


def construye(args) -> tuple[Informe, dict, str, dict[str, str], dict[str, Opinion]]:
    inf = Informe()
    ruta_out = Path(args.outcomes).expanduser()
    ruta_ora = Path(args.referencia).expanduser()
    for ruta, nombre in ((ruta_out, "outcomes"), (ruta_ora, "referencia")):
        if not ruta.is_file():
            inf.problemas.append(f"{nombre}: no existe {ruta}")
    if inf.problemas:
        return inf, {}, "", {}, {}
    decisiones = lee_outcomes(ruta_out, inf)
    opiniones, meta, tipo = lee_referencia(ruta_ora, inf)
    for file_id in sorted(set(decisiones) | set(opiniones)):
        inf.filas.append(_fila(file_id, decisiones.get(file_id), opiniones.get(file_id)))
    inf.filas.sort(key=lambda f: f.file_id)
    if tipo == "jsonl":
        inf.notas.append("comparacion con un JSONL de referencia: su unica decision se toma como conjunto "
                         "admisible de un elemento, asi que FUERA_ALTO/MEDIO son mas probables que "
                         "con `oracle.json` (que declara el abanico de resultados admisibles)")
    return inf, meta, tipo, decisiones, opiniones


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verifica por fuera si nuestras decisiones caen en el conjunto de resultados "
                    "admisibles que declara un fichero de referencia externo.",
        epilog="Codigo de salida: 0 conformidad estricta (cero FUERA_*); 2 solo desacuerdos de "
               "severidad baja o no-primarios; 1 algun FUERA_ALTO o FUERA_MEDIO (o falta una "
               "decision nuestra).",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--outcomes", default=str(OUTCOMES_POR_DEFECTO),
                        help="JSONL con nuestras decisiones (por defecto %(default)s)")
    parser.add_argument("--referencia", required=True,
                        help="JSON con envoltorio `{\"files\": {...}}` o JSONL plano de "
                             "decisiones; es un dato de entrada y no se versiona")
    parser.add_argument("--json", action="store_true",
                        help="salida maquina (JSON) en lugar de la tabla legible")
    parser.add_argument("--verbose", action="store_true",
                        help="incluye las facturas OK y el detalle de cada fallo")
    args = parser.parse_args(argv)

    inf, meta, tipo, decisiones, opiniones = construye(args)
    if not decisiones or not opiniones:
        print("REFERENCIA EXTERNA -- no se puede comparar")
        for problema in inf.problemas:
            print(f"   ! {problema}")
        return 1
    if args.json:
        return _imprime_json(inf, meta, tipo, Path(args.outcomes).expanduser(),
                             Path(args.referencia).expanduser(), decisiones, opiniones)
    return _imprime_texto(inf, meta, tipo, Path(args.outcomes).expanduser(),
                          Path(args.referencia).expanduser(), decisiones, opiniones, args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
