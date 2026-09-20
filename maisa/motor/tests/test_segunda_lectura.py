"""Tests de `maisa.segunda_lectura`: la guarda asimetrica de la segunda lectura.

Todo sintetico, sin red ni corpus: lo que se prueba aqui es la regla, no el
motor. Las dos propiedades que no pueden fallar son las que separan este modulo
de un «la nube rellena lo que falta»:

* el IBAN que el local **si** leyo no se tapa nunca (es la senal de fraude);
* el importe no sale nunca de la nube (es lo que mueve el pago).
"""
from __future__ import annotations

import pytest

from maisa import segunda_lectura as S, texto


# ------------------------------------------------------------------ dobles
class Proveedor:
    def __init__(self, nif: str, iban: str) -> None:
        self.nif = nif
        self.iban = iban


class Maestro:
    """Lo minimo que consulta `segunda_lectura`, con dos proveedores."""

    def __init__(self) -> None:
        self.proveedores = {
            "P1": Proveedor("B98120774", "ES4414650100951704302211"),
            "P2": Proveedor("A46311208", "ES8201280011230100044571"),
        }
        self.pedidos = {"PO-2026-0717": "P1", "PO-2026-0724": "P2"}
        self.por_nif = {p.nif: pid for pid, p in self.proveedores.items()}

    def vocabulario_pedidos(self) -> list[str]:
        return list(self.pedidos)

    def vocabulario_ibans(self) -> list[str]:
        return [p.iban for p in self.proveedores.values()]

    def proveedor_de_pedido(self, pedido: str):
        return self.proveedores.get(self.pedidos.get(pedido))


def leida(**campos) -> texto.Lectura:
    """Una `Lectura` con los campos que se le pasen (valores sueltos)."""
    kwargs = {
        campo: [texto.Candidato(v, "test", 1.0) for v in valores]
        for campo, valores in campos.items()
    }
    return texto.Lectura(file_id="f.pdf", paginas=1, metodo="vision_ocr", **kwargs)


@pytest.fixture
def maestro() -> Maestro:
    return Maestro()


# --------------------------------------------------- rellena_identificadores
def test_rellena_el_pedido_ilegible_con_el_de_la_nube(maestro):
    local = leida(pedido=["PO-2028-0717"])  # anio comido por el OCR
    nube = leida(pedido=["PO-2026-0717"])
    hibrida, cambios = S.rellena_identificadores(local, nube, maestro)
    assert hibrida.valores("pedido") == ["PO-2026-0717"]
    assert cambios == ["pedido<-nube:PO-2026-0717"]


def test_no_tapa_el_iban_que_el_local_si_leyo(maestro):
    """La senal de desvio de pago: si el local leyo un IBAN, la nube no lo toca."""
    local = leida(pedido=["PO-2026-0717"], iban=["ES0000000000000000000000"])
    nube = leida(pedido=["PO-2026-0717"], iban=["ES4414650100951704302211"])
    hibrida, cambios = S.rellena_identificadores(local, nube, maestro)
    assert hibrida.valores("iban") == ["ES0000000000000000000000"]
    assert not any(c.startswith("iban<-") for c in cambios)


def test_admite_el_iban_de_la_nube_solo_si_el_local_no_leyo_ninguno(maestro):
    local = leida(pedido=["PO-2026-0717"])
    nube = leida(pedido=["PO-2026-0717"], iban=["ES4414650100951704302211"])
    hibrida, cambios = S.rellena_identificadores(local, nube, maestro)
    assert hibrida.valores("iban") == ["ES4414650100951704302211"]
    assert cambios == ["iban<-nube:ES4414650100951704302211"]


def test_rechaza_un_iban_valido_que_no_es_el_del_proveedor_del_pedido(maestro):
    """Un IBAN del maestro que no es el del proveedor no vale: no es su cuenta."""
    local = leida(pedido=["PO-2026-0717"])
    nube = leida(iban=["ES8201280011230100044571"])  # es de P2, no de P1
    hibrida, cambios = S.rellena_identificadores(local, nube, maestro)
    assert hibrida.valores("iban") == []
    assert cambios == []


def test_rechaza_un_pedido_inventado_con_formato_valido(maestro):
    """`PO-2020-0001` no esta en el maestro: es el modo de fallo propio de la nube."""
    local = leida()
    nube = leida(pedido=["PO-2020-0001"], nif=["B98120774"])
    hibrida, cambios = S.rellena_identificadores(local, nube, maestro)
    assert hibrida.valores("pedido") == []
    assert cambios == ["nif<-nube:B98120774"]


