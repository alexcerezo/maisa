#!/usr/bin/env python3
"""Evidencia **ejecutada** de resiliencia y recuperacion (rubrica, 10 puntos).

No afirma: ejecuta y mide. Cada bloque hace algo que puede salir mal y reporta
lo que de verdad ha pasado, incluidos los casos en los que el motor no se
comporta como el titular del pitch decia.

Bloques:

* **0. Lote de referencia** -- un lote completo con la cache de OCR caliente,
  contando cuantas veces se llama al servicio de vision (deberian ser 0).
* **A. Muerte a mitad del lote** -- ``SIGKILL`` al pipeline cuando la traza ya
  tiene eventos: la traza queda truncada, pero *es un prefijo valido* (nada de
  ``cadena_rota``) y ``trace.Registro(ruta, continuar=True)`` la reanuda sin
  romper la cadena. Se comprueba tambien el camino de error: continuar sobre
  una traza ya rota lanza ``TrazaError``.
* **B. Manipulacion detectada** -- se cambia un solo campo de un solo evento y
  ``trace.verifica`` dice la linea exacta. Tambien el ``hash_prev`` de la
  ultima linea.
* **C. Sello** -- ``trace.sello`` como ancla publicada: cambia si cambia la
  cabeza, y ``verifica(..., sello=...)`` denuncia ``sello_distinto`` incluso
  ante una reescritura **coherente** que recalcula toda la cadena.
* **D. ERP caido** -- el motor arranca del snapshot en disco sin ERP vivo, y
  con una URL inalcanzable falla de forma **declarada** (``erp.ErrorERP``),
  sin fichero de salida a medias.
* **E0. OCR inalcanzable** -- que hace el escalon de vision cuando el servicio
  no responde, y como la cache es el plan B.
* **E. OCR caido / cache** -- la cache ``sha256`` evita repetir vision: se mide
  lote caliente contra lote frio (moviendo la cache temporalmente y
  restaurandola byte a byte).
* **F. Un PDF del lote esta corrupto** -- lote de 3 (1 bueno + 1 truncado + 1
  vacio): hoy el proceso es fail-stop (exit != 0, ningun ``outcomes.jsonl`` a
  medias) y el lote se recupera apartando el fichero roto.
* **G. Cache de OCR corrupta o manipulada** -- JSON truncado y ``sha256`` que
  no cuadra se rehacen solos; cambiar el texto dejando el ``sha256`` del PDF
  intacto **no** se detecta (limite declarado, con el arreglo propuesto).
* **H. Bridge ERP vivo** (``--erp-vivo``) -- telemetria real contra el bridge
  de Alberto: ``ORA-00600`` cada 10 consultas, ``ERP-429`` con 8 clientes en
  paralelo, caducidad del token a los 300 usos y contraste del lote completo
  entre ERP vivo y snapshot.

Uso:
    cd maisa && PYTHONPATH=src ../.venv/bin/python tools/evidencia_resiliencia.py
    cd maisa && PYTHONPATH=src ../.venv/bin/python tools/evidencia_resiliencia.py --rapido
    cd maisa && PYTHONPATH=src ../.venv/bin/python tools/evidencia_resiliencia.py --erp-vivo

``--rapido`` acorta el lote frio (que es el caro: ~4 min con los 500 PDF) a
los primeros N PDF; las cifras del informe dicen siempre sobre cuantos PDF se
midio. Todo lo temporal vive en un directorio bajo ``maisa/`` y se borra al
final; la cache de OCR se restaura siempre (incluso si algo falla).

Exit 0 solo si **todas** las comprobaciones pasan.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ / "src") not in sys.path:
    sys.path.insert(0, str(RAIZ / "src"))

from maisa import erp, lectura, procesa, trace  # noqa: E402

PDFS = procesa.FACTURAS_POR_DEFECTO
XLSX = procesa.XLSX_POR_DEFECTO
CONFIG = procesa.CONFIG_POR_DEFECTO
SNAPSHOT = procesa.SNAPSHOT_POR_DEFECTO
BRIDGE = procesa.FACTURAS_POR_DEFECTO.parent / "alberto_erp.py"
ERP_VIVO = "http://127.0.0.1:8009"
ERP_MUERTO = "http://127.0.0.1:1/"

# Matar el lote cuando la traza tenga estos eventos (el pipeline escribe 2 por
# factura: `lectura` + `decision`). 60 deja claro que esta a mitad: ni el
# primer evento ni el ultimo.
EVENTOS_ANTES_DE_MATAR = 60
INTENTOS_DE_MUERTE = 3

# Bloque H (bridge ERP vivo): el bridge caduca a los 300 usos, asi que se lanza
# alguna consulta mas para ver donde salta el SES-401. 8 clientes a la vez pasan
# del limite de 10 req/s del bridge y fuerzan los ERP-429.
USOS_HASTA_CADUCIDAD = 320
CLIENTES_429 = 8
CONSULTAS_POR_CLIENTE_429 = 12


# --------------------------------------------------------------------------- #
# Informe
# --------------------------------------------------------------------------- #


class Informe:
    """Acumula lineas y comprobaciones; decide el codigo de salida."""

    def __init__(self) -> None:
        self._fallos: list[str] = []
        self._comprobaciones = 0

    def titulo(self, texto: str) -> None:
        print(f"\n{'=' * 78}\n{texto}\n{'=' * 78}", flush=True)

    def sub(self, texto: str) -> None:
        print(f"\n-- {texto}", flush=True)

    def dato(self, etiqueta: str, valor: object) -> None:
        print(f"   {etiqueta:<34} {valor}", flush=True)

    def nota(self, texto: str) -> None:
        for i, linea in enumerate(texto.splitlines()):
            print(f"   {'*' if i == 0 else ' '} {linea}", flush=True)

    def comprueba(self, nombre: str, condicion: bool, detalle: str = "") -> bool:
        self._comprobaciones += 1
        marca = "OK   " if condicion else "FALLO"
        print(f"   [{marca}] {nombre}{(' -- ' + detalle) if detalle else ''}", flush=True)
        if not condicion:
            self._fallos.append(nombre)
        return bool(condicion)

    def cierra(self) -> int:
        print(f"\n{'=' * 78}", flush=True)
        if self._fallos:
            print(f"RESULTADO: {len(self._fallos)}/{self._comprobaciones} FALLOS", flush=True)
            for fallo in self._fallos:
                print(f"   - {fallo}", flush=True)
            return 1
        print(f"RESULTADO: {self._comprobaciones}/{self._comprobaciones} comprobaciones OK",
              flush=True)
        return 0


INFO = Informe()


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #


def sha256_fich(ruta: Path) -> str:
    return hashlib.sha256(Path(ruta).read_bytes()).hexdigest()


def manifiesto(directorio: Path) -> dict[str, str]:
    """``{nombre: sha256}`` de todos los JSON de un directorio (orden estable)."""
    return {f.name: sha256_fich(f) for f in sorted(Path(directorio).glob("*.json"))}


def lineas(ruta: Path) -> list[str]:
    if not Path(ruta).exists():
        return []
    return Path(ruta).read_text(encoding="utf-8").splitlines()


def lineas_por_tipo(eventos: list[trace.Evento]) -> dict[str, int]:
    cuenta: dict[str, int] = {}
    for ev in eventos:
        cuenta[ev.tipo] = cuenta.get(ev.tipo, 0) + 1
    return dict(sorted(cuenta.items()))


def resumen_problemas(problemas: list[trace.Problema]) -> str:
    if not problemas:
        return "ninguno"
    return "; ".join(str(p) for p in problemas)


def clases(problemas: list[trace.Problema]) -> set[str]:
    return {p.clase for p in problemas}


def reescribe(ruta: Path, indice: int, cambia) -> Path:
    """Copia un log y aplica ``cambia(dict)`` a la linea ``indice`` (0-based)."""
    original = lineas(ruta)
    obj = json.loads(original[indice])
    cambia(obj)
    original[indice] = trace.canoniza(obj)
    ruta.write_text("\n".join(original) + "\n", encoding="utf-8")
    return ruta


def entorno_hijo() -> dict[str, str]:
    """PYTHONPATH del subproceso: ``src`` + lo que ya traia el proceso padre.

    Antes se *sustituia* ``PYTHONPATH``, y eso dejaba al hijo sin las
    dependencias de terceros cuando ``sys.executable`` no era el interprete del
    entorno virtual: el hijo moria con ``ModuleNotFoundError: openpyxl`` y el
    bloque de muerte se quedaba sin traza que analizar. Se conserva ademas el
    ``site-packages`` del interprete actual por si el padre se invoco sin
    ``PYTHONPATH`` (venv activado).
    """
    partes = [str(RAIZ / "src")]
    for origen in (sysconfig.get_paths().get("purelib"), os.environ.get("PYTHONPATH")):
        for trozo in str(origen or "").split(os.pathsep):
            if trozo and trozo not in partes:
                partes.append(trozo)
    return dict(os.environ, PYTHONPATH=os.pathsep.join(partes))


def lanza_pipeline(salida: Path, extra: list[str] | None = None,
                   stderr: Path | None = None) -> subprocess.Popen:
    orden = [
        sys.executable, "-m", "maisa.procesa",
        "--salida", str(salida), "--trabajadores", "4",
    ] + (extra or [])
    err = stderr.open("wb") if stderr else subprocess.DEVNULL
    return subprocess.Popen(
        orden, cwd=str(RAIZ), env=entorno_hijo(), stdout=subprocess.DEVNULL, stderr=err,
    )


def ejecuta_pipeline(salida: Path, extra: list[str] | None = None,
                     timeout: float = 900) -> tuple[int, str]:
    """Pipeline como subproceso; devuelve (codigo, ultimas lineas de stderr)."""
    orden = [
        sys.executable, "-m", "maisa.procesa",
        "--salida", str(salida), "--trabajadores", "4",
    ] + (extra or [])
    hecho = subprocess.run(
        orden, cwd=str(RAIZ), env=entorno_hijo(), timeout=timeout,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    return hecho.returncode, (hecho.stdout or "").strip()


def corre_en_proceso(salida: Path, snapshot: Path | None = SNAPSHOT,
                     erp_url: str | None = None, facturas: Path = PDFS,
                     trabajadores: int = 4) -> tuple[float, str]:
    """Ejecuta ``procesa.procesa`` en este proceso y devuelve (segundos, salida)."""
    buf = io.StringIO()
    arranque = time.monotonic()
    with contextlib.redirect_stdout(buf):
        procesa.procesa(
            facturas, XLSX, CONFIG, snapshot, erp_url, salida, trabajadores, 1,
            traza_hash=True,
        )
    return time.monotonic() - arranque, buf.getvalue()


def espera_traza(ruta: Path, minimo: int, limite: float,
                 proceso: subprocess.Popen | None = None) -> int:
    """Cuenta lineas hasta llegar a ``minimo`` o agotar ``limite`` segundos.

    Si se pasa ``proceso``, se deja de esperar en cuanto el hijo termina: sin
    eso, un hijo que muere al arrancar (por ejemplo, sin sus dependencias)
    bloquea el bloque entero durante ``limite`` segundos para nada.
    """
    arranque = time.monotonic()
    vistos = 0
    while time.monotonic() - arranque < limite:
        if ruta.exists():
            vistos = ruta.read_bytes().count(b"\n")
            if vistos >= minimo:
                return vistos
        if proceso is not None and proceso.poll() is not None:
            break
        time.sleep(0.0005)
    return vistos


# --------------------------------------------------------------------------- #
# Bloque 0: lote de referencia (cache caliente)
# --------------------------------------------------------------------------- #


def bloque_referencia(tmp: Path) -> tuple[Path, float, dict, str]:
    """Lote completo con la cache de OCR en su sitio. Devuelve la traza buena."""
    INFO.titulo("0. LOTE DE REFERENCIA (cache de OCR caliente)")
    cache = lectura.CACHE_OCR
    contador = {"n": 0, "t": 0.0}
    real = lectura.ocr_contenedor

    def espia(ruta: Path, timeout: int = 300) -> str:
        contador["n"] += 1
        t0 = time.monotonic()
        try:
            return real(ruta, timeout)
        finally:
            contador["t"] += time.monotonic() - t0

    lectura.ocr_contenedor = espia
    try:
        salida = tmp / "referencia" / "outcomes.jsonl"
        segundos, texto = corre_en_proceso(salida)
    finally:
        lectura.ocr_contenedor = real

    traza = salida.with_name(salida.stem + "_traza.jsonl")
    eventos = trace.carga(traza)
    INFO.dato("PDF en el lote", len(sorted(PDFS.glob("*.pdf"))))
    INFO.dato("eventos en la traza", len(eventos))
    INFO.dato("tipos de evento", lineas_por_tipo(eventos))
    INFO.dato("segundos (lote caliente)", f"{segundos:.2f}")
    INFO.dato("llamadas al servicio OCR", contador["n"])
    INFO.dato("segundos dentro de OCR", f"{contador['t']:.2f}")
    INFO.dato("sello de la traza de referencia", trace.sello(traza))
    for linea in texto.splitlines():
        if linea.startswith(("resultado", "lectura", "validacion", "traza hash")):
            INFO.nota(linea.strip())
    INFO.comprueba("la traza de referencia esta integra", not trace.verifica(traza),
                   resumen_problemas(trace.verifica(traza)))
    INFO.comprueba("cache caliente -> 0 llamadas a OCR", contador["n"] == 0,
                   f"{contador['n']} llamadas")
    INFO.comprueba("la cache existe", cache.exists() and len(manifiesto(cache)) > 0,
                   f"{len(manifiesto(cache))} entradas en {cache}")
    return traza, segundos, contador, texto


# --------------------------------------------------------------------------- #
# Bloque A: muerte a mitad del lote y reanudacion
# --------------------------------------------------------------------------- #


def bloque_muerte(tmp: Path) -> Path:
    INFO.titulo("A. MUERTE A MITAD DEL LOTE (SIGKILL) Y REANUDACION")
    INFO.nota(
        "El pipeline escribe y hace flush linea a linea, asi que una muerte\n"
        "brusca deja un prefijo legible. Lo que hay que demostrar es que ese\n"
        "prefijo NO acusa `cadena_rota` (lo que se escribio, se escribio bien)\n"
        "y que se puede continuar encima sin reescribir nada."
    )

    candidata: Path | None = None
    for intento in range(1, INTENTOS_DE_MUERTE + 1):
        INFO.sub(f"intento {intento}/{INTENTOS_DE_MUERTE}: SIGKILL a mitad del lote")
        carpeta = tmp / f"muerte{intento}"
        salida = carpeta / "outcomes.jsonl"
        traza = salida.with_name(salida.stem + "_traza.jsonl")
        err = carpeta / "stderr.txt"
        carpeta.mkdir(parents=True, exist_ok=True)
        proceso = lanza_pipeline(salida, ["--traza-hash"], stderr=err)
        t0 = time.monotonic()
        vistos = espera_traza(traza, EVENTOS_ANTES_DE_MATAR, limite=60.0, proceso=proceso)
        os.kill(proceso.pid, signal.SIGKILL)
        proceso.wait()
        tardanza = time.monotonic() - t0

        if not traza.exists():
            # El hijo no llego a escribir la traza: sin esto, el fallo salia
            # como un FileNotFoundError crudo y sin la causa (el stderr del
            # hijo), que es justo lo unico que explica el problema.
            detalle = err.read_text(encoding="utf-8", errors="replace").strip()
            raise SystemExit(
                f"el pipeline no escribio la traza en {traza} tras {tardanza:.1f} s "
                f"(codigo de salida {proceso.returncode}).\n"
                f"stderr del hijo:\n{detalle or '(vacio)'}"
            )

        eventos = trace.carga(traza)
        problemas = trace.verifica(traza)
        texto = traza.read_text(encoding="utf-8")
        INFO.dato("eventos vistos al matar", vistos)
        INFO.dato("codigo de salida del proceso", proceso.returncode)
        INFO.dato("segundos hasta la muerte", f"{tardanza:.2f}")
        INFO.dato("lineas escritas", len(texto.splitlines()))
        INFO.dato("acaba en salto de linea", texto.endswith("\n"))
        INFO.dato("tipos de evento escritos", lineas_por_tipo(eventos))
        INFO.dato("problemas de verifica", resumen_problemas(problemas))
        INFO.dato("salida parcial de outcomes.jsonl",
                  "existe" if salida.exists() else "no existe")
        INFO.comprueba(f"intento {intento}: el proceso murio por SIGKILL",
                       proceso.returncode == -signal.SIGKILL)
        INFO.comprueba(f"intento {intento}: la traza quedo a medias (sin evento `fin`)",
                       not any(ev.tipo == trace.TIPO_FIN for ev in eventos),
                       f"{len(eventos)} eventos")
        INFO.comprueba(f"intento {intento}: el prefijo no acusa cadena_rota",
                       "cadena_rota" not in clases(problemas))
        if candidata is None and not problemas:
            candidata = traza

    if candidata is None:
        candidata = tmp / "muerte1" / "outcomes_traza.jsonl"
        INFO.nota("Ningun intento dejo un prefijo sin problemas; se reanuda sobre el ultimo.")
    INFO.dato("prefijo elegido para reanudar", candidata)

    # --- reanudacion -------------------------------------------------------
    INFO.sub("reanudacion: Registro(ruta, continuar=True)")
    sello_previo = trace.sello(candidata)
    antes = lineas(candidata)
    eventos_antes = trace.carga(candidata)
    INFO.dato("prefijo de partida", f"{len(antes)} lineas, sello {sello_previo[:16]}...")

    registro = trace.Registro(candidata, continuar=True)
    INFO.dato("eventos cargados al reanudar", len(registro))
    nuevo = registro.anota(trace.TIPO_LECTURA, "REANUDADO.pdf", sha256="cd" * 32,
                           escalon="capa_texto", calidad=1.0, reanudado=True)
    INFO.dato("primer seq nuevo", nuevo.seq)
    INFO.dato("hash_prev del primer evento nuevo", f"{nuevo.hash_prev[:16]}...")
    registro.anota(trace.TIPO_DECISION, "REANUDADO.pdf", result="PAGAR", motivos=[],
                   reanudado=True)
    registro.anota(trace.TIPO_FIN, resultados={"PAGAR": 1}, reanudado=True)
    registro.cierra()

    despues = lineas(candidata)
    problemas_final = trace.verifica(candidata)
    INFO.dato("lineas tras reanudar", len(despues))
    INFO.dato("problemas tras reanudar", resumen_problemas(problemas_final))
    INFO.dato("sello final", trace.sello(candidata))
    INFO.comprueba("al reanudar se cargan exactamente los eventos del prefijo",
                   len(registro) == len(eventos_antes) + 3,
                   f"{len(registro)} == {len(eventos_antes)} + 3")
    INFO.comprueba("el primer evento nuevo encadena con la cabeza anterior",
                   nuevo.hash_prev == sello_previo)
    INFO.comprueba("el seq continua sin hueco",
                   nuevo.seq == eventos_antes[-1].seq + 1,
                   f"{nuevo.seq} tras {eventos_antes[-1].seq}")
    INFO.comprueba("el prefijo NO se ha reescrito (byte a byte)",
                   despues[:len(antes)] == antes)
    INFO.comprueba("la traza reanudada esta integra", not problemas_final,
                   resumen_problemas(problemas_final))

    # --- camino de error: continuar sobre una traza ya rota ----------------
    INFO.sub("camino de error: continuar sobre una traza ya rota")
    rota = tmp / "muerte_rota.jsonl"
    original = lineas(candidata)
    rota.write_text("\n".join(original) + "\n", encoding="utf-8")
    indice = next(
        i for i, l in enumerate(original)
        if json.loads(l)["tipo"] == trace.TIPO_DECISION
        and json.loads(l)["datos"].get("result") == "PAGAR"
    )
    reescribe(rota, indice, lambda o: o["datos"].__setitem__("result", "NO_PAGAR"))
    problemas_rota = trace.verifica(rota)
    INFO.dato("linea manipulada (1-based)", indice + 1)
    INFO.dato("problemas de la traza rota", resumen_problemas(problemas_rota))
    INFO.dato("lineas que senala", [p.linea for p in problemas_rota])
    lanzada = None
    try:
        trace.Registro(rota, continuar=True)
    except trace.TrazaError as exc:
        lanzada = str(exc)
    INFO.dato("excepcion", (lanzada or "NINGUNA")[:200])
    INFO.comprueba("continuar sobre una traza rota lanza TrazaError", lanzada is not None)
    INFO.comprueba("el error dice cuantos problemas y el primero",
                   lanzada is not None and "ya esta rota" in lanzada and "problemas" in lanzada)
    return candidata


# --------------------------------------------------------------------------- #
# Bloque B: manipulacion detectada
# --------------------------------------------------------------------------- #


def bloque_manipulacion(tmp: Path, buena: Path) -> None:
    INFO.titulo("B. MANIPULACION DETECTADA")
    original = lineas(buena)
    indice = next(
        i for i, l in enumerate(original)
        if json.loads(l)["tipo"] == trace.TIPO_DECISION
        and json.loads(l)["datos"].get("result") == "PAGAR"
    )
    file_id = json.loads(original[indice])["file_id"]
    INFO.dato("linea manipulada (1-based)", indice + 1)
    INFO.dato("factura", file_id)
    INFO.dato("cambio", "datos.result: PAGAR -> NO_PAGAR")

    manipulada = tmp / "manipulada.jsonl"
    manipulada.write_text("\n".join(original) + "\n", encoding="utf-8")
    reescribe(manipulada, indice,
              lambda o: o["datos"].__setitem__("result", "NO_PAGAR"))
    problemas = trace.verifica(manipulada)
    INFO.dato("problemas", resumen_problemas(problemas))
    INFO.dato("lineas senaladas", [p.linea for p in problemas])
    INFO.comprueba("se detecta la manipulacion", len(problemas) == 1,
                   resumen_problemas(problemas))
    INFO.comprueba("el problema es hash_roto", clases(problemas) == {"hash_roto"},
                   str(clases(problemas)))
    INFO.comprueba("el mensaje senala la linea exacta",
                   [p.linea for p in problemas] == [indice + 1],
                   f"{[p.linea for p in problemas]} vs {[indice + 1]}")
    INFO.comprueba("el mensaje nombra el hash declarado y el recalculado",
                   "no cuadra con el contenido" in str(problemas[0]))

    decisiones = {ev.file_id: ev for ev in trace.carga(manipulada)
                  if ev.tipo == trace.TIPO_DECISION}
    INFO.dato("resultado que ahora lee el log", decisiones[file_id].datos.get("result"))
    INFO.comprueba("la manipulacion es semantica (cambia la decision leida)",
                   decisiones[file_id].datos.get("result") == "NO_PAGAR")

    INFO.sub("alterar el hash_prev de la ULTIMA linea")
    ultima = tmp / "ultima_prev.jsonl"
    ultima.write_text("\n".join(original) + "\n", encoding="utf-8")
    reescribe(ultima, len(original) - 1,
              lambda o: o.__setitem__("hash_prev", "f" * 64))
    problemas_ultima = trace.verifica(ultima)
    INFO.dato("problemas", resumen_problemas(problemas_ultima))
    INFO.dato("lineas senaladas", [p.linea for p in problemas_ultima])
    INFO.comprueba("el hash_prev de la ultima linea se detecta como cadena_rota",
                   "cadena_rota" in clases(problemas_ultima))
    INFO.comprueba("y ademas como hash_roto (el hash cubre hash_prev)",
                   "hash_roto" in clases(problemas_ultima))

    INFO.sub("nota: por que una linea del medio no da cadena_rota")
    INFO.nota(
        "Al tocar `datos` de la linea 3 el `hash_prev` de la linea 4 sigue\n"
        "apuntando al hash *declarado* de la 3, que no ha cambiado: la cadena\n"
        "esta 'intacta' y quien delata es el hash propio. Las dos comprobaciones\n"
        "son independientes a proposito."
    )
    INFO.comprueba("no hay cadena_rota en el caso de la linea del medio",
                   "cadena_rota" not in clases(problemas), str(clases(problemas)))


# --------------------------------------------------------------------------- #
# Bloque C: sello
# --------------------------------------------------------------------------- #


def bloque_sello(tmp: Path, buena: Path) -> None:
    INFO.titulo("C. SELLO: ANCLA PUBLICADA DE TODA LA TRAZA")
    sello_bueno = trace.sello(buena)
    original = lineas(buena)
    INFO.dato("sello de la traza buena", sello_bueno)

    INFO.sub("c1) un byte distinto en la cabeza -> el sello cambia")
    un_byte = tmp / "sello_un_byte.jsonl"
    un_byte.write_text("\n".join(original) + "\n", encoding="utf-8")
    reescribe(un_byte, len(original) - 1,
              lambda o: o.__setitem__("hash", "f" + o["hash"][1:]))
    sello_un_byte = trace.sello(un_byte)
    problemas = trace.verifica(un_byte, sello=sello_bueno)
    INFO.dato("sello tras cambiar 1 byte", sello_un_byte)
    INFO.dato("problemas con el sello esperado", resumen_problemas(problemas))
    INFO.comprueba("el sello cambia con un byte", sello_un_byte != sello_bueno)
    INFO.comprueba("verifica(sello=...) reporta sello_distinto",
                   "sello_distinto" in clases(problemas), str(clases(problemas)))

    INFO.sub("c2) un evento anadido de forma legitima -> el sello cambia")
    anadida = tmp / "sello_anadida.jsonl"
    anadida.write_text("\n".join(original) + "\n", encoding="utf-8")
    with trace.Registro(anadida, continuar=True) as reg:
        reg.anota(trace.TIPO_FIN, resultados={"PAGAR": 448}, nota="evento anadido despues")
    problemas_anadida = trace.verifica(anadida, sello=sello_bueno)
    INFO.dato("problemas con el sello esperado", resumen_problemas(problemas_anadida))
    INFO.comprueba("la traza anadida es valida pero el sello ya no cuadra",
                   trace.verifica(anadida) == [] and "sello_distinto" in clases(problemas_anadida))

    INFO.sub("c3) reescritura COHERENTE: se recalcula toda la cadena")
    eventos = trace.carga(buena)
    nuevos: list[trace.Evento] = []
    previo = trace.GENESIS
    for i, ev in enumerate(eventos):
        datos = dict(ev.datos)
        if i == 2:
            datos["result"] = "NO_PAGAR"  # mismo cambio de fondo que en B
        nuevo_ev = trace.Evento(seq=ev.seq, ts=ev.ts, tipo=ev.tipo, file_id=ev.file_id,
                                datos=datos, hash_prev=previo)
        previo = nuevo_ev.hash
        nuevos.append(nuevo_ev)
    coherente = tmp / "reescritura_coherente.jsonl"
    coherente.write_text("\n".join(ev.canonico() for ev in nuevos) + "\n", encoding="utf-8")
    problemas_coherente = trace.verifica(coherente)
    problemas_sello = trace.verifica(coherente, sello=sello_bueno)
    INFO.dato("verifica SIN sello", resumen_problemas(problemas_coherente))
    INFO.dato("sello de la reescritura", trace.sello(coherente))
    INFO.dato("verifica CON sello esperado", resumen_problemas(problemas_sello))
    INFO.comprueba("la reescritura coherente pasa verifica sin sello",
                   not problemas_coherente, resumen_problemas(problemas_coherente))
    INFO.comprueba("el sello la delata igualmente",
                   "sello_distinto" in clases(problemas_sello))
    INFO.comprueba("el sello de la reescritura es distinto del publicado",
                   trace.sello(coherente) != sello_bueno)

    INFO.sub("c4) limite honesto del sello")
    del_medio = tmp / "sello_del_medio.jsonl"
    del_medio.write_text("\n".join(original) + "\n", encoding="utf-8")
    reescribe(del_medio, 3, lambda o: o["datos"].__setitem__("result", "NO_PAGAR"))
    INFO.dato("sello tras tocar una linea del medio", trace.sello(del_medio))
    INFO.dato("problemas de verifica (sin sello)", resumen_problemas(trace.verifica(del_medio)))
    INFO.comprueba("el sello NO cambia al tocar una linea del medio",
                   trace.sello(del_medio) == sello_bueno)
    INFO.nota(
        "El sello es un ancla de la cabeza, no un checksum del fichero: ancla\n"
        "la traza entera *si* se publica fuera del log. Para el contenido linea\n"
        "a linea lo que vale es verifica(), que aqui si detecta el hash_roto."
    )


# --------------------------------------------------------------------------- #
# Bloque D: ERP caido
# --------------------------------------------------------------------------- #


def bloque_erp(tmp: Path) -> None:
    INFO.titulo("D. ERP CAIDO: SNAPSHOT EN DISCO Y FALLO DECLARADO")

    INFO.sub("d1) hay ERP vivo?")
    vivo = False
    detalle = ""
    try:
        with urllib.request.urlopen(ERP_VIVO, timeout=2):
            vivo = True
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        detalle = str(getattr(exc, "reason", exc))
    INFO.dato("sonda a " + ERP_VIVO, "responde" if vivo else f"sin respuesta ({detalle})")
    INFO.dato("snapshot en disco", f"{SNAPSHOT} ({SNAPSHOT.stat().st_size} bytes)"
              if SNAPSHOT.exists() else "no existe")

    INFO.sub("d2) lote completo arrancando del snapshot, con el bridge caido")
    salida = tmp / "erp_snapshot" / "outcomes.jsonl"
    codigo, texto = ejecuta_pipeline(
        salida, ["--traza-hash", "--snapshot", str(SNAPSHOT), "--erp-url", ERP_VIVO],
    )
    INFO.dato("codigo de salida", codigo)
    for linea in texto.splitlines():
        if linea.startswith(("facturas", "resultado", "erp", "validacion", "traza hash")):
            INFO.nota(linea.strip())
    INFO.comprueba("el motor no necesita ERP vivo si hay snapshot", codigo == 0,
                   f"exit {codigo}")
    if not vivo:
        INFO.comprueba("y ademas funciona apuntando a un ERP que no responde",
                       codigo == 0, "el bridge no estaba levantado durante la prueba")
    else:
        INFO.nota(
            "El bridge SI estaba levantado, asi que esta corrida no demuestra el\n"
            "caso 'ERP caido'; lo que demuestra es que con snapshot basta para\n"
            "decidir. El contraste vivo contra snapshot lo hace el bloque H5."
        )
    INFO.comprueba("la salida se produce y valida", salida.exists()
                   and "validacion : OK" in texto)

    INFO.sub("d3) sin snapshot y con una URL inalcanzable: fallo declarado")
    salida_mala = tmp / "erp_caido" / "outcomes.jsonl"
    arranque = time.monotonic()
    codigo_malo, texto_malo = ejecuta_pipeline(
        salida_mala, ["--snapshot", str(tmp / "no_existe.json"), "--erp-url", ERP_MUERTO],
    )
    tardanza = time.monotonic() - arranque
    ultima = (texto_malo.splitlines() or [""])[-1]
    INFO.dato("codigo de salida", codigo_malo)
    INFO.dato("segundos hasta fallar", f"{tardanza:.2f}")
    INFO.dato("ultima linea de la salida", ultima)
    INFO.dato("fichero de salida", "existe" if salida_mala.exists() else "NO se creo")
    INFO.comprueba("el proceso falla con codigo distinto de 0", codigo_malo != 0,
                   f"exit {codigo_malo}")
    INFO.comprueba("la excepcion es declarada (erp.ErrorERP con su codigo legacy)",
                   "ErrorERP" in ultima and "SES-401" in ultima, ultima)
    INFO.comprueba("no se escribe un outcomes.jsonl vacio en silencio",
                   not salida_mala.exists())

    INFO.sub("d4) la misma excepcion, capturada en proceso")
    tipo: str | None = None
    try:
        procesa.construye_decisor(XLSX, CONFIG, tmp / "no_existe.json", ERP_MUERTO)
    except erp.ErrorERP as exc:
        tipo = type(exc).__name__
        INFO.dato("excepcion", f"{tipo}: {exc}")
        INFO.dato("codigo legacy", exc.codigo)
        INFO.dato("reintentable", exc.reintentable)
    INFO.comprueba("se puede capturar como erp.ErrorERP sin mirar el texto",
                   tipo == "ErrorERP", str(tipo))

    INFO.sub("d5) el snapshot es lo que se lee cuando el ERP no esta")
    asientos = erp.carga_snapshot(SNAPSHOT)
    INFO.dato("asientos del snapshot", len(asientos))
    INFO.dato("primero", asientos[0].como_dict() if asientos else "-")
    INFO.comprueba("el snapshot en disco tiene los 516 asientos", len(asientos) == 516,
                   str(len(asientos)))


# --------------------------------------------------------------------------- #
# Bloque E: cache de OCR
# --------------------------------------------------------------------------- #


def bloque_ocr_caido(tmp: Path) -> None:
    """E0: que hace el motor si el servicio de vision no responde."""
    INFO.titulo("E0. SERVICIO DE OCR INALCANZABLE")
    INFO.nota(
        "No se mata el contenedor de OCR (esta en uso): se apunta el modulo a un\n"
        "puerto muerto durante una sola lectura, con la cache desactivada, para\n"
        "ver el comportamiento del escalon de vision. Se restaura la URL despues."
    )
    claves = {f.stem for f in lectura.CACHE_OCR.glob("*.json")}
    con_ocr = [p for p in sorted(PDFS.glob("*.pdf")) if lectura.sha256_pdf(p) in claves]
    if not con_ocr:
        INFO.nota("no hay ningun PDF que exija OCR; se omite este bloque")
        return
    pdf = con_ocr[0]
    INFO.dato("PDF de prueba", pdf.name)

    real_url = lectura.OCR_URL
    lectura.OCR_URL = "http://127.0.0.1:1"
    tipo = "ninguna (devolvio un documento)"
    mensaje = ""
    try:
        doc = lectura.lee(pdf, usar_cache=False)
        mensaje = f"escalon={doc.escalon} calidad={doc.calidad:.2f}"
    except Exception as exc:  # noqa: BLE001 -- es justo lo que queremos reportar
        tipo = f"{type(exc).__module__}.{type(exc).__name__}"
        mensaje = str(exc).splitlines()[0][:110]
    finally:
        lectura.OCR_URL = real_url

    INFO.dato("con el OCR inalcanzable", tipo)
    INFO.dato("mensaje", mensaje)
    INFO.comprueba("el escalon de vision falla de forma ruidosa, no devuelve texto vacio",
                   "Error" in tipo, tipo)

    doc = lectura.lee(pdf)
    INFO.dato("el mismo PDF con la cache",
              f"escalon={doc.escalon} cache={doc.cache} {doc.segundos * 1000:.1f} ms")
    INFO.comprueba("con la cache presente el PDF se lee sin tocar el servicio",
                   doc.cache and doc.escalon == "cache_ocr", doc.escalon)


def bloque_pdf_roto(tmp: Path) -> None:
    """F: que pasa si un PDF del lote esta corrupto (truncado o vacio)."""
    INFO.titulo("F. UN PDF DEL LOTE ESTA CORRUPTO")
    INFO.nota(
        "Dos PDF rotos de verdad (uno truncado al 33%, uno de 0 bytes) en un lote\n"
        "de 3 junto a uno bueno. Se mide el comportamiento del motor, que hoy es\n"
        "fail-stop: no decide mal, pero tampoco salva el resto del lote."
    )
    carpeta = tmp / "pdf_roto"
    carpeta.mkdir(parents=True, exist_ok=True)
    buenos = sorted(PDFS.glob("*.pdf"))
    if not buenos:
        INFO.nota("no hay PDF en el lote; se omite este bloque")
        return
    bueno = buenos[0]
    shutil.copy(bueno, carpeta / bueno.name)
    original = bueno.read_bytes()
    truncado = carpeta / "rota_truncada.pdf"
    truncado.write_bytes(original[: len(original) // 3])
    vacio = carpeta / "rota_vacia.pdf"
    vacio.write_bytes(b"")
    INFO.dato("PDF bueno", f"{bueno.name} ({len(original)} bytes)")
    INFO.dato("PDF truncado", f"{truncado.name} ({truncado.stat().st_size} bytes)")
    INFO.dato("PDF vacio", f"{vacio.name} (0 bytes)")

    for ruta in (truncado, vacio):
        try:
            doc = lectura.lee(ruta)
            INFO.dato(f"lectura.lee({ruta.name})",
                      f"escalon={doc.escalon} calidad={doc.calidad:.2f}")
        except Exception as exc:  # noqa: BLE001 -- es justo lo que queremos reportar
            INFO.dato(f"lectura.lee({ruta.name})",
                      f"{type(exc).__module__}.{type(exc).__name__}: "
                      f"{str(exc).splitlines()[0][:70]}")

    salida = tmp / "pdf_roto" / "outcomes.jsonl"
    traza = tmp / "pdf_roto" / "outcomes_traza.jsonl"
    codigo, texto = ejecuta_pipeline(salida, ["--facturas", str(carpeta), "--traza-hash"])
    INFO.dato("lote de 3 (1 bueno + 2 rotos): exit", codigo)
    INFO.dato("ultima linea de la salida",
              (texto.strip().splitlines() or ["(vacio)"])[-1][:100])
    INFO.dato("outcomes.jsonl escrito", salida.exists())
    eventos = [json.loads(l) for l in lineas(traza)] if traza.exists() else []
    INFO.dato("eventos en la traza del lote fallido", len(eventos))
    INFO.dato("tipos de evento",
              dict(sorted({t: sum(1 for e in eventos if e["tipo"] == t)
                           for t in {e["tipo"] for e in eventos}}.items())))
    INFO.dato("problemas de verifica",
              resumen_problemas(trace.verifica(traza)) if traza.exists() else "no hay traza")
    INFO.comprueba("un PDF corrupto hace fallar el proceso (fail-stop)",
                   codigo != 0, f"exit {codigo}")
    INFO.comprueba("no se publica un outcomes.jsonl a medias", not salida.exists())
    INFO.comprueba("el fallo es ruidoso y dice el fichero",
                   "PdfStreamError" in texto or "EmptyFileError" in texto)
    INFO.comprueba("la traza del lote interrumpido queda integra y sin `fin`",
                   bool(eventos) and not any(e["tipo"] == "fin" for e in eventos)
                   and not trace.verifica(traza),
                   f"{len(eventos)} eventos")

    INFO.sub("recuperacion: apartar el fichero roto y reejecutar")
    for ruta in (truncado, vacio):
        ruta.unlink()
    salida2 = tmp / "pdf_roto" / "outcomes_sano.jsonl"
    codigo2, texto2 = ejecuta_pipeline(salida2, ["--facturas", str(carpeta)])
    emitidas = 0
    if salida2.exists():
        emitidas = len([l for l in salida2.read_text(encoding="utf-8").splitlines() if l.strip()])
    INFO.dato("mismo lote sin los rotos: exit", codigo2)
    INFO.dato("facturas emitidas", emitidas)
    INFO.comprueba("quitando el fichero roto el lote vuelve a salir", codigo2 == 0,
                   f"exit {codigo2}")
    INFO.comprueba("y emite la factura buena", emitidas == 1, f"{emitidas} facturas")
    INFO.nota(
        "Límite declarado: hoy no hay guarda por fichero en `lectura.lee_lote`, así\n"
        "que un PDF ilegible se lleva el lote entero por delante. Lo que sí garantiza\n"
        "el motor es que no se decide mal: exit != 0 y ni un outcomes.jsonl a medias."
    )


def bloque_cache_manipulada(tmp: Path) -> None:
    """G: que pasa si alguien toca la cache de OCR."""
    INFO.titulo("G. CACHE DE OCR CORRUPTA O MANIPULADA")
    INFO.nota(
        "La entrada de cache es {sha256 del PDF, texto}. Se prueban tres danos:\n"
        "JSON truncado, sha256 que no cuadra y texto cambiado con el sha intacto.\n"
        "La entrada se restaura byte a byte en cualquier caso."
    )
    cache = lectura.CACHE_OCR
    claves = {f.stem for f in cache.glob("*.json")}
    elegidos = [p for p in sorted(PDFS.glob("*.pdf")) if lectura.sha256_pdf(p) in claves]
    if not elegidos:
        INFO.nota("no hay ningun PDF en la cache; se omite este bloque")
        return
    pdf = elegidos[0]
    entrada = cache / f"{lectura.sha256_pdf(pdf)}.json"
    respaldo = entrada.read_bytes()
    INFO.dato("PDF en cache", pdf.name)
    INFO.dato("entrada", f"{entrada.name} ({len(respaldo)} bytes)")
    try:
        entrada.write_bytes(respaldo[: len(respaldo) // 2])
        doc = lectura.lee(pdf)
        INFO.dato("(a) JSON truncado",
                  f"escalon={doc.escalon} cache={doc.cache} {doc.segundos:.2f}s")
        INFO.comprueba("(a) un JSON roto se rehace solo (se relee por OCR)",
                       doc.escalon == "vision_ocr" and not doc.cache, doc.escalon)

        falso = json.loads(respaldo.decode("utf-8"))
        falso["sha256"] = "0" * 64
        entrada.write_text(json.dumps(falso, ensure_ascii=False), encoding="utf-8")
        doc = lectura.lee(pdf)
        INFO.dato("(b) sha256 que no cuadra",
                  f"escalon={doc.escalon} cache={doc.cache} {doc.segundos:.2f}s")
        INFO.comprueba("(b) una entrada de otro PDF se ignora y se rehace",
                       doc.escalon == "vision_ocr" and not doc.cache, doc.escalon)

        carpeta = tmp / "cache_manipulada"
        carpeta.mkdir(parents=True, exist_ok=True)
        shutil.copy(pdf, carpeta / pdf.name)
        salida = tmp / "cache_manipulada" / "legitima.jsonl"
        _, texto = corre_en_proceso(salida, facturas=carpeta)
        legitima = _primer_resultado(salida)
        INFO.dato("decision con la cache legitima", legitima)
        INFO.dato("motivos con la cache legitima", _motivos(salida))

        manipulado = json.loads(respaldo.decode("utf-8"))
        manipulado["texto"] = (
            "FACTURA PROVEEDOR DESCONOCIDO S.L.\nNIF: X0000000X\n"
            "IBAN: ES0000000000000000000000\nPEDIDO: PO-2026-9999\n"
            "FECHA: 01/01/2026\nBASE 1000,00 IVA 210,00 TOTAL 999999,00\n"
        )
        entrada.write_text(json.dumps(manipulado, ensure_ascii=False), encoding="utf-8")
        doc = lectura.lee(pdf)
        INFO.dato("(c) texto cambiado, sha intacto",
                  f"escalon={doc.escalon} cache={doc.cache} {doc.segundos:.2f}s")
        salida2 = tmp / "cache_manipulada" / "manipulada.jsonl"
        _, texto2 = corre_en_proceso(salida2, facturas=carpeta)
        INFO.dato("decision con la cache manipulada", _primer_resultado(salida2))
        motivos = _motivos(salida2)
        INFO.dato("motivos que publica", motivos)
        INFO.comprueba("(c) el sha256 no protege el CONTENIDO de la cache",
                       doc.cache and doc.escalon == "cache_ocr",
                       "se lee como cache_ocr, sin tocar el servicio")
        INFO.nota(
            "Límite declarado: la cache esta anclada al sha256 del *PDF*, no al del\n"
            "*texto*. Quien pueda escribir en .cache/ocr cambia lo que el motor lee y,\n"
            "con ello, el motivo que se publica. Arreglo propuesto: guardar tambien el\n"
            "sha256 del texto y rechazar la entrada si no cuadra (2 lineas)."
        )
    finally:
        entrada.write_bytes(respaldo)
        INFO.dato("entrada de cache restaurada", entrada.read_bytes() == respaldo)
        INFO.comprueba("la cache queda como estaba", entrada.read_bytes() == respaldo)


def bloque_erp_vivo(tmp: Path) -> None:
    """H: telemetria contra el bridge ERP vivo (opcional, ``--erp-vivo``)."""
    INFO.titulo("H. BRIDGE ERP VIVO: ORA-00600, 429 Y CADUCIDAD DE SESION")
    INFO.nota(
        f"Requiere el bridge de Alberto en {ERP_VIVO}. Mide lo que el motor aguanta\n"
        "con el ERP fallando a proposito cada 10 consultas, limitando a 10 req/s y\n"
        "caducando el token a los 900 s o 300 usos."
    )
    if not _responde(ERP_VIVO):
        INFO.dato("sonda al bridge", "sin respuesta; se omite este bloque")
        INFO.nota(f"levanta el bridge con: python3 {BRIDGE}")
        return

    cliente = erp.ERP(ERP_VIVO)
    arranque = time.monotonic()
    asientos = cliente.asientos()
    segundos = time.monotonic() - arranque
    INFO.dato("asientos descargados", len(asientos))
    INFO.dato("segundos", f"{segundos:.3f}")
    INFO.dato("telemetria", json.dumps(cliente.metricas.como_dict(), ensure_ascii=False))
    INFO.comprueba("el lote de asientos se descarga entero pese a los ORA-00600",
                   len(asientos) > 0, f"{len(asientos)} asientos")
    INFO.comprueba("los ORA-00600 se absorben sin error al llamante",
                   not cliente.metricas.errores and cliente.metricas.reintentos_ora > 0,
                   f"{cliente.metricas.reintentos_ora} reintentos")

    INFO.sub("H1. token caducado en el servidor -> relogin transparente")
    cliente.token = "token-caducado-a-proposito"
    antes = cliente.metricas.relogins
    arranque = time.monotonic()
    cliente.asientos()
    INFO.dato("relogins", cliente.metricas.relogins - antes)
    INFO.dato("segundos", f"{time.monotonic() - arranque:.3f}")
    INFO.dato("errores", cliente.metricas.errores)
    INFO.comprueba("un SES-401 se resuelve con un relogin automatico",
                   cliente.metricas.relogins - antes >= 1, f"{antes} -> {cliente.metricas.relogins}")

    INFO.sub("H2. caducidad por USOS (300 en el servidor), sin adelantarse")
    INFO.nota(
        "Se pone `usos_token = 0` antes de cada consulta para que la renovacion\n"
        "preventiva no tape el limite del servidor: quien caduca es el bridge."
    )
    cliente2 = erp.ERP(ERP_VIVO)
    cliente2.login()
    relogins_antes = cliente2.metricas.relogins
    primer_relogin = None
    fallos = 0
    for uso in range(1, USOS_HASTA_CADUCIDAD + 1):
        cliente2.usos_token = 0
        try:
            cliente2._consulta("/erp/asientos?pagina=1")
        except erp.ErrorERP as exc:
            fallos += 1
            INFO.dato("ErrorERP al llamante", f"uso {uso}: {exc}")
        if cliente2.metricas.relogins > relogins_antes and primer_relogin is None:
            primer_relogin = uso
    INFO.dato("consultas lanzadas", USOS_HASTA_CADUCIDAD)
    INFO.dato("relogins disparados", cliente2.metricas.relogins - relogins_antes)
    INFO.dato("primer relogin en la consulta", primer_relogin)
    INFO.dato("reintentos ORA-00600", cliente2.metricas.reintentos_ora)
    INFO.dato("esperas 429", cliente2.metricas.reintentos_429)
    INFO.dato("excepciones al llamante", fallos)
    INFO.comprueba("la caducidad por usos se resuelve sola",
                   cliente2.metricas.relogins - relogins_antes == 1 and fallos == 0,
                   f"{cliente2.metricas.relogins - relogins_antes} relogin, {fallos} fallos")

    INFO.sub("H3. renovacion preventiva contra enterarse por el servidor")
    preventivo = erp.ERP(ERP_VIVO)
    preventivo.login()
    preventivo.token_creado -= erp.TOKEN_VIGENCIA_SEGUNDOS + 100.0
    peticiones_antes = preventivo.metricas.peticiones
    preventivo._consulta("/erp/asientos?pagina=1")
    gasto_preventivo = preventivo.metricas.peticiones - peticiones_antes
    reactivo = erp.ERP(ERP_VIVO)
    reactivo.login()
    reactivo.token = "token-invalido"
    peticiones_antes = reactivo.metricas.peticiones
    reactivo._consulta("/erp/asientos?pagina=1")
    gasto_reactivo = reactivo.metricas.peticiones - peticiones_antes
    INFO.dato("peticiones (preventivo: login+consulta)", gasto_preventivo)
    INFO.dato("peticiones (reactivo: 401+login+consulta)", gasto_reactivo)
    INFO.dato("errores", preventivo.metricas.errores + reactivo.metricas.errores)
    INFO.comprueba("adelantarse cuesta menos peticiones que esperar al 401",
                   gasto_preventivo < gasto_reactivo,
                   f"{gasto_preventivo} < {gasto_reactivo}")

    INFO.sub("H4. 8 clientes en paralelo: mas de 10 req/s contra el bridge")
    clientes = [erp.ERP(ERP_VIVO) for _ in range(CLIENTES_429)]
    for c in clientes:
        c._intervalo = 0.0
        c.login()
    antes_429 = sum(c.metricas.reintentos_429 for c in clientes)
    escapados: list[str] = []
    arranque = time.monotonic()
    hilos = []
    import threading
    for c in clientes:
        hilos.append(threading.Thread(target=_martillea, args=(c, escapados)))
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    INFO.dato("segundos", f"{time.monotonic() - arranque:.3f}")
    INFO.dato("peticiones totales", sum(c.metricas.peticiones for c in clientes))
    INFO.dato("esperas 429", sum(c.metricas.reintentos_429 for c in clientes) - antes_429)
    INFO.dato("errores internos no recuperados", sum(len(c.metricas.errores) for c in clientes))
    INFO.dato("excepciones al llamante", len(escapados))
    INFO.comprueba("el cliente se frena solo cuando el bridge devuelve 429",
                   sum(c.metricas.reintentos_429 for c in clientes) - antes_429 > 0,
                   f"{sum(c.metricas.reintentos_429 for c in clientes) - antes_429} esperas")
    INFO.comprueba("ningun fallo silencioso: lo que no se recupera se declara",
                   all(e for e in escapados) and not any(c.metricas.errores for c in clientes),
                   f"{len(escapados)} excepciones declaradas")

    INFO.sub("H5. el mismo lote contra el ERP vivo y contra el snapshot")
    salida_vivo = tmp / "erp_vivo.jsonl"
    salida_snap = tmp / "erp_snapshot.jsonl"
    cod_vivo, _ = ejecuta_pipeline(salida_vivo, ["--erp-url", ERP_VIVO])
    cod_snap, _ = ejecuta_pipeline(salida_snap, ["--snapshot", str(SNAPSHOT)])
    vivo = _resultados(salida_vivo)
    snap = _resultados(salida_snap)
    distintas = sorted(k for k in set(vivo) & set(snap) if vivo[k] != snap[k])
    INFO.dato("exit (vivo / snapshot)", f"{cod_vivo} / {cod_snap}")
    INFO.dato("facturas comparadas", len(set(vivo) & set(snap)))
    INFO.dato("facturas con otra decision", len(distintas))
    INFO.comprueba("el snapshot da las mismas decisiones que el ERP vivo",
                   cod_vivo == 0 and cod_snap == 0 and not distintas,
                   f"{len(distintas)} diferencias")


def _primer_resultado(salida: Path) -> str:
    resultados = _resultados(salida)
    return next(iter(resultados.values()), "(sin salida)")


def _motivos(salida: Path) -> list[str]:
    if not salida.exists():
        return []
    for linea in salida.read_text(encoding="utf-8").splitlines():
        if linea.strip():
            return list(json.loads(linea).get("motivos") or [])
    return []


def _resultados(salida: Path) -> dict[str, str]:
    if not salida.exists():
        return {}
    salida_por_id: dict[str, str] = {}
    for linea in salida.read_text(encoding="utf-8").splitlines():
        if linea.strip():
            fila = json.loads(linea)
            salida_por_id[fila["file_id"]] = fila["result"]
    return salida_por_id


def _responde(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/erp/estado", timeout=timeout):
            return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _martillea(cliente: "erp.ERP", escapados: list[str]) -> None:
    for _ in range(CONSULTAS_POR_CLIENTE_429):
        try:
            cliente._consulta("/erp/asientos?pagina=1")
        except erp.ErrorERP as exc:
            escapados.append(str(exc))


def bloque_cache(tmp: Path, segundos_caliente: float, llamadas_caliente: int,
                 pdfs: Path, etiqueta: str, parcial: bool = False) -> None:
    INFO.titulo("E. OCR CAIDO / CACHE: LOTE CALIENTE CONTRA LOTE FRIO")
    INFO.nota(
        f"Medicion sobre {etiqueta}. La cache vive en {lectura.CACHE_OCR} y se\n"
        "indexa por sha256 del PDF. Se mueve temporalmente a un directorio\n"
        "propio, se mide el lote frio y se restaura byte a byte."
    )
    cache = lectura.CACHE_OCR
    antes = manifiesto(cache)
    INFO.dato("entradas de cache antes", len(antes))

    contador = {"n": 0, "t": 0.0}
    real = lectura.ocr_contenedor

    def espia(ruta: Path, timeout: int = 300) -> str:
        contador["n"] += 1
        t0 = time.monotonic()
        try:
            return real(ruta, timeout)
        finally:
            contador["t"] += time.monotonic() - t0

    guardada = tmp / "cache_ocr_original"
    if guardada.exists():
        shutil.rmtree(guardada)
    shutil.move(str(cache), str(guardada))
    segundos_frio = 0.0
    texto_frio = ""
    reconstruida: dict[str, str] = {}
    try:
        lectura.ocr_contenedor = espia
        try:
            salida = tmp / "frio" / "outcomes.jsonl"
            segundos_frio, texto_frio = corre_en_proceso(salida, facturas=pdfs)
        finally:
            lectura.ocr_contenedor = real
        reconstruida = manifiesto(cache)
    finally:
        if cache.exists():
            shutil.rmtree(cache)
        shutil.move(str(guardada), str(cache))

    despues = manifiesto(cache)
    INFO.dato("segundos (lote frio)", f"{segundos_frio:.2f}")
    INFO.dato("llamadas al servicio OCR (frio)", contador["n"])
    INFO.dato("segundos dentro de OCR (frio)", f"{contador['t']:.2f}")
    for linea in texto_frio.splitlines():
        if linea.startswith(("resultado", "lectura")):
            INFO.nota(linea.strip())
    INFO.dato("entradas de cache reconstruidas", len(reconstruida))
    INFO.dato("segundos (lote caliente)", f"{segundos_caliente:.2f}")
    INFO.dato("llamadas al servicio OCR (caliente)", llamadas_caliente)
    INFO.dato("factor de aceleracion", f"{segundos_frio / max(segundos_caliente, 1e-9):.1f}x")
    INFO.dato("entradas restauradas", len(despues))

    mismas_claves = (set(reconstruida) <= set(antes)) if parcial else (set(reconstruida) == set(antes))
    mismo_contenido = all(antes.get(k) == v for k, v in reconstruida.items())

    INFO.comprueba("el lote caliente no llama a OCR", llamadas_caliente == 0,
                   f"{llamadas_caliente} llamadas")
    INFO.comprueba("el lote frio si llama a OCR", contador["n"] > 0,
                   f"{contador['n']} llamadas")
    INFO.comprueba("el lote frio tarda mas que el caliente",
                   segundos_frio > segundos_caliente,
                   f"{segundos_frio:.2f}s > {segundos_caliente:.2f}s")
    INFO.comprueba("el lote frio reconstruye las mismas entradas de cache",
                   mismas_claves and bool(reconstruida),
                   f"{len(reconstruida)} reconstruidas sobre {len(antes)} previas")
    INFO.comprueba("el contenido reconstruido es identico byte a byte", mismo_contenido)
    INFO.comprueba("la cache original queda restaurada byte a byte",
                   despues == antes, f"{len(despues)} entradas")
    INFO.comprueba("no queda basura de la cache temporal", not guardada.exists())


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def seleccion_rapida(destino: Path, n_ocr: int) -> tuple[Path, int, int]:
    """Muestra dirigida para ``--rapido``: N PDF que necesitan OCR + N que no.

    Los PDF que exigen vision estan concentrados al final del lote, asi que un
    "los primeros N" no mediria nada. Se eligen por sha256 contra la cache.
    """
    claves = {f.stem for f in lectura.CACHE_OCR.glob("*.json")}
    pdfs = sorted(PDFS.glob("*.pdf"))
    con_ocr = [p for p in pdfs if lectura.sha256_pdf(p) in claves]
    sin_ocr = [p for p in pdfs if lectura.sha256_pdf(p) not in claves][:n_ocr]
    elegidos = con_ocr[:n_ocr] + sin_ocr
    destino.mkdir(parents=True, exist_ok=True)
    for pdf in elegidos:
        objetivo = destino / pdf.name
        try:
            os.link(pdf, objetivo)
        except OSError:
            objetivo.symlink_to(pdf.resolve())
    return destino, min(n_ocr, len(con_ocr)), len(elegidos)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Evidencia ejecutada de resiliencia (maisa).")
    p.add_argument("--rapido", action="store_true",
                   help="mide el lote frio sobre una muestra en vez de los 500 PDF")
    p.add_argument("--ocr-frios", type=int, default=6,
                   help="PDF que exigen OCR en la muestra de --rapido (por defecto 6)")
    p.add_argument("--erp-vivo", action="store_true",
                   help=f"mide tambien contra el bridge ERP vivo (necesita "
                        f"{BRIDGE} escuchando en el puerto 8009)")
    args = p.parse_args(argv)

    raiz_tmp = Path(tempfile.mkdtemp(prefix="evidencia_resiliencia_", dir=str(RAIZ)))
    print(f"directorio temporal : {raiz_tmp}")
    print(f"python              : {sys.executable}")
    print(f"cache de OCR        : {lectura.CACHE_OCR}")
    print(f"modo                : {'rapido' if args.rapido else 'completo'}")

    pdfs = PDFS
    etiqueta = f"los {len(sorted(PDFS.glob('*.pdf')))} PDF del lote"
    if args.rapido:
        pdfs, n_ocr, n_total = seleccion_rapida(raiz_tmp / "pdfs_rapido", args.ocr_frios)
        etiqueta = (f"una muestra de {n_total} PDF ({n_ocr} que exigen OCR y "
                    f"{n_total - n_ocr} con capa de texto)")

    try:
        buena, segundos_caliente, contador_ref, _ = bloque_referencia(raiz_tmp)
        llamadas_caliente = contador_ref["n"]
        if args.rapido:
            INFO.nota(
                "Modo rapido: el lote frio se mide sobre una muestra, asi que se\n"
                "vuelve a medir el caliente sobre exactamente la misma muestra\n"
                "para que la comparacion sea justa."
            )
            segundos_caliente, llamadas_caliente = _mide_caliente(raiz_tmp, pdfs)
            INFO.dato("segundos (caliente, muestra)", f"{segundos_caliente:.2f}")
            INFO.dato("llamadas a OCR (caliente, muestra)", llamadas_caliente)
        bloque_muerte(raiz_tmp)
        bloque_manipulacion(raiz_tmp, buena)
        bloque_sello(raiz_tmp, buena)
        bloque_erp(raiz_tmp)
        bloque_ocr_caido(raiz_tmp)
        bloque_cache(raiz_tmp, segundos_caliente, llamadas_caliente, pdfs, etiqueta,
                     parcial=args.rapido)
        bloque_pdf_roto(raiz_tmp)
        bloque_cache_manipulada(raiz_tmp)
        if args.erp_vivo:
            bloque_erp_vivo(raiz_tmp)
        return INFO.cierra()
    finally:
        shutil.rmtree(raiz_tmp, ignore_errors=True)
        print(f"\ndirectorio temporal borrado: {raiz_tmp}")


def _mide_caliente(tmp: Path, pdfs: Path) -> tuple[float, int]:
    """Lote caliente sobre el mismo subconjunto que el frio (modo rapido)."""
    contador = {"n": 0}
    real = lectura.ocr_contenedor

    def espia(ruta: Path, timeout: int = 300) -> str:
        contador["n"] += 1
        return real(ruta, timeout)

    lectura.ocr_contenedor = espia
    try:
        salida = tmp / "caliente" / "outcomes.jsonl"
        segundos, _ = corre_en_proceso(salida, facturas=pdfs)
    finally:
        lectura.ocr_contenedor = real
    return segundos, contador["n"]


if __name__ == "__main__":
    raise SystemExit(main())
