"""Cache HTTP del listado: ETag, revalidacion con `If-None-Match` y 304.

El listado se sirve de la traza del motor, que solo cambia cuando el motor
vuelve a ejecutarse: es el candidato natural a llevar `ETag`. Aqui se fija ese
contrato; el cuerpo del 200 lo siguen comprobando los tests de `test_facturas`.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.traza import FIRMA_SIN_TRAZA, TrazaStore

from .conftest import REGISTROS, construir_settings, escribir_traza

RUTA = "/api/facturas"


def _etag(respuesta) -> str:
    """ETag fuerte (entre comillas dobles y sin el prefijo debil `W/`)."""
    etag = respuesta.headers["etag"]
    assert etag.startswith('"') and etag.endswith('"'), etag
    assert len(etag) > 2
    return etag


# --------------------------------------------------------------------------- #
# Cabeceras del 200
# --------------------------------------------------------------------------- #
def test_el_200_trae_etag_y_cache_control(client):
    respuesta = client.get(RUTA)
    assert respuesta.status_code == 200
    _etag(respuesta)
    assert respuesta.headers["cache-control"] == "private, no-cache"
    assert respuesta.headers["vary"] == "Accept-Encoding"
    # El contrato del cuerpo no cambia por anadir cache.
    assert respuesta.json()["total"] == 4


def test_el_etag_es_estable_entre_peticiones_iguales(client):
    assert _etag(client.get(RUTA)) == _etag(client.get(RUTA))
    # El orden de los parametros no altera la respuesta ni, por tanto, el ETag.
    assert _etag(client.get(RUTA, params={"lote": 1, "limit": 2})) == _etag(
        client.get(RUTA, params={"limit": 2, "lote": 1})
    )


# --------------------------------------------------------------------------- #
# Revalidacion
# --------------------------------------------------------------------------- #
def test_if_none_match_igual_da_304_sin_cuerpo(client):
    primera = client.get(RUTA)
    etag = _etag(primera)

    segunda = client.get(RUTA, headers={"If-None-Match": etag})
    assert segunda.status_code == 304
    assert segunda.headers["etag"] == etag
    assert segunda.headers["cache-control"] == "private, no-cache"
    assert segunda.content == b""
    assert segunda.text == ""


def test_if_none_match_distinto_da_200(client):
    respuesta = client.get(RUTA, headers={"If-None-Match": '"etag-caducado"'})
    assert respuesta.status_code == 200
    assert _etag(respuesta) != '"etag-caducado"'
    assert respuesta.json()["total"] == 4


def test_if_none_match_asterisco_da_304(client):
    respuesta = client.get(RUTA, headers={"If-None-Match": "*"})
    assert respuesta.status_code == 304
    assert respuesta.content == b""


def test_if_none_match_debil_y_en_lista(client):
    """La comparacion es debil y la cabecera es una lista separada por comas."""
    etag = _etag(client.get(RUTA))

    debil = client.get(RUTA, headers={"If-None-Match": f"W/{etag}"})
    assert debil.status_code == 304
    assert debil.headers["etag"] == etag

    lista = client.get(RUTA, headers={"If-None-Match": f'"otro", {etag}'})
    assert lista.status_code == 304

    ninguno = client.get(RUTA, headers={"If-None-Match": '"uno", "dos"'})
    assert ninguno.status_code == 200


def test_sin_traza_el_503_gana_al_304(tmp_path: Path, facturas_dir: Path, ui_dir: Path):
    """Un `If-None-Match: *` no puede tapar que la traza ya no esta."""
    vacio = tmp_path / "sin-traza"
    vacio.mkdir()
    settings = construir_settings(vacio, facturas_dir, ui_dir)
    with TestClient(create_app(settings)) as cliente:
        respuesta = cliente.get(RUTA, headers={"If-None-Match": "*"})
    assert respuesta.status_code == 503
    assert respuesta.json()["error"]["codigo"] == "traza_no_disponible"


# --------------------------------------------------------------------------- #
# Que invalida el ETag
# --------------------------------------------------------------------------- #
def test_el_etag_cambia_con_cada_parametro_de_consulta(client):
    base = _etag(client.get(RUTA))
    variantes = [
        {"resultado": "PAGAR"},
        {"lote": 1},
        {"proveedor": "levante"},
        {"q": "PO-2026-0476"},
        {"limit": 1},
        {"offset": 1},
    ]
    etags = {_etag(client.get(RUTA, params=params)) for params in variantes}
    assert base not in etags
    assert len(etags) == len(variantes)
    # El parametro cambia de verdad la respuesta: no es solo un ETag distinto.
    assert client.get(RUTA, params={"resultado": "PAGAR"}).json()["total"] != 4


def test_el_etag_cambia_cuando_cambia_la_traza(client, outputs_dir: Path):
    antes = _etag(client.get(RUTA))

    nueva = dict(REGISTROS[0], file_id="2026-02-04_P005.pdf")
    escribir_traza(outputs_dir / "outcomes_traza.jsonl", [*REGISTROS, nueva])

    despues = client.get(RUTA)
    assert despues.status_code == 200
    assert despues.json()["total"] == 5
    assert _etag(despues) != antes

    # El ETag viejo ya no vale: hay que devolver el cuerpo nuevo, no un 304.
    revalidacion = client.get(RUTA, headers={"If-None-Match": antes})
    assert revalidacion.status_code == 200
    assert revalidacion.json()["total"] == 5


# --------------------------------------------------------------------------- #
# Firma del store
# --------------------------------------------------------------------------- #
def test_la_firma_del_store_es_estable_y_sigue_al_fichero(outputs_dir: Path):
    ruta = outputs_dir / "outcomes_traza.jsonl"
    store = TrazaStore(ruta)

    firma = store.firma()
    assert firma == store.firma()
    assert len(firma) == 64  # sha256 en hexadecimal
    assert firma != FIRMA_SIN_TRAZA

    escribir_traza(ruta, REGISTROS[:1])
    assert store.firma() != firma


def test_la_firma_sin_traza_es_estable(tmp_path: Path):
    store = TrazaStore(tmp_path / "no-existe.jsonl")
    assert store.firma() == FIRMA_SIN_TRAZA
    assert store.firma() == FIRMA_SIN_TRAZA
    assert store.disponible is False
