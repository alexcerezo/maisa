"""Traza de eventos **encadenada por hash**: quien decidio, con que y por que.

El `outcomes.jsonl` que se entrega es la respuesta; esto es la *justificacion*.
Cada evento se serializa de forma **canonica y determinista** (JSON con claves
ordenadas, separadores compactos y `Decimal` -> `str`) y su `hash` cubre
tambien el `hash_prev`, asi que la traza es una cadena: cambiar un solo
caracter de cualquier linea deja de cuadrar y `verifica` lo dice.

Que se puede responder con esto:

* **Que se leyo y de donde** -> evento ``lectura`` (sha256 del PDF, escalon,
  calidad, metodo, sospechosos).
* **Con que regla** -> evento ``lote`` (version de la norma, config, maestro)
  y el campo ``hechos`` de cada ``decision``.
* **Por que se decidio** -> ``linaje``/``explica`` reconstruyen la cadena de un
  ``file_id`` con sus motivos.
* **Que cambio entre dos ejecuciones** -> ``diff`` compara dos logs y dice, por
  ``file_id``, que decision cambio y con que motivo (si esta en los datos).

Vocabulario de eventos. Los cuatro tipos propios de esta traza:

* ``lote`` -- cabecera: que se leyo, con que norma y con que config.
* ``lectura`` -- un PDF leido: sha256, escalon, calidad y metodo.
* ``decision`` -- el resultado y los hechos que lo sostienen.
* ``fin`` -- cierre: contadores por resultado y por escalon.

Y los once del vocabulario cerrado del motor Rust legado (``TRASPASO.md``
3.7, ``docker/mongosh/02-schema-init.js``), con el valor exacto en
MAYUSCULAS que el Rust escribe en ``eventos.tipo``:

* ``OCR_OK`` / ``OCR_FAIL`` -- la lectura OCR salio bien o fallo de verdad
  (``{"lines": [], "pages": 0}`` no es lo mismo que "el PDF no trae NIF").
* ``ERP_CACHE_HIT`` / ``ERP_CACHE_MISS`` -- la consulta al ERP se sirvio de
  cache o hubo que ir al servicio.
* ``ERP_RETRY_ORA_00600`` -- reintento por error ORA del ERP, con backoff.
* ``ERP_RETRY_SES_401`` -- sesion caducada: relogin y repetir la llamada.
* ``ERP_RETRY_ERP_429`` -- el ERP pide esperar (limite de peticiones).
* ``REVISION_ABIERTA`` / ``REVISION_RESUELTA`` -- ciclo de vida de una
  revision humana.
* ``DECISION_EMITIDA`` -- la decision se emitio, con la version de la norma.
* ``EXPEDIENTE_ESTADO`` -- transicion de estado de un expediente.

``TIPOS`` reune los quince y ``es_tipo_valido`` dice si un tipo esta dentro.
El vocabulario es informativo: ``anota()`` no lo impone, porque hay trazas ya
escritas y porque el formato en disco no puede cambiar.

Nada de esto toca la entrega: el modulo es autonomo y solo se activa con
``--traza-hash`` en ``maisa.procesa`` (por defecto, apagado).
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

# Hash previo del primer evento: 64 ceros (un sha256 "vacio" reconocible).
GENESIS = "0" * 64
# `file_id` de los eventos que no son de un fichero concreto (lote, fin).
GLOBAL = "*"
# Campos exactos de un evento en disco, en el orden en que se serializa.
CAMPOS = ("seq", "ts", "tipo", "file_id", "datos", "hash_prev", "hash")

TIPO_LOTE = "lote"
TIPO_LECTURA = "lectura"
TIPO_DECISION = "decision"
TIPO_FIN = "fin"

# Vocabulario cerrado del motor Rust legado (`TRASPASO.md` 3.7 y
# `docker/mongosh/02-schema-init.js`): once tipos, con el valor exacto en
# MAYUSCULAS que el Rust guarda en `eventos.tipo`. "No se inventa ninguno mas".
TIPO_OCR_OK = "OCR_OK"
TIPO_OCR_FAIL = "OCR_FAIL"
TIPO_ERP_CACHE_HIT = "ERP_CACHE_HIT"
TIPO_ERP_CACHE_MISS = "ERP_CACHE_MISS"
TIPO_ERP_RETRY_ORA_00600 = "ERP_RETRY_ORA_00600"
TIPO_ERP_RETRY_SES_401 = "ERP_RETRY_SES_401"
TIPO_ERP_RETRY_ERP_429 = "ERP_RETRY_ERP_429"
TIPO_REVISION_ABIERTA = "REVISION_ABIERTA"
TIPO_REVISION_RESUELTA = "REVISION_RESUELTA"
TIPO_DECISION_EMITIDA = "DECISION_EMITIDA"
TIPO_EXPEDIENTE_ESTADO = "EXPEDIENTE_ESTADO"

# Todos los tipos admitidos: los cuatro propios (minusculas) y los once del
# vocabulario del Rust. Es solo una lista de nombres: no entra en el hash.
TIPOS = (
    TIPO_LOTE,
    TIPO_LECTURA,
    TIPO_DECISION,
    TIPO_FIN,
    TIPO_OCR_OK,
    TIPO_OCR_FAIL,
    TIPO_ERP_CACHE_HIT,
    TIPO_ERP_CACHE_MISS,
    TIPO_ERP_RETRY_ORA_00600,
    TIPO_ERP_RETRY_SES_401,
    TIPO_ERP_RETRY_ERP_429,
    TIPO_REVISION_ABIERTA,
    TIPO_REVISION_RESUELTA,
    TIPO_DECISION_EMITIDA,
    TIPO_EXPEDIENTE_ESTADO,
)


def es_tipo_valido(tipo: str) -> bool:
    """``True`` si ``tipo`` esta en el vocabulario cerrado de ``TIPOS``.

    Es una consulta, no una puerta: ``anota()`` sigue aceptando cualquier
    ``tipo``, para no invalidar las trazas ya escritas ni el formato en disco.
    Sirve para que quien anade eventos nuevos se quede dentro del vocabulario
    en vez de inventarse uno.
    """
    return tipo in TIPOS


class TrazaError(Exception):
    """Error de uso de la traza (no de contenido: eso se reporta, no se lanza)."""


class RegistroInmutable(TrazaError, TypeError):
    """Se intento modificar o borrar algo ya escrito. La traza es append-only."""


class EventoInvalido(TrazaError, ValueError):
    """Un evento no se puede construir o no es serializable de forma canonica."""


class Problema(str):
    """Un problema de verificacion.

    Es un ``str`` (se puede imprimir y unir sin ceremonia) que ademas lleva
    ``clase``, ``linea`` y ``detalle`` para poder tratarlo como dato.
    """

    __slots__ = ("clase", "linea", "detalle")

    def __new__(cls, clase: str, linea: int, detalle: str) -> "Problema":
        texto = f"linea {linea}: {clase}: {detalle}" if linea else f"{clase}: {detalle}"
        obj = super().__new__(cls, texto)
        obj.clase = clase
        obj.linea = linea
        obj.detalle = detalle
        return obj


# --------------------------------------------------------------------------- #
# Serializacion canonica
# --------------------------------------------------------------------------- #


def _limpia(valor: Any) -> Any:
    """Normaliza a tipos JSON deterministas (``Decimal`` -> ``str``, etc.).

    Es la unica puerta por la que entra un dato en la traza, asi que el mismo
    objeto produce siempre la misma cadena: sin esto, un ``Decimal("3012.89")``
    y un ``float`` darian hashes distintos para la misma factura.
    """
    if valor is None or isinstance(valor, (bool, int, str)):
        return valor
    if isinstance(valor, float):
        if valor != valor or valor in (float("inf"), float("-inf")):
            raise EventoInvalido(f"numero no finito en la traza: {valor!r}")
        return valor
    if isinstance(valor, Decimal):
        return str(valor)
    if isinstance(valor, (datetime, date, time)):
        return valor.isoformat()
    if isinstance(valor, Path):
        return str(valor)
    if isinstance(valor, Mapping):
        return {str(k): _limpia(v) for k, v in valor.items()}
    if isinstance(valor, (set, frozenset)):
        # Un conjunto no tiene orden: se ordena por su forma canonica.
        return sorted((_limpia(v) for v in valor), key=canoniza)
    if isinstance(valor, (list, tuple)):
        return [_limpia(v) for v in valor]
    if dataclasses.is_dataclass(valor) and not isinstance(valor, type):
        return {f.name: _limpia(getattr(valor, f.name)) for f in dataclasses.fields(valor)}
    if hasattr(valor, "como_dict"):
        return _limpia(valor.como_dict())
    raise EventoInvalido(f"tipo no serializable en la traza: {type(valor).__name__}")


def canoniza(valor: Any) -> str:
    """Serializacion canonica: misma entrada -> misma cadena, siempre."""
    return json.dumps(
        _limpia(valor), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    )


def _hash_evento(
    seq: int, ts: str, tipo: str, file_id: str, datos: Mapping, hash_prev: str
) -> str:
    """sha256 de la forma canonica del evento **incluido** ``hash_prev``."""
    payload = {
        "seq": seq, "ts": ts, "tipo": tipo,
        "file_id": file_id, "datos": datos, "hash_prev": hash_prev,
    }
    return hashlib.sha256(canoniza(payload).encode("utf-8")).hexdigest()


def reloj_utc() -> str:
    """Marca de tiempo ISO-8601 UTC con milisegundos (ordena como texto)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class DatosCongelados(dict):
    """``dict`` de solo lectura: sigue siendo un ``dict``, pero no se muta.

    Asi ``evento.datos`` se puede volcar a JSON y comparar como cualquier dict,
    pero ``evento.datos["result"] = "PAGAR"`` falla en vez de mentir.
    """

    __slots__ = ()

    def _no_muta(self, *_: Any, **__: Any) -> Any:
        raise RegistroInmutable("los datos de un evento son inmutables; anota() uno nuevo")

    __setitem__ = _no_muta
    __delitem__ = _no_muta
    pop = _no_muta
    popitem = _no_muta
    clear = _no_muta
    update = _no_muta
    setdefault = _no_muta


