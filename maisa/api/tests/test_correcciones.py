"""Correcciones: datos que un operador completa a mano sobre una factura.

La invariante que se comprueba una y otra vez en este fichero es la misma que
justifica que la coleccion exista: **corregir un dato no cambia la decision del
motor**. El operador anota al lado; `resultado` sigue siendo el de la traza.
"""

from __future__ import annotations

import pytest

from app.mongo_repo import CAMPOS_CORREGIBLES

# ESCALAR y `vision_ocr`: es el caso real, el motor no leyo ni NIF ni IBAN.
ESCALADA = "2026-02-01_P002.pdf"
PAGADA = "2026-02-03_P004.pdf"


def _campos(respuesta) -> dict[str, dict]:
    return {campo["campo"]: campo for campo in respuesta.json()["campos"]}


def _poner(cliente, file_id, campos, **extra):
    return cliente.put(f"/api/facturas/{file_id}/correcciones", json={"campos": campos, **extra})


# --------------------------------------------------------------------------- #
# Lectura
# --------------------------------------------------------------------------- #
def test_sin_correcciones_devuelve_lista_vacia(cliente_con_fakes):
    respuesta = cliente_con_fakes.get(f"/api/facturas/{ESCALADA}/correcciones")
    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert datos["file_id"] == ESCALADA
    assert datos["campos"] == []
    assert datos["actualizado_en"] is None


def test_correcciones_factura_inexistente_da_404(cliente_con_fakes):
    respuesta = cliente_con_fakes.get("/api/facturas/2026-12-31_P999.pdf/correcciones")
    assert respuesta.status_code == 404
    assert respuesta.json()["error"]["codigo"] == "factura_no_encontrada"


def test_valor_motor_es_none_cuando_el_motor_no_leyo_el_campo(cliente_con_fakes):
    """La gracia del endpoint: contrastar lo que hay en el PDF con lo que dijo el motor."""
    _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": "B90233418"}})
    campo = _campos(cliente_con_fakes.get(f"/api/facturas/{ESCALADA}/correcciones"))["nif"]
    assert campo["valor"] == "B90233418"
    assert campo["valor_motor"] is None


def test_valor_motor_trae_lo_que_si_leyo(cliente_con_fakes):
    _poner(cliente_con_fakes, ESCALADA, {"fecha": {"valor": "2026-02-28"}})
    campo = _campos(cliente_con_fakes.get(f"/api/facturas/{ESCALADA}/correcciones"))["fecha"]
    assert campo["valor"] == "2026-02-28"
    assert campo["valor_motor"] == "2026-02-31"


def test_valor_motor_no_se_guarda_en_mongo(cliente_con_fakes, fake_mongo):
    """Se deriva de la traza: guardarlo seria una copia que se queda obsoleta."""
    _poner(cliente_con_fakes, ESCALADA, {"total": {"valor": "1512.50"}})
    guardado = fake_mongo.correcciones[ESCALADA]["campos"]["total"]
    assert "valor_motor" not in guardado
    assert set(guardado) == {"valor", "nota", "autor", "actualizado_en"}


# --------------------------------------------------------------------------- #
# Escritura
# --------------------------------------------------------------------------- #
def test_guardar_un_campo(cliente_con_fakes):
    respuesta = _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": "B90233418"}})
    assert respuesta.status_code == 200
    campo = _campos(respuesta)["nif"]
    assert campo["valor"] == "B90233418"
    assert campo["nota"] is None
    assert campo["actualizado_en"] is not None


def test_guardar_varios_campos_de_una_vez(cliente_con_fakes):
    respuesta = _poner(
        cliente_con_fakes,
        ESCALADA,
        {"nif": {"valor": "B90233418"}, "iban": {"valor": "ES2100491500051234567890"}},
    )
    assert set(_campos(respuesta)) == {"nif", "iban"}


def test_guardar_es_una_fusion_no_un_reemplazo(cliente_con_fakes):
    """Corregir el IBAN despues no puede borrar el NIF ya corregido."""
    _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": "B90233418"}})
    respuesta = _poner(cliente_con_fakes, ESCALADA, {"iban": {"valor": "ES2100"}})
    assert set(_campos(respuesta)) == {"nif", "iban"}


def test_volver_a_corregir_el_mismo_campo_lo_pisa(cliente_con_fakes):
    _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": "B90233418"}})
    respuesta = _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": "B46102331"}})
    campos = _campos(respuesta)
    assert campos["nif"]["valor"] == "B46102331"
    assert len(campos) == 1


