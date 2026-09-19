"""Lectura del Excel caotico: solo las hojas que son verdad.

``FINAL_v7_DEFINITIVO_ahorasi.xlsx`` tiene 14 hojas y 10 son ruido deliberado
(``NO_TOCAR``, ``backup_marzo``, ``MACROS_ROTAS``, ``v6_deprecated``...). Aqui
las hojas se leen por lista blanca explicita: si un dato no viene de una de
ellas, no existe para el motor de decision.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from openpyxl import load_workbook

from .normaliza import a_decimal, a_fecha, norm_iban, norm_nif, norm_nombre, norm_pedido

HOJA_PROVEEDORES = "Proveedores"
HOJA_PEDIDOS = "Pedidos_2026"
HOJA_NORMA = "Norma_Pagos_v3"
HOJA_PENDIENTES = "pendiente_revisar"

# Ruido declarado: se documenta para que la decision de ignorarlo sea auditable.
HOJAS_RUIDO = (
    "NO_TOCAR", "backup_marzo", "Pedidos_2025_OLD", "MACROS_ROTAS",
    "v6_deprecated", "tablas_dinamicas", "Hoja1", "Hoja1 (2)",
    "notas_alberto", "Sheet3",
)


@dataclass(frozen=True)
class Proveedor:
    id: str
    razon_social: str
    nif: str
    iban: str
    ciudad: str
    condiciones: str


@dataclass(frozen=True)
class Pedido:
    pedido: str
    proveedor_id: str
    nif: str
    importe: Decimal | None
    estado: str
    fecha: str


@dataclass
class Maestro:
    """Las tres hojas utiles, ya normalizadas, mas el sha256 del libro."""

    proveedores: dict[str, Proveedor]
    por_nif: dict[str, list[Proveedor]]
    por_iban: dict[str, list[Proveedor]]
    pedidos: dict[str, Pedido]
    norma: list[str]
    pendientes_revisar: list[str]
    duplicados: list[str]
    sha256: str
    hoja_estado: dict[str, str]

    def proveedor_de_pedido(self, pedido: str) -> Proveedor | None:
        p = self.pedidos.get(pedido)
        if p is None:
            return None
        return self.proveedores.get(p.proveedor_id)

    def vocabulario_pedidos(self) -> list[str]:
        return list(self.pedidos)

    def vocabulario_nifs(self) -> list[str]:
        return sorted({p.nif for p in self.proveedores.values() if p.nif})

    def vocabulario_ibans(self) -> list[str]:
        return sorted({p.iban for p in self.proveedores.values() if p.iban})


def _celdas(fila) -> list:
    return [c.value for c in fila]


def _cabecera(fila) -> list[str]:
    return [str(c or "").strip().lower() for c in _celdas(fila)]


def _indice(cabecera: list[str], *alias: str) -> int | None:
    for i, nombre in enumerate(cabecera):
        limpio = re.sub(r"[^a-z0-9]", "", nombre)
        for objetivo in alias:
            if limpio == re.sub(r"[^a-z0-9]", "", objetivo.lower()):
                return i
    return None


def carga(ruta: Path) -> Maestro:
    """Lee el libro entero y devuelve el maestro normalizado y deduplicado."""
    sha = hashlib.sha256(ruta.read_bytes()).hexdigest()
    libro = load_workbook(ruta, data_only=True, read_only=True)

    proveedores: dict[str, Proveedor] = {}
    duplicados: list[str] = []
    hoja_estado: dict[str, str] = {}
    for nombre in libro.sheetnames:
        hoja_estado[nombre] = "ruido_declarado" if nombre in HOJAS_RUIDO else "util"

    if HOJA_PROVEEDORES in libro.sheetnames:
        hoja = libro[HOJA_PROVEEDORES]
        filas = hoja.iter_rows(values_only=True)
        cabecera = [str(c or "").strip().lower() for c in next(filas)]
        i_id = _indice(cabecera, "id", "proveedorid")
        i_razon = _indice(cabecera, "razon social", "razonsocial")
        i_nif = _indice(cabecera, "nif", "cif")
        i_iban = _indice(cabecera, "iban")
        i_ciudad = _indice(cabecera, "ciudad")
        i_cond = _indice(cabecera, "condiciones")
        for fila in filas:
            if not fila or i_id is None or not fila[i_id]:
                continue
            pid = str(fila[i_id]).strip().upper()
            prov = Proveedor(
                id=pid,
                razon_social=str(fila[i_razon] or "").strip() if i_razon is not None else "",
                nif=norm_nif(fila[i_nif]) if i_nif is not None else "",
                iban=norm_iban(fila[i_iban]) if i_iban is not None else "",
                ciudad=str(fila[i_ciudad] or "").strip() if i_ciudad is not None else "",
                condiciones=str(fila[i_cond] or "").strip() if i_cond is not None else "",
            )
            anterior = proveedores.get(pid)
            if anterior is not None:
                # Fila repetida: se conserva la primera y se deja constancia.
                if anterior == prov:
                    duplicados.append(f"{HOJA_PROVEEDORES}!{pid} (fila duplicada identica)")
                else:
                    duplicados.append(
                        f"{HOJA_PROVEEDORES}!{pid} (fila duplicada CON CONFLICTO: "
                        f"iban {anterior.iban} vs {prov.iban})"
                    )
                continue
            proveedores[pid] = prov

    pedidos: dict[str, Pedido] = {}
    if HOJA_PEDIDOS in libro.sheetnames:
        hoja = libro[HOJA_PEDIDOS]
        filas = hoja.iter_rows(values_only=True)
        cabecera = [str(c or "").strip().lower() for c in next(filas)]
        i_ped = _indice(cabecera, "pedido")
        i_prov = _indice(cabecera, "proveedorid", "proveedor")
        i_nif = _indice(cabecera, "nif")
        i_imp = _indice(cabecera, "importe_total", "importe")
        i_est = _indice(cabecera, "estado")
        i_fec = _indice(cabecera, "fecha_pedido", "fecha")
        for fila in filas:
            if not fila or i_ped is None or not fila[i_ped]:
                continue
            clave = norm_pedido(fila[i_ped])
            if not clave:
                continue
            fecha = a_fecha(fila[i_fec]) if i_fec is not None else None
            pedidos[clave] = Pedido(
                pedido=clave,
                proveedor_id=str(fila[i_prov] or "").strip().upper() if i_prov is not None else "",
                nif=norm_nif(fila[i_nif]) if i_nif is not None else "",
                importe=a_decimal(fila[i_imp]) if i_imp is not None else None,
                estado=str(fila[i_est] or "").strip().upper() if i_est is not None else "",
                fecha=fecha.isoformat() if fecha else "",
            )

    norma: list[str] = []
    if HOJA_NORMA in libro.sheetnames:
        for fila in libro[HOJA_NORMA].iter_rows(values_only=True):
            texto = str(fila[0]).strip() if fila and fila[0] else ""
            if texto:
                norma.append(texto)

    pendientes: list[str] = []
    if HOJA_PENDIENTES in libro.sheetnames:
        for fila in libro[HOJA_PENDIENTES].iter_rows(values_only=True):
            for celda in fila or ():
                if not celda:
                    continue
                for m in re.finditer(r"PO[\s\-]?\d{4}[\s\-]?\d{1,4}", str(celda)):
                    pendientes.append(norm_pedido(m.group(0)))

    libro.close()

    por_nif: dict[str, list[Proveedor]] = {}
    por_iban: dict[str, list[Proveedor]] = {}
    for prov in proveedores.values():
        if prov.nif:
            por_nif.setdefault(prov.nif, []).append(prov)
        if prov.iban:
            por_iban.setdefault(prov.iban, []).append(prov)

    return Maestro(
        proveedores=proveedores, por_nif=por_nif, por_iban=por_iban,
        pedidos=pedidos, norma=norma, pendientes_revisar=sorted(set(pendientes)),
        duplicados=duplicados, sha256=sha, hoja_estado=hoja_estado,
    )


def proveedor_por_nombre(maestro: Maestro, texto: str) -> Proveedor | None:
    """Resuelve el emisor por nombre cuando el NIF no es legible."""
    objetivo = norm_nombre(texto)
    if not objetivo:
        return None
    for prov in maestro.proveedores.values():
        base = norm_nombre(prov.razon_social)
        if base and (base in objetivo or objetivo in base):
            return prov
    return None