# --------------------------------------------------------------------------- #
# Evento
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Evento:
    """Un hecho de la traza. Inmutable y con su hash encadenado.

    ``hash`` cubre ``seq``, ``ts``, ``tipo``, ``file_id``, ``datos`` y
    ``hash_prev``: cambiar cualquier campo (o el eslabon anterior) invalida el
    hash y con el toda la cadena que venga detras.

    Nota: el ``Evento`` **no** valida su propio ``hash`` al construirse, para
    poder leer un log manipulado y reportarlo. Usa ``comprueba()`` para
    verificar uno concreto y ``verifica()`` para el log entero.
    """

    seq: int
    ts: str
    tipo: str
    file_id: str
    datos: dict = field(default_factory=dict)
    hash_prev: str = GENESIS
    hash: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq < 0:
            raise EventoInvalido(f"seq invalido: {self.seq!r}")
        for nombre in ("ts", "tipo", "file_id"):
            valor = getattr(self, nombre)
            if not isinstance(valor, str) or not valor:
                raise EventoInvalido(f"{nombre} invalido: {valor!r}")
        if not isinstance(self.datos, Mapping):
            raise EventoInvalido(f"datos debe ser un dict, no {type(self.datos).__name__}")
        if not isinstance(self.hash_prev, str):
            raise EventoInvalido(f"hash_prev invalido: {self.hash_prev!r}")
        object.__setattr__(self, "datos", DatosCongelados(self.datos))
        if not self.hash:
            object.__setattr__(self, "hash", self.recalcula())
        if len(self.hash) != 64 or not self.hash_prev or len(self.hash_prev) != 64:
            raise EventoInvalido("hash/hash_prev deben ser sha256 de 64 caracteres")

    @classmethod
    def crea(
        cls,
        *,
        seq: int,
        tipo: str,
        file_id: str = GLOBAL,
        datos: Mapping | None = None,
        hash_prev: str = GENESIS,
        ts: str | None = None,
    ) -> "Evento":
        """Construye el evento y calcula su hash (el camino normal)."""
        return cls(
            seq=seq, ts=ts if ts is not None else reloj_utc(), tipo=tipo,
            file_id=file_id, datos=dict(datos or {}), hash_prev=hash_prev,
        )

    def recalcula(self) -> str:
        """Hash que *deberia* tener este evento segun su contenido."""
        return _hash_evento(self.seq, self.ts, self.tipo, self.file_id, self.datos, self.hash_prev)

    def comprueba(self) -> bool:
        """``True`` si el hash declarado cuadra con el contenido."""
        return self.hash == self.recalcula()

    def como_dict(self) -> dict:
        """Copia mutable del evento (para JSON, informes o comparaciones)."""
        return {
            "seq": self.seq, "ts": self.ts, "tipo": self.tipo,
            "file_id": self.file_id, "datos": dict(self.datos),
            "hash_prev": self.hash_prev, "hash": self.hash,
        }

    def canonico(self) -> str:
        """La linea exacta que se escribe en disco (canonica y ordenada)."""
        return canoniza(self.como_dict())

    def __str__(self) -> str:
        pista = ""
        for clave in ("result", "resultado", "motivo"):
            if self.datos.get(clave):
                pista = f" -> {self.datos[clave]}"
                break
        return f"#{self.seq:04d} {self.ts} [{self.tipo}] {self.file_id}{pista}"