def test_se_guarda_la_nota_y_el_autor(cliente_con_fakes):
    respuesta = _poner(
        cliente_con_fakes,
        ESCALADA,
        {"nif": {"valor": "B90233418", "nota": "leido del sello de la esquina"}},
        autor="ana",
    )
    campo = _campos(respuesta)["nif"]
    assert campo["nota"] == "leido del sello de la esquina"
    assert campo["autor"] == "ana"


def test_el_valor_se_recorta(cliente_con_fakes):
    respuesta = _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": "  B90233418  "}})
    assert _campos(respuesta)["nif"]["valor"] == "B90233418"


def test_nota_en_blanco_se_guarda_como_null(cliente_con_fakes):
    respuesta = _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": "B", "nota": "   "}})
    assert _campos(respuesta)["nif"]["nota"] is None


def test_los_campos_salen_en_orden_canonico(cliente_con_fakes):
    """El panel pinta el formulario en este orden; no puede bailar entre peticiones."""
    _poner(cliente_con_fakes, ESCALADA, {"total": {"valor": "1"}, "nif": {"valor": "B"}})
    respuesta = cliente_con_fakes.get(f"/api/facturas/{ESCALADA}/correcciones")
    assert [campo["campo"] for campo in respuesta.json()["campos"]] == ["nif", "total"]


def test_todos_los_campos_corregibles_se_aceptan(cliente_con_fakes):
    respuesta = _poner(
        cliente_con_fakes, ESCALADA, {campo: {"valor": "x"} for campo in CAMPOS_CORREGIBLES}
    )
    assert respuesta.status_code == 200
    assert set(_campos(respuesta)) == set(CAMPOS_CORREGIBLES)


# --------------------------------------------------------------------------- #
# La decision del motor no se toca
# --------------------------------------------------------------------------- #
def test_corregir_no_cambia_el_resultado(cliente_con_fakes):
    """Lo mas importante del fichero: la traza sigue contando lo que paso."""
    antes = cliente_con_fakes.get(f"/api/facturas/{ESCALADA}").json()
    assert antes["resultado"] == "ESCALAR"
    _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": "B90233418"}, "iban": {"valor": "ES21"}})
    despues = cliente_con_fakes.get(f"/api/facturas/{ESCALADA}").json()
    assert despues["resultado"] == "ESCALAR"
    assert despues["motivos"] == antes["motivos"]
    assert despues["campos"] == antes["campos"]


def test_corregir_una_pagada_no_la_convierte_en_no_pagar_ni_al_reves(cliente_con_fakes):
    antes = cliente_con_fakes.get(f"/api/facturas/{PAGADA}").json()["resultado"]
    _poner(cliente_con_fakes, PAGADA, {"total": {"valor": "999.99"}})
    assert cliente_con_fakes.get(f"/api/facturas/{PAGADA}").json()["resultado"] == antes


def test_borrar_la_correccion_tampoco_mueve_la_decision(cliente_con_fakes):
    _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": "B90233418"}})
    cliente_con_fakes.delete(f"/api/facturas/{ESCALADA}/correcciones")
    assert cliente_con_fakes.get(f"/api/facturas/{ESCALADA}").json()["resultado"] == "ESCALAR"


def test_el_detalle_incluye_las_correcciones(cliente_con_fakes):
    _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": "B90233418"}})
    detalle = cliente_con_fakes.get(f"/api/facturas/{ESCALADA}").json()
    assert detalle["correcciones"]["campos"][0]["campo"] == "nif"
    assert detalle["correcciones"]["campos"][0]["valor"] == "B90233418"


def test_el_detalle_sin_correcciones_las_da_vacias(cliente_con_fakes):
    detalle = cliente_con_fakes.get(f"/api/facturas/{ESCALADA}").json()
    assert detalle["correcciones"]["campos"] == []


# --------------------------------------------------------------------------- #
# Borrado
# --------------------------------------------------------------------------- #
def test_borrar_todas(cliente_con_fakes):
    _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": "B"}, "iban": {"valor": "ES"}})
    respuesta = cliente_con_fakes.delete(f"/api/facturas/{ESCALADA}/correcciones")
    assert respuesta.status_code == 200
    assert respuesta.json()["campos"] == []
    assert _campos(cliente_con_fakes.get(f"/api/facturas/{ESCALADA}/correcciones")) == {}


def test_borrar_un_solo_campo_conserva_los_demas(cliente_con_fakes):
    _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": "B"}, "iban": {"valor": "ES"}})
    respuesta = cliente_con_fakes.delete(f"/api/facturas/{ESCALADA}/correcciones?campo=nif")
    assert set(_campos(respuesta)) == {"iban"}


