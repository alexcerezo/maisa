"""La norma se rechaza entera o no se usa: nunca se decide con media norma.

Estos tests existen por un fallo concreto y reproducido. `Politica.carga` leia
todos los campos con `.get(clave, por_defecto)`, asi que un TOML con otro
esquema (el del motor Rust legado, o uno con una clave mal escrita) cargaba
"bien" con `[precedencia]`, `[reglas]` y `[hechos_duros]` vacios. Resultado:
las 500 facturas caian al resultado por defecto y la entrega salia con
`{'PAGAR': 500}`, `validacion: OK` y codigo de salida 0. Un fail-open silencioso
que paga todo. Ahora `ConfigInvalida` para el motor.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from maisa import norma, procesa

RAIZ = Path(__file__).resolve().parent.parent
VIVA = RAIZ / "config" / "reglas.toml"
LEGADO = RAIZ.parent / "config" / "reglas.toml"

MINIMA = """
version = "prueba"
[umbrales]
[precedencia]
PAGAR = 1
NO_PAGAR = 2
ESCALAR = 3
[reglas.r1]
si_falla = "ESCALAR"
[hechos_duros]
"""


def escribe(tmp_path: Path, cuerpo: str, nombre: str = "reglas.toml") -> Path:
    ruta = tmp_path / nombre
    ruta.write_text(cuerpo, encoding="utf-8")
    return ruta


def test_la_norma_viva_carga(politica: norma.Politica) -> None:
    """Control: la validacion no puede rechazar la norma buena."""
    assert politica.version == "norma_v3.1"
    assert set(politica.precedencia) == set(norma.RESULTADOS)


def test_la_plantilla_minima_carga(tmp_path: Path) -> None:
    assert norma.Politica.carga(escribe(tmp_path, MINIMA)).version == "prueba"


def test_el_config_legado_del_motor_rust_se_rechaza() -> None:
    """El fallo original: este fichero pagaba las 500 facturas sin quejarse."""
    if not LEGADO.exists():  # pragma: no cover - depende del checkout
        pytest.skip(f"no esta el config legado: {LEGADO}")
    with pytest.raises(norma.ConfigInvalida) as exc:
        norma.Politica.carga(LEGADO)
    assert "faltan las secciones" in str(exc.value)
    # El mensaje tiene que decir cual es la buena: si no, el siguiente que lo
    # ejecute vuelve a apuntar al legado.
    assert "motor/config/reglas.toml" in str(exc.value)


@pytest.mark.parametrize("seccion", ["umbrales", "precedencia", "reglas", "hechos_duros"])
def test_falta_una_seccion(tmp_path: Path, seccion: str) -> None:
    cuerpo = MINIMA
    if seccion == "umbrales":
        cuerpo = cuerpo.replace("[umbrales]\n", "")
    elif seccion == "precedencia":
        cuerpo = cuerpo.replace("[precedencia]\nPAGAR = 1\nNO_PAGAR = 2\nESCALAR = 3\n", "")
    elif seccion == "reglas":
        cuerpo = cuerpo.replace('[reglas.r1]\nsi_falla = "ESCALAR"\n', "")
    else:
        cuerpo = cuerpo.replace("[hechos_duros]\n", "")
    with pytest.raises(norma.ConfigInvalida, match="faltan las secciones"):
        norma.Politica.carga(escribe(tmp_path, cuerpo))


def test_clave_desconocida_se_rechaza(tmp_path: Path) -> None:
    """Una clave mal escrita no se ignora: cambia la decision."""
    # A nivel de primer nivel (antes de cualquier [seccion]): despues de
    # `version` sigue siendo raiz, no una clave de la tabla anterior.
    cuerpo = MINIMA.replace('version = "prueba"\n', 'version = "prueba"\numbralez = 1\n')
    with pytest.raises(norma.ConfigInvalida, match="desconocidas"):
        norma.Politica.carga(escribe(tmp_path, cuerpo))


@pytest.mark.parametrize("cuerpo, trozo", [
    (MINIMA.replace('version = "prueba"', "version = 3"), "version"),
    (MINIMA.replace('version = "prueba"', 'version = "  "'), "version"),
    (MINIMA.replace("PAGAR = 1\n", ""), "no declara"),
    # [reglas] presente pero vacia: sin reglas, todo cae al resultado por defecto.
    (MINIMA.replace('[reglas.r1]\nsi_falla = "ESCALAR"\n', "[reglas]\n"), "al menos una regla"),
])
def test_esquema_roto(tmp_path: Path, cuerpo: str, trozo: str) -> None:
    with pytest.raises(norma.ConfigInvalida, match=trozo):
        norma.Politica.carga(escribe(tmp_path, cuerpo))


def test_el_cli_sale_con_2_y_no_escribe_entrega(tmp_path: Path) -> None:
    """Fail-loud en la puerta de entrada: ni exit 0 ni un JSONL a medias."""
    if not LEGADO.exists():  # pragma: no cover - depende del checkout
        pytest.skip(f"no esta el config legado: {LEGADO}")
    salida = tmp_path / "outcomes.jsonl"
    codigo = procesa.main([
        "--config", str(LEGADO),
        "--facturas", str(tmp_path),
        "--salida", str(salida),
    ])
    assert codigo == 2
    assert not salida.exists()