# --------------------------------------------------------------------------- #
# Registro append-only
# --------------------------------------------------------------------------- #


class Registro:
    """Log de eventos append-only, opcionalmente respaldado por un fichero.

    Si se le pasa ``ruta``, cada ``anota()`` escribe y hace ``flush``: un
    proceso interrumpido deja un prefijo legible (y truncado *detectable*), no
    un fichero a medias sin aviso. Sin ``ruta`` el registro vive solo en
    memoria y se puede volcar despues con ``escribe()``.
    """

    def __init__(
        self,
        ruta: Path | str | None = None,
        *,
        reloj: Callable[[], str] | None = None,
        continuar: bool = False,
    ) -> None:
        self._eventos: list[Evento] = []
        self._ruta = Path(ruta) if ruta is not None else None
        self._reloj = reloj or reloj_utc
        self._fh = None
        if self._ruta is not None:
            self._abre(continuar)

    def _abre(self, continuar: bool) -> None:
        self._ruta.parent.mkdir(parents=True, exist_ok=True)
        if continuar and self._ruta.exists() and self._ruta.stat().st_size:
            problemas = verifica(self._ruta)
            if problemas:
                raise TrazaError(
                    f"no se puede continuar {self._ruta}: la traza ya esta rota "
                    f"({len(problemas)} problemas, el primero: {problemas[0]})"
                )
            self._eventos.extend(carga(self._ruta))
            self._fh = self._ruta.open("a", encoding="utf-8")
        else:
            self._fh = self._ruta.open("w", encoding="utf-8")

    @property
    def ruta(self) -> Path | None:
        return self._ruta

    @property
    def cabeza(self) -> str:
        """Hash del ultimo evento (o ``GENESIS`` si esta vacio): el eslabon siguiente."""
        return self._eventos[-1].hash if self._eventos else GENESIS

    def anota(self, tipo: str, file_id: str = GLOBAL, **datos: Any) -> Evento:
        """Anade un evento y devuelve el evento escrito. La unica escritura posible."""
        ev = Evento.crea(
            seq=len(self._eventos), ts=self._reloj(), tipo=tipo,
            file_id=file_id, datos=datos, hash_prev=self.cabeza,
        )
        self._eventos.append(ev)
        if self._fh is not None:
            self._fh.write(ev.canonico() + "\n")
            self._fh.flush()
        return ev

    # -- lectura ---------------------------------------------------------- #
    @property
    def eventos(self) -> tuple[Evento, ...]:
        return tuple(self._eventos)

    def __len__(self) -> int:
        return len(self._eventos)

    def __iter__(self) -> Iterator[Evento]:
        return iter(tuple(self._eventos))

    def __getitem__(self, indice: int | slice) -> Evento | tuple[Evento, ...]:
        if isinstance(indice, slice):
            return tuple(self._eventos[indice])
        return self._eventos[indice]

    def __repr__(self) -> str:
        return f"Registro({len(self._eventos)} eventos, ruta={self._ruta})"

    # -- inmutabilidad ---------------------------------------------------- #
    def _inmutable(self, *_: Any, **__: Any) -> Any:
        raise RegistroInmutable(
            "la traza es append-only: no se puede modificar ni borrar un evento ya escrito"
        )

    __setitem__ = _inmutable
    __delitem__ = _inmutable
    append = _inmutable
    extend = _inmutable
    insert = _inmutable
    remove = _inmutable
    pop = _inmutable
    popitem = _inmutable
    clear = _inmutable
    sort = _inmutable
    reverse = _inmutable
    update = _inmutable
    modifica = _inmutable
    borra = _inmutable

    # -- salida ----------------------------------------------------------- #
    def escribe(self, ruta: Path | str) -> Path:
        """Vuelca el registro entero como JSONL canonico (una linea por evento)."""
        destino = Path(ruta)
        destino.parent.mkdir(parents=True, exist_ok=True)
        with destino.open("w", encoding="utf-8") as fh:
            for ev in self._eventos:
                fh.write(ev.canonico() + "\n")
        return destino

    def cierra(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "Registro":
        return self

    def __exit__(self, *_: Any) -> None:
        self.cierra()

    def sello(self) -> str:
        """Hash de la cabeza: el unico valor que hay que publicar para anclar la traza."""
        return self.cabeza

    def verifica(self, sello: str | None = None) -> list[Problema]:
        """Problemas del log en disco (vacio == traza integra)."""
        if self._fh is not None:
            self._fh.flush()
        if self._ruta is None:
            raise TrazaError("registro en memoria: usa verifica(ruta) sobre el fichero")
        return verifica(self._ruta, sello=sello)

    # -- linaje y comparacion --------------------------------------------- #
    def linaje(self, file_id: str) -> list[Evento]:
        """Cadena de eventos de un fichero, en orden de escritura."""
        return [ev for ev in self._eventos if ev.file_id == file_id]

    def explica(self, file_id: str) -> dict | None:
        """Respuesta a "por que se decidio esto": hechos, motivos y cadena."""
        cadena = self.linaje(file_id)
        if not cadena:
            return None
        return _explica(file_id, cadena)

    def decisiones(self) -> dict[str, Evento]:
        """Ultima decision de cada ``file_id``."""
        return _decisiones(self._eventos)

    def diff(self, otro: "Registro", *, solo_cambios: bool = True) -> list[Cambio]:
        """Compara las decisiones de este registro con las de ``otro``."""
        return _diff(self._eventos, otro._eventos, solo_cambios=solo_cambios)


# --------------------------------------------------------------------------- #
# Verificacion
# --------------------------------------------------------------------------- #


def _lineas(ruta: Path) -> tuple[list[str], list[Problema]]:
    """Parte el fichero en lineas y avisa de truncamiento o ilegibilidad."""
    problemas: list[Problema] = []
    ruta = Path(ruta)
    if not ruta.exists():
        return [], [Problema("no_existe", 0, f"no existe el fichero {ruta}")]
    try:
        texto = ruta.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        return [], [Problema("ilegible", 0, f"no se pudo leer {ruta}: {exc}")]
    if texto == "":
        return [], problemas
    if not texto.endswith("\n"):
        # Escritura interrumpida: la ultima linea puede estar a medias.
        problemas.append(
            Problema("truncado", len(texto.split("\n")), "el log no termina en salto de linea")
        )
        return texto.split("\n"), problemas
    return texto.split("\n")[:-1], problemas


def verifica(ruta: Path | str, *, sello: str | None = None) -> list[Problema]:
    """Relee el log y devuelve la lista de problemas (vacia == traza integra).

    Comprueba, linea a linea y sin abortar ante una linea rota: hash roto,
    ``hash_prev`` que no encadena, ``seq`` discontinuo, JSON invalido, lineas
    vacias, truncamiento y perdida de la forma canonica. Detecta el cambio de
    **un solo caracter** en cualquier linea, no solo en la ultima.
    """
    problemas: list[Problema] = []
    lineas, avisos = _lineas(Path(ruta))
    problemas.extend(avisos)

    prev_hash = GENESIS
    prev_seq = -1
    perdidas = 0
    cadena_incierta = False
    for num, linea in enumerate(lineas, 1):
        if not linea.strip():
            # Una linea en blanco es ruido, no un evento perdido: no desplaza el seq.
            problemas.append(Problema("linea_vacia", num, "linea en blanco en medio del log"))
            continue
        try:
            obj = json.loads(linea)
        except json.JSONDecodeError as exc:
            problemas.append(
                Problema("json_invalido", num, f"no es JSON: {exc.msg} (columna {exc.colno})")
            )
            perdidas += 1
            cadena_incierta = True
            continue
        if not isinstance(obj, dict):
            problemas.append(
                Problema("evento_invalido", num, f"la linea es {type(obj).__name__}, no un objeto")
            )
            perdidas += 1
            cadena_incierta = True
            continue
        faltan = [c for c in CAMPOS if c not in obj]
        if faltan:
            problemas.append(
                Problema("evento_invalido", num, f"faltan campos: {', '.join(faltan)}")
            )
            perdidas += 1
            cadena_incierta = True
            continue

        recalculado = _hash_evento(
            obj["seq"], obj["ts"], obj["tipo"], obj["file_id"], obj["datos"], obj["hash_prev"]
        )
        declarado = obj["hash"]
        if declarado != recalculado:
            problemas.append(
                Problema(
                    "hash_roto", num,
                    f"el hash declarado {str(declarado)[:12]}... no cuadra con el contenido "
                    f"({recalculado[:12]}...)",
                )
            )
        if not cadena_incierta and obj["hash_prev"] != prev_hash:
            problemas.append(
                Problema(
                    "cadena_rota", num,
                    f"hash_prev {str(obj['hash_prev'])[:12]}... no es el hash del evento "
                    f"anterior ({prev_hash[:12]}...)",
                )
            )
        cadena_incierta = False

        esperado = prev_seq + 1 + perdidas
        if obj["seq"] != esperado:
            problemas.append(
                Problema("seq_discontinuo", num, f"seq={obj['seq']} y se esperaba {esperado}")
            )
        if linea != canoniza(obj):
            problemas.append(
                Problema(
                    "formato_no_canonico", num,
                    "la linea no esta en forma canonica (claves ordenadas, sin espacios)",
                )
            )

        if isinstance(declarado, str):
            prev_hash = declarado
        if isinstance(obj["seq"], int) and not isinstance(obj["seq"], bool):
            prev_seq = obj["seq"]
        perdidas = 0

    if sello is not None and prev_hash != sello:
        problemas.append(
            Problema(
                "sello_distinto", len(lineas),
                f"la cabeza del log es {prev_hash[:12]}... y se esperaba {str(sello)[:12]}...",
            )
        )
    return problemas


def carga(ruta: Path | str) -> list[Evento]:
    """Eventos legibles de un log, en orden. Las lineas rotas se saltan (no revientan)."""
    eventos: list[Evento] = []
    lineas, _ = _lineas(Path(ruta))
    for linea in lineas:
        if not linea.strip():
            continue
        try:
            obj = json.loads(linea)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or any(c not in obj for c in CAMPOS):
            continue
        try:
            eventos.append(
                Evento(
                    seq=obj["seq"], ts=obj["ts"], tipo=obj["tipo"], file_id=obj["file_id"],
                    datos=obj["datos"], hash_prev=obj["hash_prev"], hash=obj["hash"],
                )
            )
        except (EventoInvalido, TypeError):
            continue
    return eventos


def sello(ruta: Path | str) -> str:
    """Hash de la cabeza del log: el valor a publicar para anclar la traza entera."""
    eventos = carga(ruta)
    return eventos[-1].hash if eventos else GENESIS


# --------------------------------------------------------------------------- #
# Linaje y diff
# --------------------------------------------------------------------------- #


def _decisiones(eventos: Iterable[Evento]) -> dict[str, Evento]:
    """Ultima decision por ``file_id``."""
    por_file: dict[str, Evento] = {}
    for ev in eventos:
        if ev.tipo == TIPO_DECISION:
            por_file[ev.file_id] = ev
    return por_file


def _hechos_fallidos(ev: Evento) -> list[str]:
    """Hechos que no se cumplieron, ya redactados (la parte util del "por que")."""
    salida: list[str] = []
    for hecho in ev.datos.get("hechos") or ():
        if not isinstance(hecho, Mapping) or hecho.get("ok"):
            continue
        etiqueta = hecho.get("nombre") or hecho.get("regla") or "?"
        salida.append(f"{etiqueta}: {hecho.get('motivo', 'sin motivo declarado')}")
    return salida


def _explica(file_id: str, cadena: list[Evento]) -> dict:
    decision = next((ev for ev in reversed(cadena) if ev.tipo == TIPO_DECISION), None)
    lectura = next((ev for ev in reversed(cadena) if ev.tipo == TIPO_LECTURA), None)
    por_que = list(_hechos_fallidos(decision)) if decision else []
    if decision:
        por_que.extend(str(m) for m in decision.datos.get("motivos") or ())
    return {
        "file_id": file_id,
        "resultado": decision.datos.get("result") if decision else None,
        "motivos": list(decision.datos.get("motivos") or ()) if decision else [],
        "version_norma": decision.datos.get("version_norma") if decision else None,
        "sha256": lectura.datos.get("sha256") if lectura else None,
        "eventos": [ev.como_dict() for ev in cadena],
        "por_que": por_que,
    }


def linaje(ruta: Path | str, file_id: str) -> list[Evento]:
    """Cadena de eventos de un ``file_id`` dentro de un log."""
    return [ev for ev in carga(ruta) if ev.file_id == file_id]


def explica(ruta: Path | str, file_id: str) -> dict | None:
    """Version de ``Registro.explica`` sobre un log en disco."""
    cadena = linaje(ruta, file_id)
    return _explica(file_id, cadena) if cadena else None


@dataclass(frozen=True, slots=True)
class Cambio:
    """Una decision que no es igual entre dos ejecuciones."""

    file_id: str
    clase: str  # "cambio" | "solo_en_a" | "solo_en_b"
    antes: str | None
    despues: str | None
    motivo: str
    antes_motivos: tuple[str, ...] = ()
    despues_motivos: tuple[str, ...] = ()

    def como_dict(self) -> dict:
        return dataclasses.asdict(self)

    def __str__(self) -> str:
        return f"{self.file_id}: {self.antes} -> {self.despues} ({self.clase}) {self.motivo}"


def _motivo(ev_a: Evento | None, ev_b: Evento | None) -> str:
    """Motivo del cambio, tomado de los datos de la traza cuando existe."""
    da = ev_a.datos if ev_a else {}
    db = ev_b.datos if ev_b else {}
    for clave in ("motivo", "razon", "por_que", "explicacion"):
        for datos in (db, da):
            if datos.get(clave):
                return str(datos[clave])
    motivos_a = tuple(da.get("motivos") or ())
    motivos_b = tuple(db.get("motivos") or ())
    if motivos_a != motivos_b:
        return f"motivos distintos: {list(motivos_a)} -> {list(motivos_b)}"
    if da.get("version_norma") != db.get("version_norma"):
        return f"norma distinta: {da.get('version_norma')} -> {db.get('version_norma')}"
    if da.get("sha256") != db.get("sha256") and da.get("sha256") and db.get("sha256"):
        return "el documento cambio (sha256 distinto)"
    return "sin motivo declarado en la traza"


def _diff(
    eventos_a: Iterable[Evento], eventos_b: Iterable[Evento], *, solo_cambios: bool = True
) -> list[Cambio]:
    da, db = _decisiones(eventos_a), _decisiones(eventos_b)
    cambios: list[Cambio] = []
    for file_id in sorted(set(da) | set(db)):
        ev_a, ev_b = da.get(file_id), db.get(file_id)
        antes = ev_a.datos.get("result") if ev_a else None
        despues = ev_b.datos.get("result") if ev_b else None
        if ev_a is not None and ev_b is not None and antes == despues:
            if solo_cambios:
                continue
            clase = "sin_cambio"
        elif ev_a is None:
            clase = "solo_en_b"
        elif ev_b is None:
            clase = "solo_en_a"
        else:
            clase = "cambio"
        cambios.append(
            Cambio(
                file_id=file_id, clase=clase, antes=antes, despues=despues,
                motivo=_motivo(ev_a, ev_b),
                antes_motivos=tuple(ev_a.datos.get("motivos") or ()) if ev_a else (),
                despues_motivos=tuple(ev_b.datos.get("motivos") or ()) if ev_b else (),
            )
        )
    return cambios


def diff(ruta_a: Path | str, ruta_b: Path | str, *, solo_cambios: bool = True) -> list[Cambio]:
    """Decisiones que cambian entre dos ejecuciones (log A -> log B)."""
    return _diff(carga(ruta_a), carga(ruta_b), solo_cambios=solo_cambios)


def resumen_diff(cambios: Iterable[Cambio]) -> dict:
    """Contadores del diff, listos para imprimir en el pitch."""
    por_clase: dict[str, int] = {}
    transiciones: dict[str, int] = {}
    for cambio in cambios:
        por_clase[cambio.clase] = por_clase.get(cambio.clase, 0) + 1
        clave = f"{cambio.antes} -> {cambio.despues}"
        transiciones[clave] = transiciones.get(clave, 0) + 1
    return {
        "total": sum(por_clase.values()),
        "por_clase": dict(sorted(por_clase.items())),
        "por_transicion": dict(sorted(transiciones.items(), key=lambda kv: -kv[1])),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _cmd_verifica(args: argparse.Namespace) -> int:
    problemas = verifica(args.log, sello=args.sello)
    if not problemas:
        print(f"{args.log}: OK ({len(carga(args.log))} eventos, sello {sello(args.log)[:12]}...)")
        return 0
    print(f"{args.log}: {len(problemas)} problema(s)")
    for problema in problemas:
        print(f"  - {problema}")
    return 1


def _cmd_diff(args: argparse.Namespace) -> int:
    cambios = diff(args.log1, args.log2, solo_cambios=not args.todos)
    resumen = resumen_diff(cambios)
    print(f"{args.log1} -> {args.log2}")
    print(f"decisiones distintas: {resumen['total']}  {resumen['por_clase']}")
    if resumen["por_transicion"]:
        print(f"transiciones: {resumen['por_transicion']}")
    for cambio in cambios[: args.max]:
        print(f"  - {cambio}")
        if cambio.antes_motivos != cambio.despues_motivos:
            print(f"      antes  : {list(cambio.antes_motivos)}")
            print(f"      ahora  : {list(cambio.despues_motivos)}")
    if len(cambios) > args.max:
        print(f"  ... y {len(cambios) - args.max} mas")
    return 0


def _cmd_linaje(args: argparse.Namespace) -> int:
    explicacion = explica(args.log, args.file_id)
    if explicacion is None:
        print(f"{args.file_id}: sin eventos en {args.log}")
        return 1
    print(f"{explicacion['file_id']}: {explicacion['resultado']} (norma {explicacion['version_norma']})")
    for evento in explicacion["eventos"]:
        print(f"  {evento['seq']:04d} [{evento['tipo']}] {evento['ts']} hash={evento['hash'][:12]}...")
    print("por que:")
    for razon in explicacion["por_que"] or ["sin motivos declarados"]:
        print(f"  - {razon}")
    return 0


def _cmd_sello(args: argparse.Namespace) -> int:
    print(sello(args.log))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Traza encadenada por hash: verifica, linaje y diff.")
    sub = p.add_subparsers(dest="orden", required=True)

    v = sub.add_parser("verifica", help="revisa la integridad de un log")
    v.add_argument("log", type=Path)
    v.add_argument("--sello", default=None, help="hash esperado de la cabeza del log")
    v.set_defaults(func=_cmd_verifica)

    d = sub.add_parser("diff", help="compara las decisiones de dos ejecuciones")
    d.add_argument("log1", type=Path)
    d.add_argument("log2", type=Path)
    d.add_argument("--todos", action="store_true", help="incluye tambien las decisiones iguales")
    d.add_argument("--max", type=int, default=20, help="cuantos cambios se detallan")
    d.set_defaults(func=_cmd_diff)

    lin = sub.add_parser("linaje", help="cadena de eventos de un file_id")
    lin.add_argument("log", type=Path)
    lin.add_argument("file_id")
    lin.set_defaults(func=_cmd_linaje)

    s = sub.add_parser("sello", help="hash de la cabeza del log")
    s.add_argument("log", type=Path)
    s.set_defaults(func=_cmd_sello)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