def test_al_borrar_el_ultimo_campo_desaparece_el_documento(cliente_con_fakes, fake_mongo):
    """Un `campos: {}` huerfano haria que el panel pintase un formulario vacio."""
    _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": "B"}})
    cliente_con_fakes.delete(f"/api/facturas/{ESCALADA}/correcciones?campo=nif")
    assert ESCALADA not in fake_mongo.correcciones


def test_borrar_lo_que_no_existe_no_falla(cliente_con_fakes):
    respuesta = cliente_con_fakes.delete(f"/api/facturas/{ESCALADA}/correcciones")
    assert respuesta.status_code == 200
    assert respuesta.json()["campos"] == []


def test_borrar_con_campo_invalido_da_400(cliente_con_fakes):
    respuesta = cliente_con_fakes.delete(f"/api/facturas/{ESCALADA}/correcciones?campo=asiento")
    assert respuesta.status_code == 400
    assert respuesta.json()["error"]["codigo"] == "campo_no_corregible"


def test_borrar_correcciones_de_factura_inexistente_da_404(cliente_con_fakes):
    respuesta = cliente_con_fakes.delete("/api/facturas/2026-12-31_P999.pdf/correcciones")
    assert respuesta.status_code == 404


# --------------------------------------------------------------------------- #
# Validacion de la entrada
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("campo", ["asiento", "importe_erp", "resultado", "estado_erp", "nif_maestro"])
def test_campo_no_corregible_da_400(cliente_con_fakes, campo):
    """Lo que viene del ERP no esta en el PDF: el operador no puede verificarlo mirandolo."""
    respuesta = _poner(cliente_con_fakes, ESCALADA, {campo: {"valor": "x"}})
    assert respuesta.status_code == 400
    assert respuesta.json()["error"]["codigo"] == "campo_no_corregible"


def test_sin_campos_da_400(cliente_con_fakes):
    respuesta = _poner(cliente_con_fakes, ESCALADA, {})
    assert respuesta.status_code == 400
    assert respuesta.json()["error"]["codigo"] == "sin_campos"


def test_valor_vacio_da_400(cliente_con_fakes):
    respuesta = _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": ""}})
    assert respuesta.status_code == 400
    assert respuesta.json()["error"]["codigo"] == "valor_vacio"


def test_valor_solo_espacios_da_400(cliente_con_fakes):
    respuesta = _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": "    "}})
    assert respuesta.status_code == 400
    assert respuesta.json()["error"]["codigo"] == "valor_vacio"


def test_valor_demasiado_largo_da_400(cliente_con_fakes):
    respuesta = _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": "x" * 201}})
    assert respuesta.status_code == 400
    assert respuesta.json()["error"]["codigo"] == "valor_demasiado_largo"


def test_valor_de_200_caracteres_entra(cliente_con_fakes):
    respuesta = _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": "x" * 200}})
    assert respuesta.status_code == 200


def test_falta_el_valor_da_422(cliente_con_fakes):
    respuesta = cliente_con_fakes.put(
        f"/api/facturas/{ESCALADA}/correcciones", json={"campos": {"nif": {"nota": "sin valor"}}}
    )
    assert respuesta.status_code == 422


def test_nota_demasiado_larga_da_422(cliente_con_fakes):
    respuesta = _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": "B", "nota": "n" * 501}})
    assert respuesta.status_code == 422


def test_autor_demasiado_largo_da_422(cliente_con_fakes):
    respuesta = _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": "B"}}, autor="a" * 121)
    assert respuesta.status_code == 422


def test_guardar_en_factura_inexistente_da_404(cliente_con_fakes):
    respuesta = _poner(cliente_con_fakes, "2026-12-31_P999.pdf", {"nif": {"valor": "B"}})
    assert respuesta.status_code == 404
    assert respuesta.json()["error"]["codigo"] == "factura_no_encontrada"


def test_validacion_antes_de_escribir(cliente_con_fakes, fake_mongo):
    """Un campo invalido no puede dejar media correccion guardada."""
    _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": "B"}})
    _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": "B2"}, "asiento": {"valor": "x"}})
    assert fake_mongo.correcciones[ESCALADA]["campos"]["nif"]["valor"] == "B"


# --------------------------------------------------------------------------- #
# Mongo caido
# --------------------------------------------------------------------------- #
def test_guardar_con_mongo_caido_da_503(cliente_con_fakes, fake_mongo):
    fake_mongo._error = "connection refused"
    respuesta = _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": "B"}})
    assert respuesta.status_code == 503
    assert respuesta.json()["error"]["codigo"] == "mongo_no_disponible"


