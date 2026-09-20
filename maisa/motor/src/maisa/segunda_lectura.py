"""La segunda lectura de la nube: **evidencia para la cola**, nunca una decision.

Este modulo nace de una medicion, no de una intuicion. Sobre las 29 facturas
del corpus que no traen capa de texto (de 500; las otras 471 nunca llegan al
OCR) y contra el Excel maestro como test independiente:

* El motor **local** decide 29/29 contra el oro y 25/29 contra el maestro.
  Nunca paga donde el oro escala. Su error tipico es confundir un digito del
  pedido (`2026` -> `2028`), y ese pedido no existe: la norma lo detecta y
  **escala**. Error *detectable*.
* La **nube** decide 14/29. Falla tres veces en direccion peligrosa (paga donde
  el oro escala) y, sobre todo, pierde el pedido en 5 de 29 o **se lo inventa
  con formato valido** (`PO-2020-0001`). Error *indetectable*.
* El **hibrido** (local decide, nube solo rellena lo que el local no resuelve)
  decide 29/29 contra el maestro: recupera los 4 casos en que el local no leia
  un identificador y la nube si, sin tocar ninguno de los 4 desvios reales.

Por eso la nube no decide. Lo que si puede hacer, sin ningun riesgo de pago, es
**aportar la evidencia** de que una escalada era por un identificador ilegible:
el local no consiguio leerlo, la segunda lectura lo trae, el maestro lo confirma
y la escalada desaparece. Eso es un recorte de la cola de revision humana, no
una autorizacion de pago. La decision la sigue tomando `norma.Decisor` con la
lectura local.

Las cuatro condiciones son conjuntas, no una sola. Que la segunda lectura aporte
un campo no basta: `fax_2026_0411` escala por cinco motivos, la nube le da el
pedido y el documento **sigue escalando** por el importe. Marcar eso como
«confirmable» seria un recorte falso, y lo detecta el propio motor de reglas en
vez de una heuristica sobre los motivos.

La regla no es «lo que el local no cuadra con el maestro», que confundiria dos
cosas muy distintas. Es esta, campo a campo:

- **Pedido y NIF** no mueven dinero: un valor que no resuelve se trata como
  error de OCR, y ahi si se admite la segunda lectura (que debe resolver contra
  el maestro: es lo que bloquea el pedido inventado con formato valido, el modo
  de fallo propio de la nube). El pedido usa `norma.match_estricto`, que repara
  confusiones de OCR y **nunca** convierte un digito en otro digito.
- **IBAN**: es el **destino del pago**. Si el local leyo uno, aunque sea
  distinto del maestro, **no se tapa nunca**: ese es justo el hecho que dispara
  «posible desvio de pago» en la norma, y sustituirlo por el IBAN correcto
  borraria la unica senal de fraude que tenemos. Solo se admite el de la nube si
  el local **no leyo ninguno**, y solo si es el IBAN del proveedor del pedido ya
  resuelto (no vale cualquier IBAN del maestro).
- El **importe** (base, IVA, total) **no se toca nunca**: la nube no aporta la
  decision, y el importe es lo que mueve el pago.

Lo que **no** esta medido, y por eso nada de aqui decide: la tasa de falsos
positivos de la nube en identificadores *coherentes pero falsos*. La guarda
exige que el valor exista en el maestro y que el IBAN sea el del proveedor del
pedido, pero una alucinacion que caiga en un IBAN valido del maestro no la
detecta nadie. Con n=4 no se puede afirmar que sea raro.
"""

from __future__ import annotations

import copy
from typing import Any

from . import norma, texto

#: Campos que la segunda lectura puede aportar. El importe no esta, a proposito.
IDENTIFICADORES = ("pedido", "nif", "iban")


def vocabulario(maestro) -> dict[str, Any]:
    """Lo que el maestro admite como valido, por campo.

    Se deriva del maestro y no se recibe de fuera para que el banco de pruebas y
    la cola de revision no puedan mirar universos distintos.
    """
    return {
        "nif": set(maestro.por_nif),
        "iban": set(maestro.vocabulario_ibans()),
        "pedido": maestro.vocabulario_pedidos(),
    }


def identificador_ok(campo: str, valores: list[str], vocab: dict) -> bool:
    """¿Algun candidato resuelve contra el maestro?

    El NIF y el IBAN se dan por leidos solo con coincidencia **exacta**. El
    pedido admite ademas la reparacion de confusiones de OCR que ya hace la
    norma (`match_estricto`), que nunca convierte un digito en otro digito.
    """
    if campo == "pedido":
        return any(norma.match_estricto(v, vocab[campo]) for v in valores)
    return any(v in vocab[campo] for v in valores)


def pedido_resuelto(lectura: texto.Lectura, vocab: dict) -> str | None:
    """El pedido de la lectura, ya resuelto contra el maestro (o ``None``)."""
    for valor in lectura.valores("pedido"):
        m = norma.match_estricto(valor, vocab["pedido"])
        if m:
            return m[0]
    return None


def esperado_de(lectura: texto.Lectura, maestro, vocab: dict) -> tuple[str | None, str | None]:
    """``(pedido, iban_esperado)`` segun el proveedor del pedido resuelto."""
    pedido = pedido_resuelto(lectura, vocab)
    if not pedido:
        return None, None
    proveedor = maestro.proveedor_de_pedido(pedido)
    return pedido, (proveedor.iban if proveedor else None)


