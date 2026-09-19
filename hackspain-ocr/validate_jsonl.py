# -*- coding: utf-8 -*-
"""Validador de la entrega JSONL.

Comprueba que el fichero de resultados cumple el contrato del enunciado: una
linea por factura con `{"file_id": "<nombre del PDF>", "result": "<decision>"}`.

Uso:
    python validate_jsonl.py outcomes.jsonl --pdf-dir data/facturas
    python validate_jsonl.py outputs/outcomes_lote2.jsonl --pdf-dir data/facturas_lote2

Contrato validado (fuente: albertitos_plan.md, spec_y_plan.md):
  - UTF-8 SIN BOM
  - una linea por PDF, sin lineas vacias
  - clave `result` (NO `resultado`) con valor PAGAR | NO_PAGAR | ESCALAR
  - clave `file_id` con el nombre EXACTO del PDF: extension y mayusculas incluidas,
    sin separadores de ruta
  - `file_id` unico
  - todas las facturas del directorio tienen su linea (cobertura total)
  - los campos de traza extra son AVISOS, nunca errores

OJO: la comprobacion de nombres se hace por pertenencia a un conjunto con los
nombres reales leidos del directorio. No se usa `Path.exists()`, porque en
Windows el sistema de ficheros ignora mayusculas y aceptaria como valido un
`file_id` como `FACTURA_123.PDF` cuando el PDF real es `factura_123.pdf`.

Codigos de salida:
    0  correcto (puede haber avisos)
    1  el fichero incumple el contrato
    2  no existe el fichero JSONL o el directorio de PDFs
"""

import argparse
import json
import sys
from pathlib import Path

VALORES_RESULTADO = frozenset({"PAGAR", "NO_PAGAR", "ESCALAR"})
CLAVE_RESULTADO = "result"
CLAVE_FILE_ID = "file_id"
CLAVE_SOSPECHOSA = "resultado"
CLAVES_CONOCIDAS = frozenset({CLAVE_FILE_ID, CLAVE_RESULTADO})
SUFIJO_PDF = ".pdf"
BOM = "\ufeff"
SEPARADORES_RUTA = ("/", "\\")

SALIDA_OK = 0
SALIDA_ERRORES = 1
SALIDA_NO_ENCONTRADO = 2


def _preparar_salida() -> None:
    """Imprime siempre, aunque el mensaje lleve caracteres fuera del codec.

    En una consola Windows con cp1252, un `file_id` o un nombre de PDF con
    caracteres no representables (p. ej. `factura_日本.pdf`) hacia que `print`
    abortara el informe con `UnicodeEncodeError` y una traza, perdiendo el
    resumen final. Con `errors="replace"` el informe sale entero (el caracter
    problematico se sustituye por `?`).
    """
    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


class Informe:
    """Acumula errores y avisos, cada uno con su numero de linea (o None).

    Al imprimir agrupa los mensajes identicos: un fichero de 500 lineas con el
    mismo fallo produce UNA linea de informe, no 500.
    """

    def __init__(self) -> None:
        self.errores: list[tuple[int | None, str]] = []
        self.avisos: list[tuple[int | None, str]] = []

    def error(self, mensaje: str, linea: int | None = None) -> None:
        self.errores.append((linea, mensaje))

    def aviso(self, mensaje: str, linea: int | None = None) -> None:
        self.avisos.append((linea, mensaje))

    def imprimir(self, mostrar_avisos: bool = True) -> None:
        bloques = [("ERRORES", self.errores)]
        if mostrar_avisos:
            bloques.append(("AVISOS", self.avisos))
        for titulo, entradas in bloques:
            if not entradas:
                continue
            print(f"\n{titulo} ({len(entradas)}):")
            for mensaje, numeros in _agrupar(entradas):
                veces = f"  [{len(numeros)} veces]" if len(numeros) > 1 else ""
                print(f"  {_etiqueta(numeros)}{mensaje}{veces}")


def _agrupar(entradas: list[tuple[int | None, str]]) -> list[tuple[str, list[int | None]]]:
    """Agrupa por mensaje identico, ordenado por el numero de linea mas bajo."""
    grupos: dict[str, list[int | None]] = {}
    for numero, mensaje in entradas:
        grupos.setdefault(mensaje, []).append(numero)
    return sorted(grupos.items(), key=lambda par: (par[1][0] is None, par[1][0] or 0))


