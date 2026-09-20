"""Lectura de la traza del motor (`outcomes_traza.jsonl`) y de la entrega.

Decision de diseno: la traza se carga **en memoria** (500 lineas, ~1.3 MB) en
lugar de releerse en cada peticion. Se comprueba el `mtime`/tamano en cada
acceso, asi que si el motor vuelve a escribir la traza la API la recarga sola sin
reiniciar. Mongo no guarda aun `expedientes`, por lo que la traza en disco es
hoy la unica fuente de verdad de las decisiones.

Aqui no se escribe nada: el fichero de la traza es material de auditoria del
motor y la API solo lo lee.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

logger = logging.getLogger("albertitos-api")

RESULTADOS = ("PAGAR", "NO_PAGAR", "ESCALAR")

# El unico evento de la traza encadenada que trae una decision. El resto
# (`lote`, `lectura`, `fin`) describe la pasada, no la factura. Se repite aqui en
# vez de importarlo de `maisa.trace` porque la API es un paquete aparte que lee
# ficheros del motor, no codigo del motor.
TIPO_DECISION = "decision"

# Firma que se publica cuando no hay traza legible: es estable a proposito, para
# que un cliente que cachea no vea cambiar el ETag sin motivo.
FIRMA_SIN_TRAZA = "sin-traza"

# Tamano del bloque con el que se calcula el sha256 del fichero.
_BLOQUE_HASH = 1 << 20

# Campos del resumen que se indexan para la busqueda de texto libre.
CAMPOS_BUSQUEDA = ("file_id", "proveedor", "pedido", "asiento")


def _num(valor: Any) -> float | None:
    """Convierte los importes de la traza (vienen como str) a numero."""
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    try:
        return float(str(valor).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _texto(valor: Any) -> str | None:
    if valor is None:
        return None
    texto = str(valor).strip()
    return texto or None


def divisa_principal(campos: dict) -> str:
    """La divisa con la que hay que leer los importes de una factura.

    De las divisas que declara el documento manda la que **no** es la del ERP: es
    la que dispara R7 y la unica que cambia como se lee la cifra. Sin declaracion
    ninguna se asume la del ERP, que es lo que hace el motor al no escalar: el
    silencio se lee como euros, no como una duda.

    Se resuelve aqui y no en el cliente porque el listado y el detalle tienen que
    decir lo mismo, y porque el dia que el motor anada una divisa el visor no
    tiene que saber cual gana.
    """
    aceptada = _texto(campos.get("divisa_erp")) or "EUR"
    declaradas = campos.get("divisa_documento") or []
    if isinstance(declaradas, str):
        declaradas = [declaradas]
    limpias = [str(d).strip().upper() for d in declaradas if _texto(d)]
    return next((d for d in limpias if d != aceptada), limpias[0] if limpias else aceptada)


def motivo_principal(registro: dict) -> str | None:
    """Motivo que resume la decision, para la columna del visor.

    Orden: primer motivo explicito -> primer hecho incumplido (los duros primero)
    -> None cuando no hay ninguna incidencia que contar.
    """
    motivos = [m for m in (registro.get("motivos") or []) if _texto(m)]
    if motivos:
        return str(motivos[0])
    hechos = registro.get("hechos") or []
    incumplidos = [h for h in hechos if not h.get("ok")]
    incumplidos.sort(key=lambda h: (not h.get("duro"), bool(h.get("informativo"))))
    for hecho in incumplidos:
        texto = _texto(hecho.get("motivo")) or _texto(hecho.get("regla"))
        if texto:
            return texto
    return None


def carga_cola(ruta: Path) -> dict[str, dict]:
    """Lee el sidecar de la cola de revision, indexado por `file_id`.

    Lo escribe `motor/tools/cola_revision.py` y es **opcional**: sin el fichero
    la cola no tiene anotaciones y todo se revisa a mano, que es el
    comportamiento de siempre. Un sidecar ilegible tampoco puede tumbar la API:
    se ignora linea a linea, como la traza.

    Cada linea trae `{"file_id": ..., "segunda_lectura": {...}}`. Solo se usa
    `segunda_lectura`: el resto de la linea (motivos de la norma, escalon,
    resultado hibrido) es para leer el fichero a mano.
    """
    cola: dict[str, dict] = {}
    if not ruta.is_file():
        return cola
    try:
        with ruta.open("r", encoding="utf-8") as fichero:
            for numero, linea in enumerate(fichero, start=1):
                linea = linea.strip()
                if not linea:
                    continue
                try:
                    registro = json.loads(linea)
                except json.JSONDecodeError:
                    logger.warning("Cola: linea %s ilegible, se ignora.", numero)
                    continue
                file_id = _texto(registro.get("file_id"))
                anotacion = registro.get("segunda_lectura")
                if file_id and isinstance(anotacion, dict):
                    cola[file_id] = anotacion
    except OSError as exc:
        logger.warning("Cola: no se pudo leer %s (%s).", ruta, exc)
        return {}
    return cola


def segunda_lectura_resumen(anotacion: dict | None) -> dict | None:
    """La parte de la anotacion que necesita el listado de la cola.

    `confirmable` es lo que permite cerrar la incidencia sin abrirla; `desvio`
    es lo contrario y no puede viajar sin la evidencia completa, que va en el
    detalle.

    El campo se llama `segunda_lectura` y no `revision` a proposito: `revision`
    ya es el estado de revision **humana** (`PENDIENTE`/`RESUELTA`) que guarda
    Mongo y expone `PUT /api/facturas/{file_id}/revision`. Son cosas distintas:
    esto es lo que la maquina aporta, aquello lo que decide la persona.
    """
    if not anotacion:
        return None
    return {
        "confirmable": bool(anotacion.get("confirmable")),
        "desvio": bool(anotacion.get("desvio")),
    }


def aplanar(registro: dict) -> dict | None:
    """Traduce una linea de la traza a la forma plana que espera el contrato.

    La traza del motor tiene **dos formas** y esta es la unica puerta por la que
    entran las dos:

    - sin `--traza-hash`, una linea por factura, con `result`, `campos`, `lote`
      y lo demas en la raiz;
    - con `--traza-hash`, que es como se genera la traza de produccion, un
      diario encadenado de `{seq, ts, tipo, file_id, datos, hash, hash_prev}`
      con un evento por etapa (`lote`, `lectura`, `decision`, `fin`), y los
      datos de la decision **bajo `datos`**.

    Leer el diario como si fuera la forma plana no falla ruidosamente, y ese es
    el problema: la primera linea de cada factura es su `lectura` —que no trae
    `result`—, `setdefault` se queda con ella, y el listado sale entero a `null`
    (una fila por evento en vez de una por factura). Filtrar aqui deja el resto
    del modulo con un solo formato en la mano.

    Devuelve `None` para los eventos que no son una decision: eso es "no hay
    fila", que no es lo mismo que "fila vacia".
    """
    if "tipo" not in registro:
        return registro
    if registro.get("tipo") != TIPO_DECISION:
        return None
    datos = registro.get("datos")
    if not isinstance(datos, dict):
        return None
    # `file_id` va en el evento, fuera de `datos`; y `datos` lo repite dentro de
    # `campos`, que es donde lo busca el visor. Se copia al frente para que el
    # evento se lea igual que una linea plana.
    return {**datos, "file_id": _texto(registro.get("file_id")) or _texto(datos.get("file_id"))}


def resumir(registro: dict) -> dict:
    """Fila compacta para el listado del visor."""
    campos = registro.get("campos") or {}
    return {
        "file_id": registro.get("file_id"),
        "resultado": registro.get("result"),
        "motivo_principal": motivo_principal(registro),
        "proveedor": _texto(campos.get("proveedor")),
        "proveedor_id": _texto(campos.get("proveedor_id")),
        "nif": _texto(campos.get("nif_maestro")) or _texto(campos.get("nif_asiento")),
        "asiento": _texto(campos.get("asiento")),
        "pedido": _texto(campos.get("pedido")),
        "fecha": _texto(campos.get("fecha")),
        "total": _num(campos.get("total")),
        "importe_erp": _num(campos.get("importe_erp")),
        "desvio_importe": _num(campos.get("desvio_importe")),
        "estado_erp": _texto(campos.get("estado_erp")),
        # En que divisa viene el `total`. Casi siempre `"EUR"`, y entonces la
        # tabla no lo pinta: el aviso solo tiene que saltar cuando no lo es.
        "divisa": divisa_principal(campos),
        "lote": registro.get("lote"),
        "metodo_lectura": registro.get("metodo_lectura"),
        "escalon_lectura": registro.get("escalon_lectura"),
        "calidad_lectura": _num(registro.get("calidad_lectura")),
        "segundos_lectura": _num(registro.get("segundos_lectura")),
        "identificacion_fiable": registro.get("identificacion_fiable"),
        "version_norma": registro.get("version_norma"),
        "segunda_lectura": segunda_lectura_resumen(registro.get("segunda_lectura")),
    }


def detallar(registro: dict) -> dict:
    """Vista completa de una factura para el panel de detalle."""
    return {
        "file_id": registro.get("file_id"),
        "resultado": registro.get("result"),
        "motivo_principal": motivo_principal(registro),
        "motivos": registro.get("motivos") or [],
        "hechos": registro.get("hechos") or [],
        "campos": registro.get("campos") or {},
        "lectura": {
            "metodo_lectura": registro.get("metodo_lectura"),
            "escalon_lectura": registro.get("escalon_lectura"),
            "calidad_lectura": _num(registro.get("calidad_lectura")),
            "segundos_lectura": _num(registro.get("segundos_lectura")),
            "sospechosos": registro.get("sospechosos") or [],
            "sospechosos_meta": registro.get("sospechosos_meta") or [],
        },
        "lote": registro.get("lote"),
        "sha256": registro.get("sha256"),
        "identificacion_fiable": registro.get("identificacion_fiable"),
        "version_norma": registro.get("version_norma"),
        "nota_documento": registro.get("nota_documento"),
        "segunda_lectura": registro.get("segunda_lectura"),
        "resumen": resumir(registro),
    }


@dataclass(frozen=True)
class _Firma:
    """Identidad del fichero en disco, para saber si hay que recargar."""

    mtime_ns: int
    tamano: int

    @classmethod
    def de(cls, ruta: Path) -> "_Firma | None":
        try:
            st = ruta.stat()
        except OSError:
            return None
        return cls(st.st_mtime_ns, st.st_size)


def _sha256_fichero(ruta: Path) -> str | None:
    """sha256 del contenido del fichero, o None si no se puede leer."""
    try:
        digest = hashlib.sha256()
        with ruta.open("rb") as fichero:
            for bloque in iter(lambda: fichero.read(_BLOQUE_HASH), b""):
                digest.update(bloque)
    except OSError:
        return None
    return digest.hexdigest()


class TrazaStore:
    """Traza del motor en memoria, con recarga automatica si cambia el fichero.

    `ruta` es una traza o varias. El motor escribe **una traza por lote**
    (`outcomes_traza.jsonl` y `outcomes_lote2_traza.jsonl`), asi que la API
    recibe las dos y las sirve concatenadas: para el visor hay un solo listado.

    `cola` es la ruta opcional del sidecar de la cola de revision
    (`outcomes_cola.jsonl`). Si existe, su anotacion se engancha a cada registro
    al cargar, de forma que el listado y el detalle la ven sin saber de donde
    viene.
    """

    def __init__(self, ruta: Path | Sequence[Path], cola: Path | None = None) -> None:
        # `ruta` admite una sola traza (lote 1) o varias (lote 1 + lote 2). Con
        # varias se leen en el orden dado y se sirven como un unico listado, que
        # es lo que permite enseñar las 540 sin que el visor sepa de lotes.
        rutas = (ruta,) if isinstance(ruta, (str, Path)) else tuple(ruta)
        self.rutas: tuple[Path, ...] = tuple(Path(cada) for cada in rutas)
        # `ruta` sigue siendo la primera: quien ya la leia no se entera.
        self.ruta = self.rutas[0]
        self.cola = cola
        self._lock = threading.Lock()
        # `None` (y no `()`) para que la primera carga siempre ocurra, y para que
        # "ningun fichero existe" sea un estado estable que no relea en cada
        # consulta: `(None, None) == (None, None)` no vuelve a tocar el disco.
        self._firmas: tuple[_Firma | None, ...] | None = None
        self._firma_cola: _Firma | None = None
        self._hash: str | None = None
        self._hash_cola: str | None = None
        self._registros: list[dict] = []
        self._resumenes: list[dict] = []
        self._indice: dict[str, int] = {}
        self._lineas_invalidas = 0

    # ------------------------------------------------------------------ #
    # Carga
    # ------------------------------------------------------------------ #
    def cargar(self, forzar: bool = False) -> None:
        """Recarga las trazas si algun fichero cambio (o siempre, con `forzar`).

        Solo se leen los ficheros cuando su identidad en disco (`mtime`/tamano)
        cambia; el sha256 del contenido se calcula en esa misma recarga, no en
        cada consulta. Con varias trazas la recarga es conjunta: el listado es
        uno, asi que no tiene sentido recargar solo la mitad.
        """
        firmas = tuple(_Firma.de(cada) for cada in self.rutas)
        firma_cola = _Firma.de(self.cola) if self.cola is not None else None
        with self._lock:
            if (
                not forzar
                and self._firmas is not None
                and firmas == self._firmas
                and firma_cola == self._firma_cola
            ):
                return
            cola = carga_cola(self.cola) if self.cola is not None else {}
            registros: list[dict] = []
            resumenes: list[dict] = []
            indice: dict[str, int] = {}
            invalidas = 0
            digest_ficheros: list[str] = []
            hash_cola: str | None = None
            if firma_cola is not None and any(cada is not None for cada in firmas):
                hash_cola = _sha256_fichero(self.cola)
            for ruta, firma in zip(self.rutas, firmas):
                if firma is None:
                    continue
                # El sha256 de **cada** fichero entra en la firma combinada, y en
                # el orden de `self.rutas`: dos trazas distintas dan firmas
                # distintas, y reordenarlas tambien.
                digest_ficheros.append(_sha256_fichero(ruta) or "")
                with ruta.open("r", encoding="utf-8") as fichero:
                    for numero, linea in enumerate(fichero, start=1):
                        linea = linea.strip()
                        if not linea:
                            continue
                        try:
                            registro = json.loads(linea)
                        except json.JSONDecodeError:
                            invalidas += 1
                            logger.warning(
                                "Traza %s: linea %s ilegible, se ignora.", ruta.name, numero
                            )
                            continue
                        registro = aplanar(registro)
                        if registro is None:
                            # Evento del diario que no decide nada (`lote`,
                            # `lectura`, `fin`): describe la pasada, no la
                            # factura. No es una linea invalida y no se avisa
                            # por ella.
                            continue
                        file_id = _texto(registro.get("file_id"))
                        if not file_id:
                            invalidas += 1
                            logger.warning(
                                "Traza %s: linea %s sin file_id, se ignora.", ruta.name, numero
                            )
                            continue
                        anotacion = cola.get(file_id)
                        if anotacion is not None:
                            registro["segunda_lectura"] = anotacion
                        # `setdefault`: si un file_id se repite entre lotes gana
                        # el primero, que es el de la traza que va antes.
                        indice.setdefault(file_id, len(registros))
                        registros.append(registro)
                        resumenes.append(resumir(registro))
            hash_contenido = (
                hashlib.sha256("|".join(digest_ficheros).encode()).hexdigest()
                if digest_ficheros
                else None
            )
            self._firmas = firmas
            self._firma_cola = firma_cola
            self._hash = hash_contenido
            self._hash_cola = hash_cola
            self._registros = registros
            self._resumenes = resumenes
            self._indice = indice
            self._lineas_invalidas = invalidas
            logger.info(
                "Traza cargada: %s facturas%s (%s)",
                len(registros),
                f", {invalidas} lineas ignoradas" if invalidas else "",
                ", ".join(str(cada) for cada in self.rutas),
            )

    def _asegurar(self) -> None:
        self.cargar()

    # ------------------------------------------------------------------ #
    # Consultas
    # ------------------------------------------------------------------ #
    @property
    def disponible(self) -> bool:
        self._asegurar()
        return bool(self._registros)

    def total(self) -> int:
        self._asegurar()
        return len(self._registros)

    def lineas_invalidas(self) -> int:
        self._asegurar()
        return self._lineas_invalidas

    def firma(self) -> str:
        """Firma estable del contenido cargado (sha256 de lo que sirve el listado).

        Es la identidad de la traza para quien cachea: dos lecturas con la misma
        firma describen exactamente el mismo contenido, y la firma cambia cuando
        el motor reescribe el fichero. Sin traza legible devuelve
        `FIRMA_SIN_TRAZA`, que tambien es estable.

        La cola de revision entra en la firma porque el listado la expone: si el
        sidecar cambia, la respuesta cambia aunque la traza siga igual. Sin
        sidecar la firma es la de siempre (solo la traza), de modo que un
        despliegue sin cola no ve alterado su ETag.
        """
        self._asegurar()
        if not self._hash:
            return FIRMA_SIN_TRAZA
        if not self._hash_cola:
            return self._hash
        return hashlib.sha256(f"{self._hash}|{self._hash_cola}".encode()).hexdigest()

    def obtener(self, file_id: str) -> dict | None:
        self._asegurar()
        posicion = self._indice.get(file_id)
        return self._registros[posicion] if posicion is not None else None

    def contiene(self, file_id: str) -> bool:
        self._asegurar()
        return file_id in self._indice

    def buscar(
        self,
        *,
        resultado: str | None = None,
        lote: int | None = None,
        proveedor: str | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
        max_query_len: int = 64,
    ) -> tuple[list[dict], int]:
        """Listado filtrado y paginado. Devuelve (filas, total_de_coincidencias)."""
        self._asegurar()
        patron_q = compilar_busqueda(q, max_query_len)
        patron_proveedor = compilar_busqueda(proveedor, max_query_len)

        coincidencias = []
        for fila in self._resumenes:
            if resultado is not None and fila["resultado"] != resultado:
                continue
            if lote is not None and fila["lote"] != lote:
                continue
            if patron_proveedor is not None and not patron_proveedor.search(fila.get("proveedor") or ""):
                continue
            if patron_q is not None and not _coincide(fila, patron_q):
                continue
            coincidencias.append(fila)

        coincidencias.sort(key=lambda fila: (fila.get("file_id") or ""))
        return coincidencias[offset : offset + limit], len(coincidencias)

    def estadisticas(self) -> dict:
        self._asegurar()
        por_resultado = {nombre: 0 for nombre in RESULTADOS}
        por_lote: dict[str, int] = {}
        por_metodo: dict[str, int] = {}
        otros = 0
        cola = {"anotadas": 0, "confirmables": 0, "desvios": 0, "con_evidencia": 0}
        for fila in self._resumenes:
            resultado = fila.get("resultado")
            if resultado in por_resultado:
                por_resultado[resultado] += 1
            else:
                otros += 1
            lote = fila.get("lote")
            clave = str(lote) if lote is not None else "desconocido"
            por_lote[clave] = por_lote.get(clave, 0) + 1
            metodo = fila.get("metodo_lectura") or "desconocido"
            por_metodo[metodo] = por_metodo.get(metodo, 0) + 1
            anotacion = fila.get("segunda_lectura")
            if anotacion:
                cola["anotadas"] += 1
                if anotacion.get("confirmable"):
                    cola["confirmables"] += 1
                if anotacion.get("desvio"):
                    cola["desvios"] += 1
        cola["con_evidencia"] = cola["anotadas"] - cola["confirmables"] - cola["desvios"]
        return {
            "total": len(self._resumenes),
            "por_resultado": por_resultado,
            "resultados_desconocidos": otros,
            "por_lote": dict(sorted(por_lote.items())),
            "por_metodo_lectura": dict(sorted(por_metodo.items())),
            "cola_segunda_lectura": cola,
        }

    def file_ids(self) -> list[str]:
        self._asegurar()
        return [fila["file_id"] for fila in self._resumenes if fila.get("file_id")]

    def cobertura_entrega(self, ids_entrega: set[str]) -> dict[str, Any]:
        """Contrasta la entrega con la traza **lote a lote**.

        La traza cubre mas lotes que la entrega (el lote 2 del sabado se puntua
        aparte), asi que comparar los dos conjuntos enteros da `false` siempre:
        no mide un descuadre, mide que existen dos lotes. Se compara contra los
        lotes que la entrega si toca, y se publica el desglose para que el panel
        pueda decir cual esta entregado y cual no, en vez de lamentarse.
        """
        self._asegurar()
        por_lote: dict[str, set[str]] = {}
        en_traza: set[str] = set()
        for fila in self._resumenes:
            file_id = fila.get("file_id")
            if not file_id:
                continue
            en_traza.add(file_id)
            por_lote.setdefault(str(fila.get("lote") or "?"), set()).add(file_id)
        faltan = sorted(ids_entrega - en_traza)
        if ids_entrega:
            cubiertos = [ids for ids in por_lote.values() if ids & ids_entrega]
            esperado = set().union(*cubiertos) if cubiertos else set()
            coincide = not faltan and esperado == ids_entrega
        else:
            # Sin entrega no hay nada que contrastar: solo cuadra si tampoco hay
            # traza, porque una entrega vacia sobre una traza llena si es un fallo.
            coincide = not en_traza
        return {
            "coincide": coincide,
            "lotes": {
                lote: {
                    "traza": len(ids),
                    "entrega": len(ids & ids_entrega),
                    "entregado": ids <= ids_entrega,
                }
                for lote, ids in sorted(por_lote.items())
            },
            "faltan_en_traza": faltan,
        }

    def file_ids_por_resultado(self, resultado: str) -> list[str]:
        self._asegurar()
        return [fila["file_id"] for fila in self._resumenes if fila.get("resultado") == resultado]

    def versiones_norma(self) -> dict[str, int]:
        self._asegurar()
        versiones: dict[str, int] = {}
        for fila in self._resumenes:
            clave = fila.get("version_norma") or "desconocida"
            versiones[clave] = versiones.get(clave, 0) + 1
        return dict(sorted(versiones.items()))


def _coincide(fila: dict, patron: re.Pattern[str]) -> bool:
    return any(patron.search(str(fila.get(campo) or "")) for campo in CAMPOS_BUSQUEDA)


def compilar_busqueda(texto: str | None, max_len: int) -> re.Pattern[str] | None:
    """Compila una busqueda literal y segura.

    `re.escape` neutraliza metacaracteres (nada de ReDoS por `(a+)+` ni de
    inyeccion de patrones) y el texto se recorta a `max_len` caracteres para
    acotar el coste. La longitud excesiva se rechaza antes de llegar aqui.
    """
    if texto is None:
        return None
    limpio = texto.strip()
    if not limpio:
        return None
    return re.compile(re.escape(limpio[:max_len]), re.IGNORECASE)


class EntregaStore:
    """`outcomes.jsonl` (la entrega): solo se usa para contrastar totales."""

    def __init__(self, ruta: Path) -> None:
        self.ruta = ruta
        self._lock = threading.Lock()
        self._firma: _Firma | None = None
        self._resultados: dict[str, str] = {}
        self._invalidas = 0

    def cargar(self) -> None:
        firma = _Firma.de(self.ruta)
        with self._lock:
            if firma == self._firma:
                return
            resultados: dict[str, str] = {}
            invalidas = 0
            if firma is not None:
                with self.ruta.open("r", encoding="utf-8") as fichero:
                    for linea in fichero:
                        linea = linea.strip()
                        if not linea:
                            continue
                        try:
                            dato = json.loads(linea)
                        except json.JSONDecodeError:
                            invalidas += 1
                            continue
                        file_id = _texto(dato.get("file_id"))
                        if file_id:
                            resultados[file_id] = str(dato.get("result"))
                        else:
                            invalidas += 1
            self._firma = firma
            self._resultados = resultados
            self._invalidas = invalidas

    def resultados(self) -> dict[str, str]:
        self.cargar()
        return dict(self._resultados)

    def total(self) -> int:
        self.cargar()
        return len(self._resultados)

    def lineas_invalidas(self) -> int:
        self.cargar()
        return self._invalidas


def resumir_muchos(registros: Iterable[dict]) -> list[dict]:
    return [resumir(registro) for registro in registros]