def test_el_importe_no_sale_nunca_de_la_nube(maestro):
    local = leida(pedido=["PO-2026-0717"], total=["1984.40"])
    nube = leida(pedido=["PO-2026-0717"], total=["1.56"])
    hibrida, _ = S.rellena_identificadores(local, nube, maestro)
    assert hibrida.valores("total") == ["1984.40"]


def test_no_toca_la_lectura_local(maestro):
    """El hibrido es una copia: la decision de la entrega usa la lectura local."""
    local = leida(pedido=["PO-2028-0717"])
    nube = leida(pedido=["PO-2026-0717"])
    S.rellena_identificadores(local, nube, maestro)
    assert local.valores("pedido") == ["PO-2028-0717"]


def test_sin_segunda_lectura_devuelve_la_local_tal_cual(maestro):
    local = leida(pedido=["PO-2028-0717"])
    hibrida, cambios = S.rellena_identificadores(local, None, maestro)
    assert hibrida.valores("pedido") == ["PO-2028-0717"]
    assert cambios == []


# --------------------------------------------------------------- evidencia
def test_sin_segunda_lectura_no_hay_evidencia(maestro):
    assert S.evidencia(leida(pedido=["PO-2028-0717"]), None, maestro) is None


def test_sin_cambios_no_hay_evidencia(maestro):
    """El local lee todo: no hay nada que decir, y no se inventa una entrada."""
    local = leida(pedido=["PO-2026-0717"], nif=["B98120774"])
    nube = leida(pedido=["PO-2026-0717"], nif=["B98120774"])
    assert S.evidencia(local, nube, maestro, resultado_hibrido="PAGAR") is None


def test_confirmable_cuando_la_escalada_desaparece(maestro):
    local = leida(pedido=["PO-2028-0717"])
    nube = leida(pedido=["PO-2026-0717"])
    ev = S.evidencia(local, nube, maestro, resultado_hibrido="PAGAR")
    assert ev["confirmable"] is True
    assert ev["desvio"] is False
    assert ev["campos"] == {"pedido": ["PO-2026-0717"]}


def test_no_confirmable_cuando_la_segunda_lectura_aporta_pero_sigue_escalando(maestro):
    """`fax_2026_0411`: la nube da el pedido, pero el documento sigue escalando."""
    local = leida()
    nube = leida(pedido=["PO-2026-0717"])
    ev = S.evidencia(local, nube, maestro, resultado_hibrido="ESCALAR")
    assert ev["confirmable"] is False
    assert ev["desvio"] is False
    assert ev["campos"] == {"pedido": ["PO-2026-0717"]}


def test_sin_resultado_hibrido_nunca_se_marca_confirmable(maestro):
    local = leida(pedido=["PO-2028-0717"])
    nube = leida(pedido=["PO-2026-0717"])
    ev = S.evidencia(local, nube, maestro)
    assert ev["confirmable"] is False


def test_desvio_no_es_confirmable_aunque_la_nube_rellene(maestro):
    """La senal de fraude manda: aunque la nube aporte el NIF, no se recorta."""
    local = leida(iban=["ES0000000000000000000000"])
    nube = leida(nif=["B98120774"], pedido=["PO-2026-0717"])
    ev = S.evidencia(local, nube, maestro, resultado_hibrido="PAGAR")
    assert ev["confirmable"] is False
    assert ev["desvio"] is True
    assert ev["campos"] == {}
    assert "desvio de pago" in ev["motivos"][0]


# ----------------------------------------------------------- desvio_de_pago
def test_desvio_falso_si_el_iban_leido_es_el_del_proveedor(maestro):
    local = leida(pedido=["PO-2026-0717"], iban=["ES4414650100951704302211"])
    assert S.desvio_de_pago(local, maestro, S.vocabulario(maestro)) is False


def test_desvio_falso_si_no_se_leyo_ningun_iban(maestro):
    local = leida(pedido=["PO-2026-0717"])
    assert S.desvio_de_pago(local, maestro, S.vocabulario(maestro)) is False


def test_desvio_falso_si_el_pedido_no_se_resuelve(maestro):
    """Sin pedido no se puede afirmar: aqui el silencio es no inventar alarma."""
    local = leida(pedido=["PO-2028-0717"], iban=["ES0000000000000000000000"])
    assert S.desvio_de_pago(local, maestro, S.vocabulario(maestro)) is False


# -------------------------------------------------------------- vocabulario
def test_el_vocabulario_sale_del_maestro(maestro):
    vocab = S.vocabulario(maestro)
    assert vocab["pedido"] == ["PO-2026-0717", "PO-2026-0724"]
    assert vocab["nif"] == {"B98120774", "A46311208"}
    assert len(vocab["iban"]) == 2