def _etiqueta(numeros: list[int | None], limite: int = 5) -> str:
    """Prefijo con las lineas afectadas, abreviado si son muchas."""
    utiles = [numero for numero in numeros if numero is not None]
    if not utiles:
        return "            "
    if len(utiles) == 1:
        return f"linea {utiles[0]:>4}  "
    muestra = ", ".join(str(numero) for numero in utiles[:limite])
    resto = f" y {len(utiles) - limite} mas" if len(utiles) > limite else ""
    return f"lineas {muestra}{resto}  "


def listar_pdfs(directorio: Path) -> list[str]:
    """Nombres exactos de los PDF del directorio, sin entrar en subdirectorios."""
    return sorted(
        entrada.name
        for entrada in directorio.iterdir()
        if entrada.is_file() and entrada.suffix.lower() == SUFIJO_PDF
    )


def leer_texto(ruta: Path, informe: Informe) -> str | None:
    """Devuelve el texto del fichero, o None si no es UTF-8 valido."""
    try:
        return ruta.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        informe.error(f"no es UTF-8 valido (byte {exc.start}: {exc.reason})")
        return None


def partir_lineas(texto: str, informe: Informe) -> tuple[list[tuple[int, str]], int]:
    """Lineas utiles con su numero 1-based; valida BOM, vacias y CRLF.

    Devuelve tambien el numero de lineas fisicas del fichero (sin contar el
    salto final), para que el informe pueda decir cuantas se descartaron.

    Solo se perdona el salto de linea final. Cualquier otra linea vacia (o con
    solo espacios) es un error.
    """
    if texto.startswith(BOM):
        informe.error("empieza con BOM (U+FEFF): el contrato exige UTF-8 sin BOM")
        texto = texto[len(BOM) :]

    if texto == "":
        return [], 0

    crudo = texto.split("\n")
    if crudo[-1] == "":
        crudo.pop()

    utiles: list[tuple[int, str]] = []
    for numero, linea in enumerate(crudo, start=1):
        if linea.endswith("\r"):
            informe.aviso("termina en CRLF; el contrato no lo documenta", numero)
            linea = linea[:-1]
        if linea.strip() == "":
            informe.error("linea vacia", numero)
            continue
        utiles.append((numero, linea))
    return utiles, len(crudo)


def revisar_json(numero: int, linea: str, informe: Informe) -> dict | None:
    """Parsea la linea y comprueba que sea un objeto JSON."""
    try:
        documento = json.loads(linea)
    except json.JSONDecodeError as exc:
        informe.error(f"no es JSON valido ({exc.msg}, columna {exc.colno})", numero)
        return None
    if not isinstance(documento, dict):
        informe.error(f"no es un objeto JSON sino {type(documento).__name__}", numero)
        return None
    return documento


def revisar_campos(numero: int, documento: dict, informe: Informe) -> str | None:
    """Valida `file_id` y `result`; devuelve el `file_id` si es utilizable."""
    identificador = documento.get(CLAVE_FILE_ID)
    resultado = documento.get(CLAVE_RESULTADO)
    utilizable = False

    if CLAVE_FILE_ID not in documento:
        informe.error(f"falta la clave '{CLAVE_FILE_ID}'", numero)
    elif not isinstance(identificador, str) or not identificador:
        informe.error(f"'{CLAVE_FILE_ID}' debe ser una cadena no vacia", numero)
    elif any(separador in identificador for separador in SEPARADORES_RUTA):
        informe.error(f"'{CLAVE_FILE_ID}' no debe contener rutas: {identificador!r}", numero)
    else:
        utilizable = True

    if CLAVE_RESULTADO not in documento:
        if CLAVE_SOSPECHOSA in documento:
            informe.error(
                f"falta '{CLAVE_RESULTADO}' y en su lugar aparece '{CLAVE_SOSPECHOSA}': "
                f"el contrato exige '{CLAVE_RESULTADO}'",
                numero,
            )
        else:
            informe.error(f"falta la clave '{CLAVE_RESULTADO}'", numero)
    elif resultado not in VALORES_RESULTADO:
        informe.error(
            f"'{CLAVE_RESULTADO}' = {resultado!r} no es uno de "
            f"{', '.join(sorted(VALORES_RESULTADO))}",
            numero,
        )

    extras = sorted(set(documento) - CLAVES_CONOCIDAS)
    if extras:
        informe.aviso(f"campos de traza extra (permitidos): {', '.join(extras)}", numero)

    return identificador if utilizable else None