def desvio_de_pago(lectura: texto.Lectura, maestro, vocab: dict) -> bool:
    """¿La lectura trae un IBAN que no es el del proveedor de su pedido?

    Es el hecho que dispara «posible desvio de pago» en la norma. Si el pedido
    no se resuelve no se puede afirmar, asi que devuelve ``False``: aqui el
    silencio va en la direccion de no inventar una alarma.
    """
    leidos = [v for v in lectura.valores("iban") if v]
    if not leidos:
        return False
    _, esperado = esperado_de(lectura, maestro, vocab)
    return bool(esperado) and esperado not in leidos


def rellena_identificadores(
    local: texto.Lectura,
    nube: texto.Lectura | None,
    maestro,
    vocab: dict | None = None,
) -> tuple[texto.Lectura, list[str]]:
    """La lectura local con los identificadores que la nube si resuelve.

    Devuelve la lectura hibrida y la lista de sustituciones hechas. **No se usa
    para decidir**: la decision de la entrega sale de la lectura local. Existe
    para medir el brazo hibrido y para calcular la evidencia de la cola.

    El importe nunca se toca y el IBAN no se tapa nunca si el local leyo uno.
    """
    vocab = vocab if vocab is not None else vocabulario(maestro)
    hibrida = copy.deepcopy(local)
    if nube is None:
        return hibrida, []

    cambios: list[str] = []

    for campo in ("pedido", "nif"):
        if identificador_ok(campo, local.valores(campo), vocab):
            continue  # el local lo lee: la nube no tiene nada que decir aqui
        buenos = [c for c in getattr(nube, campo)
                  if identificador_ok(campo, [c.valor], vocab)]
        if not buenos:
            continue  # la nube tampoco lo trae, o lo trae inventado
        setattr(hibrida, campo, buenos)
        cambios.append(f"{campo}<-nube:{','.join(c.valor for c in buenos)}")

    if not local.valores("iban"):
        _, esperado = esperado_de(hibrida, maestro, vocab)
        buenos = [c for c in nube.iban if esperado and c.valor == esperado]
        if buenos:
            hibrida.iban = buenos
            cambios.append(f"iban<-nube:{buenos[0].valor}")

    return hibrida, cambios


def evidencia(
    local: texto.Lectura,
    nube: texto.Lectura | None,
    maestro,
    vocab: dict | None = None,
    resultado_hibrido: str | None = None,
) -> dict | None:
    """Que aporta la segunda lectura sobre una factura escalada.

    Es material de **cola de revision**, no de entrega: la decision de la
    entrega no cambia.

    `resultado_hibrido` es lo que decidiria la norma con la lectura hibrida (ver
    :func:`rellena_identificadores`). Es lo que separa «la segunda lectura
    aporta un campo» de «la segunda lectura **explica** la escalada», y la
    diferencia no es teorica: `fax_2026_0411` escala por cinco motivos, la nube
    le da el pedido y el documento **sigue escalando** por el importe. Marcarlo
    confirmable seria un recorte falso, con la evidencia en contra delante del
    revisor. Sin `resultado_hibrido` una escalada nunca se marca confirmable.

    Devuelve ``None`` cuando no hay nada que decir (la segunda lectura no aporta
    nada, o no hay segunda lectura), y si no un dict con:

    - ``confirmable``: la escalada era por un identificador ilegible y la
      segunda lectura la resuelve: el revisor puede cerrarla sin abrirla.
    - ``desvio``: la lectura trae un IBAN que no es el del proveedor de su
      pedido. **Nunca confirmable**: es la senal de fraude, y el recorte de cola
      no puede taparla.
    - ``campos``: lo que aporto la segunda lectura, campo a campo.
    - ``motivos``: por que se marca asi, en texto para el revisor.
    """
    vocab = vocab if vocab is not None else vocabulario(maestro)
    if nube is None:
        return None

    hibrida, cambios = rellena_identificadores(local, nube, maestro, vocab)

    if desvio_de_pago(hibrida, maestro, vocab):
        pedido, _ = esperado_de(hibrida, maestro, vocab)
        return {
            "confirmable": False,
            "desvio": True,
            "campos": {},
            "motivos": [
                f"el documento trae un IBAN que no es el de {pedido or 'su proveedor'}: "
                "posible desvio de pago, la revision humana no se puede recortar"
            ],
        }

    if not cambios:
        return None

    # Solo lo que la segunda lectura **anadio**: comparar contra la local es lo
    # que hace que `campos` sea la evidencia y no un volcado de la nube.
    aportados: dict[str, list[str]] = {}
    for campo in IDENTIFICADORES:
        nuevos = [v for v in hibrida.valores(campo) if v not in local.valores(campo)]
        if nuevos:
            aportados[campo] = nuevos

    if resultado_hibrido is None or resultado_hibrido == "ESCALAR":
        # Con `None` no se ha demostrado que la escalada desaparezca, y no
        # demostrarlo es motivo suficiente para no recortar la cola.
        return {
            "confirmable": False,
            "desvio": False,
            "campos": aportados,
            "motivos": [
                "la segunda lectura aporta "
                + ", ".join(f"{c}={', '.join(v)}" for c, v in aportados.items())
                + " pero la escalada no desaparece con ella: "
                "la revision humana sigue haciendo falta"
            ],
        }

    motivos = [f"el motor local no resolvio {c} contra el maestro" for c in aportados]
    motivos.append(
        "la segunda lectura lo aporta, el maestro lo confirma y la escalada "
        "desaparece: " + ", ".join(cambios)
    )
    return {
        "confirmable": True,
        "desvio": False,
        "campos": aportados,
        "motivos": motivos,
    }
