#!/usr/bin/env python3
"""Valida el arbol de entrega del track Maisa antes de publicarlo.

La spec oficial (`corpus/maisa/README.md`) exige un repositorio **separado** cuya
raiz contenga *exactamente* `la-caja-outcomes/` con tres ficheros: dos JSONL (un
objeto por cada archivo de La Caja) y `albertitos_plan.pdf`. La validacion del
jurado es binaria: exactamente un outcome por archivo y `result` en el enum. Un
solo fallo de formato invalida la entrega entera, asi que este script audita el
arbol completo antes de hacer `git push`.

Las comprobaciones semanticas de linea (enum, duplicados, espacios, cobertura)
se delegan en `maisa.emit.valida_jsonl` para no tener dos verdades distintas:
este script solo anade lo que `emit` no cubre (bytes, BOM, CRLF, PDF y la
politica de "repositorio publicable").

Uso:
    PYTHONPATH=src .venv/bin/python maisa/tools/valida_entrega.py /tmp/maisa-entrega
    PYTHONPATH=src .venv/bin/python maisa/tools/valida_entrega.py /tmp/maisa-delivery --publicable

Codigo de salida: 0 si no hay problemas bloqueantes, 1 en caso contrario.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

RAIZ_REPO = Path(__file__).resolve().parents[2]
# El motor vive en `motor/src` (repositorio de entrega) o en `maisa/src` (arbol
# de desarrollo); el corpus, en `data/` o en `corpus/maisa/`.
_SRC = next((p for p in (RAIZ_REPO / "motor" / "src", RAIZ_REPO / "maisa" / "src")
             if p.is_dir()), RAIZ_REPO / "maisa" / "src")
_CORPUS = next((p for p in (RAIZ_REPO / "data", RAIZ_REPO / "corpus" / "maisa")
                if p.is_dir()), RAIZ_REPO / "corpus" / "maisa")
sys.path.insert(0, str(_SRC))

from maisa.emit import RESULTADOS, normaliza_file_id, valida_jsonl  # noqa: E402

CARPETA = "la-caja-outcomes"
FICHEROS = ("outcomes.jsonl", "outcomes_lote2.jsonl", "albertitos_plan.pdf")
LOTE2 = "outcomes_lote2.jsonl"

BLOQUEANTE = "BLOQUEANTE"
AVISO = "AVISO"
INFO = "INFO"

# Patrones de lo que la spec prohibe subir: solucion, aplicacion ejecutable, credenciales.
CODIGO = ("*.py", "*.rs", "*.ts", "*.js", "*.go", "*.java", "*.sh", "Cargo.toml",
          "Cargo.lock", "package.json", "pyproject.toml", "requirements.txt",
          "Dockerfile", "docker-compose*.yml", "docker-compose*.yaml", "Makefile")
CREDENCIALES = (".env", ".env.*", "*.pem", "*.key", "*.p12", "id_rsa*",
                "credentials*", "*credentials.json", "*secret*")
PLANTILLAS_ENV = {".env.example", ".env.sample", ".env.template", ".env.dist"}
IGNORAR_RECURSIVO = {".git"}


@dataclass
class Problema:
    nivel: str
    donde: str
    mensaje: str

    def __str__(self) -> str:
        return f"[{self.nivel}] {self.donde}: {self.mensaje}"


@dataclass
class Informe:
    problemas: list[Problema] = field(default_factory=list)
    notas: list[str] = field(default_factory=list)
    datos: list[str] = field(default_factory=list)

    def anota(self, nivel: str, donde: str, mensaje: str) -> None:
        self.problemas.append(Problema(nivel, donde, mensaje))

    def error(self, donde: str, mensaje: str) -> None:
        self.anota(BLOQUEANTE, donde, mensaje)

    def aviso(self, donde: str, mensaje: str) -> None:
        self.anota(AVISO, donde, mensaje)

    def nota(self, texto: str) -> None:
        self.notas.append(texto)

    def dato(self, texto: str) -> None:
        self.datos.append(texto)

    @property
    def errores(self) -> list[Problema]:
        return [p for p in self.problemas if p.nivel == BLOQUEANTE]


def plano(texto: str) -> str:
    """Minusculas sin acentos, para comparar titulos de seccion sin pelearse con el PDF."""
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()


def revisa_estructura(raiz: Path, inf: Informe, publicable: bool) -> Path | None:
    carpeta = raiz / CARPETA
    if not carpeta.is_dir():
        anidados = sorted(p for p in raiz.rglob(CARPETA) if p.is_dir())
        extra = f" (encontrada anidada en {anidados[0].relative_to(raiz)}: la spec pide la raiz)" if anidados else ""
        inf.error(CARPETA, f"no existe el directorio {CARPETA}/ en la raiz de la entrega{extra}")
        return None

    presentes = sorted(p.name for p in carpeta.iterdir() if p.name not in IGNORAR_RECURSIVO)
    faltan = [f for f in FICHEROS if f not in presentes]
    sobran = [f for f in presentes if f not in FICHEROS]
    if faltan:
        inf.error(CARPETA, f"faltan ficheros obligatorios: {', '.join(faltan)}")
    if sobran:
        nivel = "estricto" if publicable else "aviso"
        inf.anota(BLOQUEANTE if publicable else AVISO, CARPETA,
                  f"ficheros extra dentro de {CARPETA}/ ({nivel}): {', '.join(sobran)}"
                  " -- la spec dice 'exactamente estos tres archivos'")
    inf.dato(f"{CARPETA}/ contiene: {', '.join(presentes) if presentes else '(vacio)'}")
    return carpeta


def revisa_bytes_jsonl(ruta: Path, inf: Informe) -> list[tuple[int, object]]:
    """Chequeos a nivel de byte/linea que `emit.valida_jsonl` no cubre. Devuelve las lineas parseadas."""
    filas: list[tuple[int, object]] = []
    raw = ruta.read_bytes()
    if not raw:
        inf.error(ruta.name, "fichero vacio (0 bytes): el jurado espera un outcome por archivo")
        return filas
    if raw.startswith(b"\xef\xbb\xbf"):
        inf.error(ruta.name, "empieza con BOM UTF-8: el verificador privado puede fallar al parsear la primera linea")
    try:
        texto = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        inf.error(ruta.name, f"no es UTF-8 valido: {exc}")
        return filas
    if b"\r\n" in raw:
        inf.error(ruta.name, "usa CRLF: la spec no lo exige, pero un split por '\\n' deja '\\r' pegado al JSON")
    elif b"\r" in raw:
        inf.error(ruta.name, "contiene CR sueltos (finales de linea clasicos)")
    if not raw.endswith(b"\n"):
        inf.aviso(ruta.name, "la ultima linea no termina en \\n (cuenta bien, pero es fragil al concatenar)")
    lineas = texto.split("\n")
    if lineas and lineas[-1] == "":
        lineas.pop()
    vacias = [i for i, ln in enumerate(lineas, 1) if not ln.strip()]
    if vacias:
        inf.error(ruta.name, f"{len(vacias)} linea(s) vacia(s) en {vacias[:10]}")
    for num, ln in enumerate(lineas, 1):
        if not ln.strip():
            continue
        try:
            fila = json.loads(ln)
        except json.JSONDecodeError as exc:
            inf.error(ruta.name, f"linea {num}: JSON invalido: {exc}")
            continue
        if not isinstance(fila, dict):
            inf.error(ruta.name, f"linea {num}: se espera un objeto JSON, llego {type(fila).__name__}")
        filas.append((num, fila))
    return filas


def _valida_inline(nombre: str, filas: list[tuple[int, object]], pdfs: list[Path], corpus_ok: bool,
                   inf: Informe) -> None:
    """Espejo minimo de `emit.valida_jsonl` para el caso en que este no puede analizar el fichero.

    `emit.valida_jsonl` asume que cada linea es un objeto JSON y revienta con
    AttributeError si alguna es una lista o un escalar. Ese caso ya es bloqueante
    por si mismo, pero no queremos perder los demas diagnosticos, asi que
    repetimos aqui solo enum, duplicados y cobertura reutilizando `RESULTADOS` y
    `normaliza_file_id` de `emit` (una sola verdad para el enum y la normalizacion).
    """
    vistos: dict[str, int] = {}
    for num, fila in filas:
        if not isinstance(fila, dict):
            continue
        fid, res = fila.get("file_id"), fila.get("result")
        if not isinstance(fid, str) or not fid:
            inf.error(nombre, f"linea {num}: file_id ausente o no textual")
        else:
            if fid != fid.strip():
                inf.error(nombre, f"linea {num}: file_id con espacios alrededor: {fid!r}")
            clave = normaliza_file_id(fid)
            if clave in vistos:
                inf.error(nombre, f"linea {num}: file_id duplicado: {fid!r} (ya en linea {vistos[clave]})")
            vistos[clave] = num
        if res not in RESULTADOS:
            inf.error(nombre, f"linea {num}: result fuera del enum: {res!r}")
    if corpus_ok:
        esperados = {normaliza_file_id(p.name) for p in pdfs}
        faltan = sorted(k for k in esperados - set(vistos))
        sobran = sorted(k for k in set(vistos) - esperados)
        if faltan:
            inf.error(nombre, f"faltan {len(faltan)} ficheros: {faltan[:10]}")
        if sobran:
            inf.error(nombre, f"sobran {len(sobran)} entradas: {sobran[:10]}")
        if len(vistos) != len(pdfs):
            inf.error(nombre, f"hay {len(vistos)} entradas y {len(pdfs)} ficheros")


def corpus_de(ruta: Path | None) -> list[Path]:
    if ruta is None or not ruta.is_dir():
        return []
    return sorted(ruta.glob("*.pdf"))


def corpus_lote2_por_defecto() -> Path | None:
    """El lote 2 lo envia Alberto el sabado; puede no existir todavia en el repo."""
    for candidato in ("facturas_lote2", "lote2", "facturas_extra"):
        ruta = _CORPUS / candidato
        if ruta.is_dir():
            return ruta
    return None


def revisa_jsonl(ruta: Path, pdfs: list[Path], corpus_ok: bool, inf: Informe) -> None:
    filas = revisa_bytes_jsonl(ruta, inf)
    objetos = all(isinstance(f, dict) for _, f in filas)
    fids = [f["file_id"] for _, f in filas if isinstance(f, dict) and isinstance(f.get("file_id"), str) and f["file_id"]]
    for fid in fids:
        if "/" in fid or "\\" in fid:
            inf.aviso(ruta.name, f"file_id con separador de ruta: {fid!r}")
    if objetos:
        try:
            problemas = valida_jsonl(ruta, pdfs if corpus_ok else [Path(f) for f in fids])
        except Exception as exc:  # noqa: BLE001 - un fichero raro no debe tumbar el validador
            inf.error(ruta.name, f"emit.valida_jsonl no pudo analizarlo ({type(exc).__name__}: {exc}); "
                                 "seguimos con las comprobaciones locales")
            _valida_inline(ruta.name, filas, pdfs, corpus_ok, inf)
        else:
            for p in problemas:
                inf.error(ruta.name, p)
    else:
        _valida_inline(ruta.name, filas, pdfs, corpus_ok, inf)
    conteo: dict[str, int] = {}
    for _, fila in filas:
        if isinstance(fila, dict) and fila.get("result") in RESULTADOS:
            conteo[fila["result"]] = conteo.get(fila["result"], 0) + 1
    reparto = " / ".join(f"{conteo.get(r, 0)} {r}" for r in RESULTADOS)
    inf.dato(f"{ruta.name}: {len(fids)} lineas con file_id -- {reparto}")
    if corpus_ok:
        inf.dato(f"{ruta.name}: contrastado contra {len(pdfs)} PDFs de {pdfs[0].parent}")
    else:
        inf.nota(f"{ruta.name}: corpus del lote 2 no disponible -> cobertura NO contrastada "
                 "(la spec anuncia 40 facturas adicionales el sabado 18:00)")
        inf.dato(f"{ruta.name}: cobertura no contrastable (sin corpus de referencia)")


def revisa_plan(ruta: Path, inf: Informe) -> None:
    if not ruta.exists():
        inf.error(ruta.name, "no existe")
        return
    if ruta.stat().st_size == 0:
        inf.error(ruta.name, "PDF de 0 bytes")
        return
    if ruta.read_bytes()[:4] != b"%PDF":
        inf.error(ruta.name, "no empieza por '%PDF': no es un PDF real")
        return
    try:
        from pypdf import PdfReader
        lector = PdfReader(str(ruta))
        n_paginas = len(lector.pages)
        texto = "\n".join((p.extract_text() or "") for p in lector.pages)
    except Exception as exc:  # noqa: BLE001 - cualquier fallo de pypdf invalida el PDF
        inf.error(ruta.name, f"pypdf no puede abrirlo: {exc}")
        return
    if n_paginas < 2:
        inf.error(ruta.name, f"tiene {n_paginas} pagina(s); la spec pide dos secciones (Arquitectura y ADRs)")
    txt = plano(texto)
    for seccion, patron in (("Arquitectura", r"arquitectura"), ("ADRs / trade-offs", r"\badr")):
        if not re.search(patron, txt):
            inf.error(ruta.name, f"no encuentro la seccion '{seccion}' en el texto del PDF")
    adrs = sorted({int(n) for n in re.findall(r"\badr[\s\-_]*(\d+)", txt)})
    if adrs:
        inf.dato(f"{ruta.name}: {n_paginas} paginas, secciones OK, {len(adrs)} ADRs detectados {adrs}")
    else:
        inf.aviso(ruta.name, "no detecto ADRs numerados ('ADR 1', 'ADR-2'...): la spec pide entre dos y cinco decisiones")
        inf.dato(f"{ruta.name}: {n_paginas} paginas, secciones OK, 0 ADRs numerados detectados")


def revisa_publicable(raiz: Path, inf: Informe) -> None:
    """La spec pide un repo separado sin solucion, sin credenciales y sin aplicacion ejecutable."""
    extras = sorted(p.name for p in raiz.iterdir() if p.name not in IGNORAR_RECURSIVO and p.name != CARPETA)
    if extras:
        inf.error("raiz", "la raiz debe contener exactamente "
                  f"{CARPETA}/ y sobra: {', '.join(extras)} (no se sube la solucion ni una app ejecutable)")
    else:
        inf.dato(f"raiz: solo {CARPETA}/ (mas .git) -- repositorio publicable en cuanto a estructura")
    for patron in CODIGO:
        for p in raiz.rglob(patron):
            if IGNORAR_RECURSIVO & set(p.parts):
                continue
            inf.error("solucion", f"codigo/aplicacion ejecutable dentro de la entrega: {p.relative_to(raiz)}")
    for patron in CREDENCIALES:
        for p in raiz.rglob(patron):
            if IGNORAR_RECURSIVO & set(p.parts):
                continue
            rel = p.relative_to(raiz)
            if p.name in PLANTILLAS_ENV:
                inf.aviso("credenciales", f"plantilla de variables de entorno en la entrega: {rel} "
                                          "(sin secretos, pero la spec pide no subir nada de la app)")
            else:
                inf.error("credenciales", f"posible credencial en la entrega: {rel}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Valida el arbol de entrega del track Maisa.")
    ap.add_argument("raiz", type=Path, help="directorio raiz del repositorio de entrega")
    ap.add_argument("--publicable", action="store_true",
                    help="modo estricto: exige ademas que la raiz no tenga nada mas que la-caja-outcomes/ "
                         "y que no se suba solucion, codigo ejecutable ni credenciales")
    ap.add_argument("--corpus", type=Path, default=_CORPUS / "facturas",
                    help="directorio con los 500 PDFs de La Caja")
    ap.add_argument("--corpus-lote2", type=Path, default=None,
                    help="directorio con los PDFs del lote 2 (si no existe, la cobertura no se contrasta)")
    args = ap.parse_args(argv)

    raiz: Path = args.raiz.resolve()
    inf = Informe()
    print(f"== valida_entrega :: {raiz}{' (modo --publicable)' if args.publicable else ''}")
    if not raiz.is_dir():
        print(f"[{BLOQUEANTE}] raiz: no es un directorio: {raiz}")
        return 1

    if args.publicable:
        revisa_publicable(raiz, inf)

    carpeta = revisa_estructura(raiz, inf, args.publicable)
    if carpeta is None:
        return _cierra(inf)

    pdfs = corpus_de(args.corpus)
    if not pdfs:
        inf.aviso("corpus", f"sin PDFs en {args.corpus}: no puedo contrastar la cobertura de outcomes.jsonl")
    lote2 = corpus_de(args.corpus_lote2) if args.corpus_lote2 else corpus_de(corpus_lote2_por_defecto())

    for nombre in FICHEROS:
        ruta = carpeta / nombre
        if not ruta.exists():
            if nombre == LOTE2:
                inf.error(nombre, "NO EXISTE: es obligatorio para la entrega (lo envia Alberto el sabado); "
                                  "el validador sigue con el resto")
            else:
                inf.error(nombre, "no existe")
            continue
        if nombre.endswith(".jsonl"):
            if nombre == LOTE2:
                revisa_jsonl(ruta, lote2, bool(lote2), inf)
            else:
                revisa_jsonl(ruta, pdfs, bool(pdfs), inf)
        else:
            revisa_plan(ruta, inf)

    return _cierra(inf)


def _cierra(inf: Informe) -> int:
    print("\n-- datos")
    for d in inf.datos:
        print(f"   · {d}")
    if inf.notas:
        print("\n-- notas")
        for n in inf.notas:
            print(f"   · {n}")
    print("\n-- problemas")
    if not inf.problemas:
        print("   (ninguno)")
    for p in inf.problemas:
        print(f"   {p}")
    errores = len(inf.errores)
    avisos = len(inf.problemas) - errores
    if errores:
        print(f"\nRESULTADO: NO PUBLICABLE -- {errores} problema(s) bloqueante(s), {avisos} aviso(s)")
        return 1
    print(f"\nRESULTADO: OK -- 0 bloqueantes, {avisos} aviso(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
