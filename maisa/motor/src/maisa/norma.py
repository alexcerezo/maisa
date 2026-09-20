"""Motor de conciliacion y decision.

Contrato: la extraccion propone, la norma decide. Aqui no hay modelo de
lenguaje ni heuristica opaca; hay hechos verificables contra el ERP y el
maestro, y una politica explicita que convierte esos hechos en un resultado.

Las reglas son R1..R6 de la Norma v3 (ver ``config/reglas.toml``). El orden y
la precedencia entre resultados estan declarados como datos.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .erp import Asiento
from .excel import Maestro, Proveedor
from .normaliza import (
    a_fecha,
    candidatos_importe,
    cuantiza,
    match_estricto,
    match_seguro,
)
from .texto import Lectura, extrae_iva_pct

# Pedido ya normalizado por norm_pedido: PO-<anio>-<cuerpo>.
_RE_PEDIDO = re.compile(r"^PO-(\d{3,4})-(\d{4})$")

PAGAR = "PAGAR"
NO_PAGAR = "NO_PAGAR"
ESCALAR = "ESCALAR"
RESULTADOS = (PAGAR, NO_PAGAR, ESCALAR)


class ConfigInvalida(ValueError):
    """El TOML no tiene la forma de la norma: no se puede decidir con el.

    Existe para que un fichero de reglas ajeno (el del motor Rust legado, o uno
    con una clave mal escrita) pare el motor en vez de degradarlo. La version
    silenciosa de esto no fallaba: pagaba las 500 facturas.
    """


def euros(valor: object) -> str:
    """Formatea un importe con dos decimales fijos.

    Los motivos y las notas acaban en la traza (nunca en el entregable, que solo
    lleva `file_id` y `result`), asi que su texto tiene que ser estable: sin
    esto, ``Decimal('2795.10')`` y el float ``2795.1`` producen cadenas
    distintas y el hash de la traza dependeria del formato numerico del
    snapshot de entrada.
    """
    if valor is None:
        return "?"
    return f"{Decimal(str(valor)):.2f}"


@dataclass
class Hecho:
    """Un hecho verificado, con la evidencia que lo sostiene."""

    regla: str
    ok: bool
    motivo: str
    datos: dict = field(default_factory=dict)
    duro: bool = False
    # Evidencia registrada que no participa en el resultado (una instruccion
    # inyectada en el documento se muestra, no se obedece ni se penaliza).
    informativo: bool = False
    # Clave del hecho en la politica. La regla dice *donde* se comprobo
    # (R5_estado, R6_anomalia); el nombre dice *que* se comprobo
    # (pago_duplicado, si_pedido_repetido). Sin esta separacion la politica de
    # hechos duros nunca se encontraba y el sistema escalaba los pagos ya
    # realizados en vez de negarlos. En hechos no duros el nombre busca la
    # sub-clave de ``[reglas.<regla>]`` y cae a ``si_falla`` si no existe.
    nombre: str = ""

    def como_dict(self) -> dict:
        return {
            "regla": self.regla, "ok": self.ok, "motivo": self.motivo,
            "datos": self.datos, "duro": self.duro, "nombre": self.nombre,
            "informativo": self.informativo,
        }


@dataclass
class Decision:
    """Resultado para una factura, con su traza de hechos."""

    file_id: str
    resultado: str
    motivos: list[str]
    hechos: list[Hecho]
    campos: dict
    identificacion_fiable: bool
    version_norma: str
    metodo_lectura: str

    def como_dict(self) -> dict:
        return {
            "file_id": self.file_id, "result": self.resultado,
            "motivos": self.motivos, "campos": self.campos,
            "identificacion_fiable": self.identificacion_fiable,
            "version_norma": self.version_norma,
            "metodo_lectura": self.metodo_lectura,
            "hechos": [h.como_dict() for h in self.hechos],
        }


@dataclass
class Politica:
    """La norma cargada desde TOML, ya tipada."""

    version: str
    fuente: str
    tolerancia: Decimal
    similitud_nif: float
    similitud_iban: float
    confianza_minima: float
    calidad_texto_minima: float
    hoy: str
    precedencia: dict[str, int]
    reglas: dict[str, dict]
    hechos_duros: dict[str, str]

    # Secciones y claves que declara la norma v3. Un TOML al que le falten no es
    # "la norma con valores por defecto": es OTRO esquema. El `config/reglas.toml`
    # del motor Rust legado usa claves planas y ninguna de estas secciones; al
    # cargarlo, `[precedencia]` y `[reglas]` quedaban vacios y **toda** factura
    # caia al resultado por defecto: 500 PAGAR, `validacion: OK` y exit 0. Un motor
    # de pagos que no encuentra su norma se para; no decide.
    SECCIONES = ("umbrales", "precedencia", "reglas", "hechos_duros")
    CLAVES = ("version", "fuente", "descripcion", "umbrales", "precedencia",
              "reglas", "hechos_duros")

    @staticmethod
    def carga(ruta: Path) -> "Politica":
        datos = tomllib.loads(ruta.read_text(encoding="utf-8"))
        faltan = [s for s in Politica.SECCIONES if s not in datos]
        if faltan:
            raise ConfigInvalida(
                f"{ruta}: faltan las secciones {faltan}. Claves de primer nivel "
                f"encontradas: {sorted(datos)}.\n"
                "  Esquema esperado: version, [umbrales], [precedencia], "
                "[reglas.*], [hechos_duros].\n"
                "  Si venias del motor Rust, `config/reglas.toml` (raiz de maisa) "
                "es el legado; la norma viva es `motor/config/reglas.toml`."
            )
        sobran = sorted(set(datos) - set(Politica.CLAVES))
        if sobran:
            raise ConfigInvalida(
                f"{ruta}: claves de primer nivel desconocidas: {sobran}. Se "
                f"admiten {list(Politica.CLAVES)}. Una clave mal escrita no se "
                "ignora: cambia la decision, asi que se rechaza en vez de adivinar."
            )
        version = datos["version"]
        if not isinstance(version, str) or not version.strip():
            raise ConfigInvalida(
                f"{ruta}: `version` debe ser texto no vacio (vino {version!r}). Es "
                "el valor que sella cada decision: sin el, la traza no dice con que "
                "norma se pago."
            )
        if not isinstance(datos["precedencia"], dict) or not datos["precedencia"]:
            raise ConfigInvalida(
                f"{ruta}: [precedencia] debe ser una tabla no vacia con "
                f"{list(RESULTADOS)}. Sin precedencia no hay forma de resolver dos "
                "reglas que disparan a la vez."
            )
        faltan_prec = [r for r in RESULTADOS if r not in datos["precedencia"]]
        if faltan_prec:
            raise ConfigInvalida(
                f"{ruta}: [precedencia] no declara {faltan_prec}. Se exigen los "
                f"tres resultados ({list(RESULTADOS)}) para que el orden sea total."
            )
        if not isinstance(datos["reglas"], dict) or not datos["reglas"]:
            raise ConfigInvalida(
                f"{ruta}: [reglas] debe ser una tabla con al menos una regla. Con "
                "cero reglas toda factura cae al resultado por defecto: el motor "
                "pagaria sin comprobar nada."
            )
        if not isinstance(datos["hechos_duros"], dict):
            raise ConfigInvalida(f"{ruta}: [hechos_duros] debe ser una tabla.")
        u = datos["umbrales"]
        return Politica(
            version=version,
            fuente=datos.get("fuente", ""),
            tolerancia=Decimal(str(u.get("tolerancia_importe", "0.01"))),
            similitud_nif=float(u.get("similitud_minima_nif", 0.85)),
            similitud_iban=float(u.get("similitud_minima_iban", 0.90)),
            confianza_minima=float(u.get("confianza_minima_campo", 0.70)),
            calidad_texto_minima=float(u.get("calidad_texto_minima", 0.60)),
            hoy=str(u.get("hoy", "")),
            precedencia={k: int(v) for k, v in datos["precedencia"].items()},
            reglas=datos["reglas"],
            hechos_duros=datos["hechos_duros"],
        )

    def politica_de(self, regla: str, clave: str, por_defecto: str = ESCALAR) -> str:
        return self.reglas.get(regla, {}).get(clave, por_defecto)

    def resultado_de_hecho_duro(self, nombre: str) -> str:
        return self.hechos_duros.get(nombre, ESCALAR)

    def mas_grave(self, a: str, b: str) -> str:
        """Devuelve el resultado de mayor precedencia (mas a la derecha en TOML)."""
        return a if self.precedencia.get(a, 0) >= self.precedencia.get(b, 0) else b


class Decisor:
    """Concilia una lectura contra el maestro y el ERP, y decide."""

    def __init__(
        self, maestro: Maestro, asientos: dict[str, Asiento], politica: Politica
    ) -> None:
        self.maestro = maestro
        self.asientos = asientos
        self.pol = politica
        self._vocab_pedidos = maestro.vocabulario_pedidos() or list(asientos)
        self._vocab_nifs = maestro.vocabulario_nifs()
        self._vocab_ibans = maestro.vocabulario_ibans()
        # Indice (NIF, importe) -> pedido, solo para combinaciones unicas. Es la
        # via de rescate cuando el OCR destroza el numero de pedido: el importe
        # y el proveedor identifican el pedido aunque el codigo sea ilegible.
        cuenta: dict[tuple[str, str], list[str]] = {}
        for ped in self._vocab_pedidos:
            a = asientos.get(ped)
            if a is not None and a.nif:
                cuenta.setdefault((a.nif, str(a.importe)), []).append(ped)
        self._por_nif_importe = {k: v[0] for k, v in cuenta.items() if len(v) == 1}
        # Indice cuerpo de pedido -> pedido, solo si el cuerpo es unico (lo es:
        # los 516 asientos son PO-2026-#### con cuerpo distinto). Permite
        # reparar un anio comido por el OCR sin adivinar nada.
        cuerpos: dict[str, list[str]] = {}
        for ped in self._vocab_pedidos:
            cuerpos.setdefault(ped.rsplit("-", 1)[-1], []).append(ped)
        self._por_cuerpo = {k: v[0] for k, v in cuerpos.items() if len(v) == 1}
        # Pedidos que el lote presenta mas de una vez. La Norma prohibe pagar
        # dos veces (punto 5), pero un pedido en dos facturas del lote no es el
        # "ya PAGADA" del ERP: es una duplicidad que un humano debe resolver
        # (punto 6), asi que se escala el lote entero. Lo declara `procesa`,
        # que es quien ve el lote completo: un decisor solo ve un documento.
        self._pedidos_repetidos: dict[str, list[str]] = {}

    def marca_pedido_repetido(self, pedido: str, *ficheros: str) -> None:
        """Declara que ``pedido`` aparece en los documentos ``ficheros`` del lote."""
        if not pedido:
            return
        vistos = self._pedidos_repetidos.setdefault(pedido, [])
        for file_id in ficheros:
            if file_id and file_id not in vistos:
                vistos.append(file_id)

    def pedidos_repetidos(self) -> list[str]:
        return sorted(self._pedidos_repetidos)

    @staticmethod
    def _es_ocr(lectura: Lectura) -> bool:
        return lectura.metodo.startswith("vision")

    def _repara_anio(self, candidato: str) -> str | None:
        """Repara el anio de un pedido leido por OCR (``PO-206-0724``).

        Solo se aplica a lecturas de vision y solo si el cuerpo de 4 digitos
        identifica **un** pedido del ERP. La capa de texto es exacta: si ahi
        aparece un anio que no existe, es un pedido inexistente de verdad y no
        se toca.
        """
        m = _RE_PEDIDO.match(candidato)
        if not m:
            return None
        return self._por_cuerpo.get(m.group(2))

    # ---------------------------------------------------------------- pedido
    def _pedido_estructural(self, lectura: Lectura) -> tuple[str, str] | None:
        """Rescata el pedido por (NIF del maestro, importe) cuando el codigo es
        ilegible. Devuelve ``(pedido, nota)`` o ``None``.

        Solo acepta combinaciones unicas en el ERP, y exige que el NIF ya este
        en el maestro: sin esas dos condiciones seria una adivinanza.
        """
        nifs = [v for v in lectura.valores("nif") if v in self.maestro.por_nif]
        if not nifs:
            return None
        for nif in nifs:
            for campo in ("total", "base"):
                for valor in lectura.valores(campo):
                    ped = self._por_nif_importe.get((nif, valor))
                    if ped:
                        return (ped, f"pedido ilegible recuperado por estructura "
                                     f"(NIF {nif} + {campo} {valor})")
        return None

    def _resuelve_pedido(self, lectura: Lectura) -> tuple[list[str], list[str], bool]:
        """Devuelve (pedidos resueltos, notas de correccion, match exacto).

        ``exacto`` es True solo si algun candidato coincidio literalmente con un
        pedido del ERP. Un rescate estructural o una reparacion OCR dejan el
        pedido identificado, pero no habilitan un hecho duro.
        """
        notas: list[str] = []
        resueltos: list[str] = []
        exacto = False
        for candidato in lectura.valores("pedido"):
            m = match_estricto(candidato, self._vocab_pedidos)
            if m:
                if m[1] == "exacto":
                    exacto = True
                else:
                    notas.append(f"pedido {candidato} reparado a {m[0]} ({m[1]})")
                if m[0] not in resueltos:
                    resueltos.append(m[0])
            else:
                # No se corrige a lo difuso: PO-2026-9999 y PO-2026-0099 se
                # parecen en 0,83 y son pedidos distintos. Un pedido inventado
                # debe seguir sin existir.
                reparado = self._repara_anio(candidato) if self._es_ocr(lectura) else None
                if reparado and reparado not in resueltos:
                    notas.append(
                        f"pedido {candidato} reparado a {reparado} "
                        f"(anio ilegible en el escaneo; el cuerpo identifica el pedido)"
                    )
                    resueltos.append(reparado)
                    continue
                notas.append(f"pedido {candidato} no existe en el ERP")
        if not resueltos:
            rescate = self._pedido_estructural(lectura)
            if rescate:
                notas.append(rescate[1])
                resueltos.append(rescate[0])
        return resueltos, notas, exacto

    def pedido_de(self, lectura: Lectura) -> str | None:
        """El pedido que identifica la factura, si es uno solo e inequivoco.

        Lo usa el proceso de lote para detectar duplicidades. Si la factura cita
        dos pedidos distintos no se puede afirmar que repita ninguno.
        """
        pedidos, _, _ = self._resuelve_pedido(lectura)
        return pedidos[0] if len(pedidos) == 1 else None

    # ------------------------------------------------------------------ nif
    def _resuelve_nif(
        self, lectura: Lectura, proveedor: Proveedor | None, asiento: Asiento
    ) -> tuple[set[str], list[str]]:
        notas: list[str] = []
        resueltos: set[str] = set()
        for candidato in lectura.valores("nif"):
            m = match_seguro(candidato, self._vocab_nifs, self.pol.similitud_nif)
            if m:
                if m[1] != "exacto":
                    notas.append(f"NIF {candidato} corregido a {m[0]} ({m[1]})")
                resueltos.add(m[0])
            else:
                notas.append(f"NIF {candidato} no figura en el maestro")
                resueltos.add(candidato)
        return resueltos, notas

    def _resuelve_iban(self, lectura: Lectura) -> tuple[set[str], list[str]]:
        notas: list[str] = []
        resueltos: set[str] = set()
        for candidato in lectura.valores("iban"):
            m = match_seguro(candidato, self._vocab_ibans, self.pol.similitud_iban)
            if m:
                if m[1] != "exacto":
                    notas.append(f"IBAN {candidato} corregido a {m[0]} ({m[1]})")
                resueltos.add(m[0])
            else:
                notas.append(f"IBAN {candidato} no figura en el maestro")
                resueltos.add(candidato)
        return resueltos, notas

    # ---------------------------------------------------------------- importes
    @staticmethod
    def _mejor_decimal(valores: list[str]) -> Decimal | None:
        for v in valores:
            try:
                return Decimal(v)
            except Exception:
                continue
        return None

    @staticmethod
    def _digitos(valor: Decimal | str) -> str:
        return re.sub(r"\D", "", str(valor))

    def _repara_importes_ocr(
        self,
        lectura: Lectura,
        base: Decimal | None,
        iva: Decimal | None,
        total: Decimal | None,
        asiento: Asiento,
    ) -> tuple[Decimal | None, Decimal | None, Decimal | None, list[str]]:
        """Reconstruye el importe de un escaneo anclandolo en el ERP.

        El OCR desalinea el separador decimal (``1.240.84``, ``52498``,
        ``1113.2080``) pero **no inventa digitos**. En lugar de adivinar se
        acepta el importe solo si alguna reconstruccion con esos mismos digitos
        cuadra con el importe del pedido en el ERP, bien por el total impreso,
        bien por la aritmetica del propio documento (base + IVA, Norma punto 3).
        Un descuadre real (``2.385,80`` frente a ``2.395,80``) tiene digitos
        distintos: ningun candidato coincide y no se repara nunca.
        """
        notas: list[str] = []
        esperado = cuantiza(asiento.importe)
        cerca = lambda v: abs(cuantiza(v) - esperado) <= self.pol.tolerancia  # noqa: E731

        cand_base = candidatos_importe(base) if base is not None else []
        cand_iva = candidatos_importe(iva) if iva is not None else []
        cand_total = candidatos_importe(total) if total is not None else []

        por_total = any(cerca(t) for t in cand_total)
        par: tuple[Decimal, Decimal] | None = None
        for b in cand_base:
            for i in cand_iva:
                if cerca(b + i):
                    par = (b, i)
                    break
            if par:
                break

        if not por_total and par is None:
            return base, iva, total, notas

        base_r, iva_r = par if par else (base, iva)
        if por_total:
            # Solo es una reparacion si el importe cambio. `por_total` tambien es
            # cierto cuando el OCR leyo el total bien -- basta con que cuadre con
            # el ERP --, y en ese caso no hay nada que reconstruir: la nota salia
            # igual, afirmando un separador decimal desalineado y repitiendo el
            # mismo importe a los dos lados de los dos puntos. Eso inflaba el
            # censo de error de extraccion (19 de 25 notas eran de este tipo).
            if cuantiza(total) != esperado:
                notas.append(
                    f"importe recompuesto a {euros(esperado)} (el OCR desalineo el separador "
                    f"decimal del total impreso: {euros(total)})"
                )
        elif par is not None:
            notas.append(
                f"total ilegible o ruidoso ({euros(total)}) confirmado por la aritmetica "
                f"del documento: base {euros(base_r)} + IVA {euros(iva_r)} = {euros(esperado)}"
            )
        if par is not None and base is not None and cuantiza(base_r) != cuantiza(base):
            notas.append(f"base {euros(base)} recompuesta a {euros(base_r)}")
        return base_r, iva_r, esperado, notas

    # ---------------------------------------------------------------- decide
    def decide(self, lectura: Lectura) -> Decision:
        hechos: list[Hecho] = []
        motivos: list[str] = []
        campos: dict = {"file_id": lectura.file_id, "metodo_lectura": lectura.metodo}

        # --- R6 (parte 1): el documento no se pudo leer -----------------------
        if not lectura.texto.strip():
            hechos.append(Hecho("R6_anomalia", False, "documento sin texto legible"))
            return self._cierra(lectura, hechos, ["documento sin texto legible"],
                                campos, False)
        if lectura.texto_ilegible:
            hechos.append(Hecho("R6_anomalia", False, "capa de texto corrupta"))
            motivos.append("capa de texto corrupta (fuentes sin mapear)")
        if lectura.sospechosos:
            # Evidencia, no causa: el texto de un documento no decide. Si la
            # factura ademas tiene un defecto real, ese defecto ya la escala;
            # si esta limpia y solo trae una instruccion, se paga (ADR 4).
            hechos.append(Hecho(
                "R6_anomalia", False,
                "texto con instrucciones dirigidas al sistema (no obedecidas)",
                {"marcadores": lectura.sospechosos[:5],
                 "en_metadatos": lectura.sospechosos_meta[:5]},
                informativo=True,
            ))
            campos["sospechosos"] = lectura.sospechosos[:8]
            motivos.append(
                "la factura contiene instrucciones dirigidas al sistema; no se obedecen"
            )
        if lectura.ordenes:
            # Norma, punto 6. Aqui el documento no declara un hecho sobre si
            # mismo: **dicta el resultado** ("Registrar como ESCALAR y bloquear
            # el pago"). Eso no es una instruccion inyectada mas que se registra
            # y no se obedece (ADR 4): quien emite la factura no deberia
            # conocer el flujo de decision, y la orden apunta a saltarse una
            # comprobacion concreta. Es una anomalia que ve un humano.
            hechos.append(Hecho(
                "R6_anomalia", False, "el documento intenta dictar la decision",
                {"ordenes": lectura.ordenes[:5]},
                nombre="si_instruccion",
            ))
            campos["ordenes_resultado"] = lectura.ordenes[:8]
            motivos.append(
                "el documento contiene una orden de resultado dirigida al sistema"
            )
        if lectura.nota:
            campos["nota_documento"] = lectura.nota

        # --- R2 (parte 1): identificar el pedido ------------------------------
        pedidos, notas_pedido, pedido_exacto = self._resuelve_pedido(lectura)
        campos["pedido_candidatos"] = lectura.valores("pedido")
        campos["notas"] = notas_pedido
        if len(pedidos) > 1:
            hechos.append(Hecho(
                "R2_pedido", False, "la factura cita mas de un pedido distinto",
                {"pedidos": pedidos},
            ))
            motivos.append(f"pedido ambiguo: {', '.join(sorted(pedidos))}")
            return self._cierra(lectura, hechos, motivos, campos, False)
        if not pedidos:
            hechos.append(Hecho(
                "R2_pedido", False, "no se pudo identificar el pedido",
                {"candidatos": lectura.valores("pedido")},
            ))
            return self._cierra(
                lectura, hechos, motivos + ["pedido no identificable"], campos, False
            )
        pedido = pedidos[0]
        # A partir de aqui sabemos de que pedido hablamos: es lo unico que
        # legitima un hecho duro. El resto de anomalias se acumulan por
        # precedencia en _cierra() sin necesidad de tocar este indicador.
        fiable = pedido_exacto
        campos["pedido"] = pedido
        asiento = self.asientos.get(pedido)
        if asiento is None:
            hechos.append(Hecho(
                "R2_pedido", False, "el pedido no existe en el ERP", {"pedido": pedido}
            ))
            return self._cierra(
                lectura, hechos, motivos + [f"{pedido} no existe en el ERP"], campos, False
            )
        campos["asiento"] = asiento.id
        campos["proveedor_id"] = asiento.proveedor
        campos["estado_erp"] = asiento.estado
        campos["importe_erp"] = str(asiento.importe)

        proveedor = self.maestro.proveedor_de_pedido(pedido)
        if proveedor is None:
            # El pedido existe en el ERP pero no en el maestro: dato roto.
            hechos.append(Hecho(
                "R2_pedido", False, "el pedido no figura en el maestro de Excel",
                {"pedido": pedido},
            ))
            return self._cierra(
                lectura, hechos, motivos + [f"{pedido} ausente del maestro"], campos, False
            )
        campos["proveedor"] = proveedor.razon_social

        # --- importes (antes que la identidad) --------------------------------
        # El importe se resuelve primero porque es lo que permite heredar del
        # ERP un NIF o un IBAN que el escaneo ha dejado ilegible: si el pedido
        # es exacto y el importe esta confirmado, sabemos de que factura
        # hablamos aunque un campo no se lea.
        base = self._mejor_decimal(lectura.valores("base"))
        iva = self._mejor_decimal(lectura.valores("iva"))
        total = self._mejor_decimal(lectura.valores("total"))
        notas_importe: list[str] = []
        if self._es_ocr(lectura):
            base, iva, total, notas_importe = self._repara_importes_ocr(
                lectura, base, iva, total, asiento
            )
        importe_confirmado = (
            total is not None
            and abs(cuantiza(total) - cuantiza(asiento.importe)) <= self.pol.tolerancia
        )
        ocr_confirmada = bool(
            self._es_ocr(lectura) and pedido_exacto and importe_confirmado
        )
        campos["notas_importe"] = notas_importe

        # --- R1: identidad del emisor y cuenta de abono -----------------------
        nifs, notas_nif = self._resuelve_nif(lectura, proveedor, asiento)
        ibans, notas_iban = self._resuelve_iban(lectura)
        campos["nif_candidatos"] = sorted(nifs)
        campos["iban_candidatos"] = sorted(ibans)
        campos["nif_maestro"] = proveedor.nif
        campos["iban_maestro"] = proveedor.iban
        campos["nif_asiento"] = asiento.nif
        # Las notas de R1 se suman a las del pedido. Se calculaban y se tiraban,
        # asi que corregir un NIF o un IBAN mal leidos era invisible: ni el
        # informe de la traza ni el censo de error de extraccion podian contar
        # esas reparaciones. `campos` es diagnostico: no entra en la decision ni
        # en la entrega, solo en la traza.
        campos["notas"] += notas_nif + notas_iban

        # --- R6: el escaneo no da para comprobar la cuenta de abono -----------
        # Punto 1 de la Norma: el IBAN de la factura debe coincidir con el del
        # maestro. Si el documento llego por vision y **no se lee ni un IBAN**,
        # esa comprobacion no se puede hacer: la herencia del ERP de mas abajo
        # rellena el hueco, pero rellenarlo no es comprobarlo. Se escala para
        # que un humano mire el papel. No se usa el umbral de calidad de la capa
        # de texto porque un escaneo no tiene capa de texto: todos los
        # documentos de vision puntuan 0,0 y el umbral escalaria los 29.
        #
        # La norma v3.1 acota la anomalia al IBAN: "escaneo del que no se lee el
        # IBAN de abono" (docs/albertitos_plan.md), y el banco de oro congelado
        # la respeta. Se probo a exigir ademas una de las dos anclas de identidad
        # (NIF o fecha) y movia `scan_021.pdf` de PAGAR a ESCALAR: tiene el IBAN
        # legible y el NIF y la fecha ilegibles, y el oraculo lo da por PAGAR.
        # Endurecer la regla pide regenerar el banco y tocar la norma; no vale
        # colarlo de paso.
        fechas_leidas = lectura.valores("fecha")
        if self._es_ocr(lectura) and not lectura.iban:
            hechos.append(Hecho(
                "R6_anomalia", False, "documento no legible",
                {"metodo": lectura.metodo, "iban_candidatos": len(lectura.iban),
                 "nif_candidatos": len(nifs),
                 "fecha_candidatos": len(fechas_leidas)},
                nombre="si_documento_no_legible",
            ))
            motivos.append("documento no legible: el escaneo no permite leer el IBAN de abono")

        if not nifs:
            if ocr_confirmada:
                hechos.append(Hecho(
                    "R1_identidad", True,
                    "NIF ilegible en el escaneo; identidad heredada del pedido del ERP",
                    {"nif": proveedor.nif},
                ))
                campos["identidad_heredada"] = True
            else:
                hechos.append(Hecho("R1_identidad", False, "no se pudo leer el NIF del emisor"))
                motivos.append("NIF del emisor no legible")
        elif proveedor.nif and proveedor.nif in nifs:
            hechos.append(Hecho(
                "R1_identidad", True, "NIF del emisor coincide con el maestro",
                {"nif": proveedor.nif},
            ))
        elif not proveedor.nif:
            # El maestro no tiene NIF para este proveedor: hueco de datos.
            hechos.append(Hecho(
                "R1_identidad", False, "el maestro no tiene NIF para este proveedor",
                {"nif_leido": sorted(nifs)},
            ))
            motivos.append("el maestro no tiene NIF para este proveedor")
        elif nifs & set(self.maestro.por_nif):
            hechos.append(Hecho(
                "R1_identidad", False,
                "el NIF de la factura es de OTRO proveedor del maestro",
                {"nif_leido": sorted(nifs & set(self.maestro.por_nif)),
                 "nif_esperado": proveedor.nif},
            ))
            motivos.append(
                "el NIF de la factura pertenece a otro proveedor del maestro"
            )
        else:
            hechos.append(Hecho(
                "R1_identidad", False, "el NIF del emisor no esta en el maestro",
                {"nif_leido": sorted(nifs), "nif_esperado": proveedor.nif},
                informativo=ocr_confirmada,
            ))
            if not ocr_confirmada:
                motivos.append("NIF del emisor desconocido en el maestro")

        if not ibans:
            if ocr_confirmada:
                hechos.append(Hecho(
                    "R1_identidad", True,
                    "IBAN ilegible en el escaneo; cuenta heredada del maestro del proveedor",
                    {"iban": proveedor.iban},
                ))
                campos["identidad_heredada"] = True
            else:
                hechos.append(Hecho("R1_identidad", False, "no se pudo leer el IBAN"))
                motivos.append("IBAN no legible")
        elif proveedor.iban and proveedor.iban in ibans:
            hechos.append(Hecho(
                "R1_identidad", True, "IBAN de la factura coincide con el maestro",
                {"iban": proveedor.iban},
            ))
        elif not proveedor.iban:
            hechos.append(Hecho(
                "R1_identidad", False, "el maestro no tiene IBAN para este proveedor",
                {"iban_leido": sorted(ibans)},
            ))
            motivos.append("el maestro no tiene IBAN para este proveedor")
        else:
            hechos.append(Hecho(
                "R1_identidad", False,
                "el IBAN de la factura NO coincide con el del maestro",
                {"iban_leido": sorted(ibans), "iban_esperado": proveedor.iban},
            ))
            motivos.append(
                "IBAN de abono distinto del maestro: posible desvio de pago"
            )

        # --- R2 (parte 2) y R3: importes --------------------------------------
        # `base`, `iva` y `total` vienen ya resueltos del bloque de importes de
        # arriba (que es donde el OCR se repara): aqui solo se publican.
        campos["base"] = str(base) if base is not None else None
        campos["iva"] = str(iva) if iva is not None else None
        campos["total"] = str(total) if total is not None else None
        campos["iva_pct"] = extrae_iva_pct(lectura.texto)

        if total is None:
            hechos.append(Hecho("R2_pedido", False, "no se pudo leer el total de la factura"))
            motivos.append("total de factura no legible")
        else:
            desvio = abs(cuantiza(total) - cuantiza(asiento.importe))
            campos["desvio_importe"] = str(desvio)
            if desvio <= self.pol.tolerancia:
                hechos.append(Hecho(
                    "R2_pedido", True,
                    "el total de la factura cuadra con el importe del pedido",
                    {"total": str(total), "pedido": str(asiento.importe)},
                ))
            else:
                hechos.append(Hecho(
                    "R2_pedido", False,
                    "el total de la factura NO cuadra con el importe del pedido",
                    {"total": str(total), "pedido": str(asiento.importe),
                     "desvio": str(desvio)},
                ))
                motivos.append(
                    f"importe descuadrado: factura {euros(total)} vs pedido "
                    f"{euros(asiento.importe)}"
                )

        if base is None or iva is None or total is None:
            hechos.append(Hecho(
                "R3_iva", False, "no se pudieron leer base, IVA y total a la vez",
                {"base": campos["base"], "iva": campos["iva"], "total": campos["total"]},
                informativo=importe_confirmado and self._es_ocr(lectura),
            ))
            if total is not None:
                motivos.append("base o IVA no legibles")
        else:
            suma = cuantiza(base) + cuantiza(iva)
            if abs(suma - cuantiza(total)) <= self.pol.tolerancia:
                hechos.append(Hecho(
                    "R3_iva", True, "total = base + IVA",
                    {"base": str(base), "iva": str(iva), "total": str(total)},
                ))
            else:
                hechos.append(Hecho(
                    "R3_iva", False, "total != base + IVA",
                    {"base": str(base), "iva": str(iva), "total": str(total),
                     "suma": str(suma)},
                ))
                motivos.append(
                    f"aritmetica incoherente: {euros(base)} + {euros(iva)} != {euros(total)}"
                )

        # --- R4: fecha --------------------------------------------------------
        fechas = lectura.valores("fecha")
        campos["fecha_candidatos"] = fechas
        if not fechas:
            hechos.append(Hecho(
                "R4_fecha", False, "fecha no legible",
                informativo=ocr_confirmada,
            ))
            if not ocr_confirmada:
                motivos.append("fecha no legible")
        else:
            # Se prefiere la primera fecha que exista en el calendario; si
            # ninguna existe, se conserva la primera con forma de fecha para
            # poder motivar "impresa pero inexistente" en vez de "no legible".
            fecha = None
            cruda = fechas[0]
            for cand in fechas:
                fecha = a_fecha(cand)
                if fecha is not None:
                    cruda = cand
                    break
            hoy = a_fecha(self.pol.hoy)
            campos["fecha"] = fecha.isoformat() if fecha else cruda
            if fecha is None:
                hechos.append(Hecho(
                    "R4_fecha", False, "la fecha impresa no existe en el calendario",
                    {"fecha_leida": cruda, "candidatos": fechas},
                ))
                motivos.append(f"fecha invalida: {cruda}")
            elif hoy is not None and fecha > hoy:
                hechos.append(Hecho(
                    "R4_fecha", False, "la fecha de la factura es futura",
                    {"fecha": fecha.isoformat(), "hoy": self.pol.hoy},
                ))
                motivos.append(f"fecha futura: {fecha.isoformat()}")
            else:
                hechos.append(Hecho(
                    "R4_fecha", True, "fecha valida y no futura",
                    {"fecha": fecha.isoformat()},
                ))

        # --- R5: estado del pedido en el ERP ----------------------------------
        if asiento.estado == "PAGADA":
            hechos.append(Hecho(
                "R5_estado", False, "el pedido ya figura PAGADA en el ERP",
                {"pedido": pedido, "asiento": asiento.id, "estado": asiento.estado},
                duro=True, nombre="pago_duplicado",
            ))
            motivos.append(f"{pedido} ya esta PAGADA en el ERP: no se paga dos veces")
        elif asiento.estado == "PENDIENTE":
            hechos.append(Hecho(
                "R5_estado", True, "el pedido esta PENDIENTE en el ERP",
                {"estado": asiento.estado},
            ))
        else:
            hechos.append(Hecho(
                "R5_estado", False, "estado del pedido desconocido en el ERP",
                {"estado": asiento.estado},
            ))
            motivos.append(f"estado ERP no reconocido: {asiento.estado}")

        # --- R6: duplicidad dentro del lote -----------------------------------
        # El mismo pedido en dos facturas del lote: pagar las dos seria pagar
        # dos veces (Norma, punto 5) y pagar solo una obliga a elegir cual. Es
        # un caso de humano, y afecta a las DOS facturas, no solo a la segunda:
        # no hay forma de saber cual es la buena sin mirarlas.
        if pedido in self._pedidos_repetidos:
            otros = [f for f in self._pedidos_repetidos[pedido] if f != lectura.file_id]
            hechos.append(Hecho(
                "R6_anomalia", False, "pedido repetido en el lote",
                {"pedido": pedido, "asiento": asiento.id, "otros_documentos": otros},
                nombre="si_pedido_repetido",
            ))
            motivos.append(
                f"el pedido {pedido} aparece en mas de una factura del lote"
                + (f": tambien en {', '.join(otros)}" if otros else "")
            )

        # --- R6: el pedido esta marcado como pendiente de revision ------------
        # Hoja `pendiente_revisar` del maestro: alguien ya levanto la mano sobre
        # este pedido y la factura no debe pagarse hasta que se resuelva.
        if pedido in self.maestro.pendientes_revisar:
            hechos.append(Hecho(
                "R6_anomalia", False, "el pedido esta pendiente de revision",
                {"pedido": pedido, "hoja": "pendiente_revisar"},
                nombre="si_pendiente_revision",
            ))
            motivos.append(
                f"{pedido} figura como pendiente de revision en el maestro"
            )

        return self._cierra(lectura, hechos, motivos, campos, fiable)

    # ----------------------------------------------------------------- cierre
    def _cierra(
        self, lectura: Lectura, hechos: list[Hecho], motivos: list[str],
        campos: dict, fiable: bool,
    ) -> Decision:
        """Aplica la precedencia declarada para producir el resultado final.

        ``fiable`` no es "la factura es correcta" sino "sabemos de que pedido
        hablamos". Solo entonces un hecho duro (un pago ya realizado) autoriza
        un NO_PAGAR; si la identificacion es dudosa, se escala a un humano.
        """
        resultado = PAGAR
        for hecho in hechos:
            if hecho.ok or hecho.informativo:
                continue
            if hecho.duro:
                # Un hecho duro solo manda si sabemos de que pedido hablamos.
                if fiable:
                    resultado = self.pol.mas_grave(
                        resultado, self.pol.resultado_de_hecho_duro(hecho.nombre or hecho.regla)
                    )
                else:
                    resultado = self.pol.mas_grave(resultado, ESCALAR)
                continue
            por_defecto = self.pol.politica_de(hecho.regla, "si_falla", ESCALAR)
            resultado = self.pol.mas_grave(
                resultado,
                self.pol.politica_de(
                    hecho.regla, hecho.nombre or "si_falla", por_defecto
                ),
            )
        if not motivos and resultado != PAGAR:
            motivos = ["anomalia no clasificada"]
        return Decision(
            file_id=lectura.file_id, resultado=resultado, motivos=motivos,
            hechos=hechos, campos=campos, identificacion_fiable=fiable,
            version_norma=self.pol.version, metodo_lectura=lectura.metodo,
        )


def carga_politica(ruta: Path) -> Politica:
    return Politica.carga(ruta)