def test_leer_con_mongo_caido_devuelve_vacio_en_vez_de_fallar(cliente_con_fakes, fake_mongo):
    """La correccion es un extra: sin Mongo, la factura se sigue viendo."""
    _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": "B"}})
    fake_mongo._error = "connection refused"
    respuesta = cliente_con_fakes.get(f"/api/facturas/{ESCALADA}/correcciones")
    assert respuesta.status_code == 200
    assert respuesta.json()["campos"] == []


def test_detalle_con_mongo_caido_sigue_trayendo_la_factura(cliente_con_fakes, fake_mongo):
    fake_mongo._error = "connection refused"
    respuesta = cliente_con_fakes.get(f"/api/facturas/{ESCALADA}")
    assert respuesta.status_code == 200
    assert respuesta.json()["resultado"] == "ESCALAR"
    assert respuesta.json()["correcciones"]["campos"] == []


def test_borrar_con_mongo_caido_da_503(cliente_con_fakes, fake_mongo):
    fake_mongo._error = "connection refused"
    respuesta = cliente_con_fakes.delete(f"/api/facturas/{ESCALADA}/correcciones")
    assert respuesta.status_code == 503


# --------------------------------------------------------------------------- #
# Aislamiento entre facturas
# --------------------------------------------------------------------------- #
def test_las_correcciones_no_se_mezclan_entre_facturas(cliente_con_fakes):
    _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": "B90233418"}})
    respuesta = cliente_con_fakes.get(f"/api/facturas/{PAGADA}/correcciones")
    assert respuesta.json()["campos"] == []


def test_borrar_una_no_toca_la_otra(cliente_con_fakes):
    _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": "B90233418"}})
    _poner(cliente_con_fakes, PAGADA, {"nif": {"valor": "B46102331"}})
    cliente_con_fakes.delete(f"/api/facturas/{ESCALADA}/correcciones")
    assert set(_campos(cliente_con_fakes.get(f"/api/facturas/{PAGADA}/correcciones"))) == {"nif"}


def test_la_revision_y_las_correcciones_son_independientes(cliente_con_fakes):
    cliente_con_fakes.put(f"/api/facturas/{ESCALADA}/revision", json={"estado": "RESUELTA"})
    _poner(cliente_con_fakes, ESCALADA, {"nif": {"valor": "B90233418"}})
    detalle = cliente_con_fakes.get(f"/api/facturas/{ESCALADA}").json()
    assert detalle["revision"]["estado"] == "RESUELTA"
    assert set(_campos(cliente_con_fakes.get(f"/api/facturas/{ESCALADA}/correcciones"))) == {"nif"}


# --------------------------------------------------------------------------- #
# Preflight del navegador (CORS)
# --------------------------------------------------------------------------- #
# Estos dos tests existen por un fallo real: el middleware de CORS estaba
# configurado con `allow_methods=["GET", "POST", "OPTIONS"]`, asi que el
# preflight de `PUT`/`DELETE` se rechazaba con un 400 **antes** de llegar al
# endpoint. El formulario del panel fallaba con un error de CORS que parecia del
# proxy y era del propio middleware. Con `curl` no se ve: no manda preflight.
ORIGEN = "http://localhost:5173"


def _preflight(cliente, file_id, metodo):
    return cliente.options(
        f"/api/facturas/{file_id}/correcciones",
        headers={
            "Origin": ORIGEN,
            "Access-Control-Request-Method": metodo,
            "Access-Control-Request-Headers": "content-type",
        },
    )


@pytest.mark.parametrize("metodo", ["PUT", "DELETE"])
def test_el_preflight_de_correcciones_pasa(cliente_con_fakes, metodo):
    respuesta = _preflight(cliente_con_fakes, ESCALADA, metodo)
    assert respuesta.status_code == 200
    assert respuesta.headers["access-control-allow-origin"] == ORIGEN
    permitidos = {m.strip() for m in respuesta.headers["access-control-allow-methods"].split(",")}
    assert metodo in permitidos


def test_el_preflight_deja_pasar_la_cabecera_de_contenido(cliente_con_fakes):
    """Sin `Content-Type` en la lista blanca, el `PUT` con JSON no sale del navegador."""
    respuesta = _preflight(cliente_con_fakes, ESCALADA, "PUT")
    permitidas = {
        h.strip().lower() for h in respuesta.headers["access-control-allow-headers"].split(",")
    }
    assert "content-type" in permitidas


def test_los_metodos_de_solo_lectura_siguen_estando(cliente_con_fakes):
    """El cambio no puede quitar lo que ya funcionaba."""
    respuesta = _preflight(cliente_con_fakes, ESCALADA, "GET")
    permitidos = {m.strip() for m in respuesta.headers["access-control-allow-methods"].split(",")}
    assert {"GET", "POST", "OPTIONS"} <= permitidos