def revisar_duplicados(pares: list[tuple[int, str]], informe: Informe) -> None:
    """Un `file_id` no puede repetirse."""
    vistos: dict[str, int] = {}
    for numero, identificador in pares:
        anterior = vistos.get(identificador)
        if anterior is None:
            vistos[identificador] = numero
        else:
            informe.error(f"'{identificador}' repetido (ya en la linea {anterior})", numero)


def revisar_cobertura(pares: list[tuple[int, str]], pdfs: list[str], informe: Informe) -> None:
    """Cada `file_id` debe ser un PDF real y cada PDF debe tener su linea."""
    reales = set(pdfs)
    por_minusculas: dict[str, str] = {}
    for nombre in pdfs:
        por_minusculas.setdefault(nombre.lower(), nombre)

    for numero, identificador in pares:
        if identificador in reales:
            continue
        real = por_minusculas.get(identificador.lower())
        if real is not None:
            informe.error(
                f"'{identificador}' no existe; el fichero real es '{real}' "
                "(el nombre debe coincidir exactamente)",
                numero,
            )
        else:
            informe.error(f"'{identificador}' no es un PDF del directorio", numero)

    presentes = {identificador for _, identificador in pares}
    for nombre in pdfs:
        if nombre not in presentes:
            informe.error(f"el PDF '{nombre}' no tiene ninguna linea")


def validar(ruta_jsonl: Path, directorio_pdf: Path, mostrar_avisos: bool) -> int:
    _preparar_salida()
    if not ruta_jsonl.is_file():
        print(f"error: no existe el fichero {ruta_jsonl}")
        return SALIDA_NO_ENCONTRADO
    if not directorio_pdf.is_dir():
        print(f"error: no existe el directorio {directorio_pdf}")
        return SALIDA_NO_ENCONTRADO

    informe = Informe()
    pdfs = listar_pdfs(directorio_pdf)

    texto = leer_texto(ruta_jsonl, informe)
    utiles, total_lineas = partir_lineas(texto, informe) if texto is not None else ([], 0)

    pares: list[tuple[int, str]] = []
    for numero, linea in utiles:
        documento = revisar_json(numero, linea, informe)
        if documento is None:
            continue
        identificador = revisar_campos(numero, documento, informe)
        if identificador is not None:
            pares.append((numero, identificador))

    revisar_duplicados(pares, informe)
    revisar_cobertura(pares, pdfs, informe)

    print(f"jsonl     {ruta_jsonl}")
    print(f"pdfs      {directorio_pdf}  ({len(pdfs)} ficheros)")
    print(f"lineas    {len(pares)} validas de {total_lineas} leidas")
    informe.imprimir(mostrar_avisos)

    if informe.errores:
        print(f"\nFALLO  {len(informe.errores)} error(es), {len(informe.avisos)} aviso(s)")
        return SALIDA_ERRORES

    print(f"\nOK  {len(pares)} lineas validas, contrato cumplido ({len(informe.avisos)} aviso(s))")
    return SALIDA_OK


def main() -> int:
    parser = argparse.ArgumentParser(description="Valida un fichero JSONL de entrega.")
    parser.add_argument("jsonl_path", type=Path, help="fichero JSONL a validar")
    parser.add_argument(
        "--pdf-dir",
        type=Path,
        required=True,
        help="directorio con los PDF de referencia (define los file_id validos)",
    )
    parser.add_argument("--quiet", action="store_true", help="no imprimir los avisos")
    args = parser.parse_args()
    return validar(args.jsonl_path, args.pdf_dir, mostrar_avisos=not args.quiet)


if __name__ == "__main__":
    sys.exit(main())
