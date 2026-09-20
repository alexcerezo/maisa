"""Tests de `maisa.lectura`: escalera, cache versionada, degradacion y nube.

Todo sintetico y sin red: el PDF se fabrica a mano (con capa de texto de
verdad, sin anadir dependencias de escritura de PDFs), el servicio de vision se
parchea con `monkeypatch` y el backoff se sustituye para no dormir de verdad.
Lo que necesita el corpus real o el contenedor va marcado `lento`.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from maisa import lectura, procesa, texto

#: Factura larga y legible: la capa de texto del PDF basta (calidad alta).
LINEAS_FACTURA = [
    "FACTURA 2026/11604",
    "Suministros Levante S.L. NIF B46102331",
    "IBAN: ES21 0049 1500 0512 3456 7890",
    "Pedido: PO-2026-0096   Fecha: 05/01/2026",
    "Base: 2.489,99   IVA (21%): 522,90   TOTAL: 3.012,89 EUR",
    "Cliente: Banco Miralmar S.A. CIF: A58231074",
] + [
    "Condiciones de pago: transferencia a 30 dias. Documento emitido conforme "
    "al RD 1619/2012 por el sistema de facturacion del proveedor."
] * 6


# --------------------------------------------------------------- PDF a mano
def _escapa_pdf(cadena: str) -> str:
    return cadena.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def pdf_con_texto(paginas: list[list[str]]) -> bytes:
    """PDF minimo con capa de texto real: una lista de lineas por pagina.

    Se escribe a mano para no depender de una libreria de generacion de PDFs:
    aqui se prueba la escalera, no el generador.
    """
    objetos: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    hijos: list[str] = []
    numero = 4
    for lineas in paginas:
        pagina, contenido = numero, numero + 1
        numero += 2
        hijos.append(f"{pagina} 0 R")
        cuerpo = ["BT /F1 10 Tf 20 800 Td 14 TL"]
        for i, linea in enumerate(lineas):
            if i:
                cuerpo.append("T*")
            cuerpo.append(f"({_escapa_pdf(linea)}) Tj")
        cuerpo.append("ET")
        flujo = "\n".join(cuerpo).encode("latin-1")
        objetos[pagina] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {contenido} 0 R >>"
        ).encode("ascii")
        objetos[contenido] = b"<< /Length %d >>\nstream\n" % len(flujo) + flujo + b"\nendstream"
    objetos[2] = (
        "<< /Type /Pages /Kids [%s] /Count %d >>" % (" ".join(hijos), len(paginas))
    ).encode("ascii")

    salida = bytearray(b"%PDF-1.4\n")
    posiciones: dict[int, int] = {}
    for n in sorted(objetos):
        posiciones[n] = len(salida)
        salida += b"%d 0 obj\n" % n + objetos[n] + b"\nendobj\n"
    inicio_xref = len(salida)
    total = max(objetos) + 1
    salida += b"xref\n0 %d\n0000000000 65535 f \n" % total
    for n in range(1, total):
        salida += b"%010d 00000 n \n" % posiciones[n]
    salida += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        total,
        inicio_xref,
    )
    return bytes(salida)


def pdf_escaneado(carpeta: Path, nombre: str = "escaneada.pdf", marca: str = "FAX") -> Path:
    """PDF con capa de texto inservible: obliga a bajar al peldano de vision."""
    ruta = carpeta / nombre
    ruta.write_bytes(pdf_con_texto([[marca, "2026"]]))
    return ruta


def pdf_con_capa(carpeta: Path, nombre: str = "legible.pdf") -> Path:
    ruta = carpeta / nombre
    ruta.write_bytes(pdf_con_texto([LINEAS_FACTURA]))
    return ruta


def respuesta_vision(*paginas: str) -> dict:
    """Respuesta del servicio de vision con granularidad de pagina."""
    return {"results": [{"text": pagina} for pagina in paginas]}


class RespuestaFalsa:
    """Respuesta HTTP minima, sin socket: solo lo que usa `lectura`."""

    def __init__(self, datos: dict, codigo: int = 200) -> None:
        self._datos = datos
        self.status_code = codigo

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise OSError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._datos


def revienta(*args: object, **kwargs: object) -> object:
    raise AssertionError("no se deberia haber llamado al servicio de vision")


def sin_servicio(*args: object, **kwargs: object) -> object:
    raise ConnectionError("connection refused")


# ------------------------------------------------------------- aislamiento
@pytest.fixture
def cache(carpeta: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Cache de OCR aislada: los tests no tocan `maisa/motor/.cache/ocr`."""
    destino = carpeta / "ocr"
    destino.mkdir()
    monkeypatch.setattr(lectura, "CACHE_OCR", destino)
    # Firma vacia: la cache legacy vale y no se consulta el servicio.
    monkeypatch.setattr(lectura, "_FIRMA_MOTOR", "")
    monkeypatch.setattr(lectura, "_espera", lambda segundos: None)
    monkeypatch.delenv("MAISA_OCR_NUBE", raising=False)
    monkeypatch.delenv("MAISA_OCR_REINTENTOS", raising=False)
    monkeypatch.delenv("MAISA_OCR_NUBE_MAX", raising=False)
    monkeypatch.delenv(lectura.CLAVE_CACHE_ENV, raising=False)
    lectura.reinicia_clave_cache()
    lectura.CACHE_CONTADORES.clear()
    lectura.reinicia_presupuesto_nube()
    return destino


def escribe_cache(cache: Path, sha: str, datos: dict) -> Path:
    ruta = cache / f"{sha}.json"
    ruta.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
    return ruta


# ------------------------------------------------------------ cache versionada
def test_cache_legacy_sin_version_se_acepta(
    carpeta: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lo ya commiteado no se invalida: es lo que permite reproducir sin red."""
    ruta = pdf_escaneado(carpeta)
    sha = lectura.sha256_pdf(ruta)
    escribe_cache(cache, sha, {"sha256": sha, "texto": "TOTAL 99,00 EUR"})
    monkeypatch.setattr(lectura, "ocr_contenedor", revienta)

    doc = lectura.lee(ruta)

    assert doc.escalon == "cache_ocr"
    assert doc.cache is True
    assert doc.degradado is False
    assert "TOTAL 99,00 EUR" in doc.lectura.texto
    assert doc.lectura.metodo == "vision_ocr"


def test_cache_legacy_con_motor_ajeno_se_acepta_si_no_hay_firma(
    carpeta: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sin firma del motor no hay con que comparar: la entrada vale igual."""
    ruta = pdf_escaneado(carpeta)
    sha = lectura.sha256_pdf(ruta)
    escribe_cache(cache, sha, {"sha256": sha, "motor": "local:otro/motor", "texto": "VIEJO"})
    monkeypatch.setattr(lectura, "ocr_contenedor", revienta)

    assert lectura.lee(ruta).escalon == "cache_ocr"


def test_cache_de_otro_motor_se_ignora(
    carpeta: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ruta = pdf_escaneado(carpeta)
    sha = lectura.sha256_pdf(ruta)
    escribe_cache(cache, sha, {
        "version": lectura.VERSION_CACHE, "sha256": sha, "motor": "local:viejo/motor",
        "paginas": ["VIEJO"], "texto": "VIEJO",
    })
    monkeypatch.setattr(lectura, "_FIRMA_MOTOR", "local:nuevo/motor")
    monkeypatch.setattr(lectura, "_peticion_ocr", lambda *a, **k: respuesta_vision("NUEVO 12,34 EUR"))

    doc = lectura.lee(ruta)

    assert doc.escalon == "vision_ocr"
    assert "NUEVO" in doc.lectura.texto
    assert doc.motor == "local:nuevo/motor"


def test_cache_con_version_desconocida_se_ignora_y_se_relee(
    carpeta: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ruta = pdf_escaneado(carpeta)
    sha = lectura.sha256_pdf(ruta)
    escribe_cache(cache, sha, {"version": 99, "sha256": sha, "texto": "VIEJO"})
    monkeypatch.setattr(lectura, "_peticion_ocr", lambda *a, **k: respuesta_vision("NUEVO 12,34 EUR"))

    doc = lectura.lee(ruta)

    assert doc.escalon == "vision_ocr"
    assert "NUEVO" in doc.lectura.texto
    guardado = json.loads((cache / f"{sha}.json").read_text(encoding="utf-8"))
    assert guardado["version"] == lectura.VERSION_CACHE
    assert guardado["sha256"] == sha
    assert guardado["escalon"] == "vision_ocr"
    assert guardado["paginas"] == ["NUEVO 12,34 EUR"]
    assert guardado["texto"] == "NUEVO 12,34 EUR"


def test_cache_versionada_se_lee_por_paginas(carpeta: Path, cache: Path) -> None:
    """`texto` se reconstruye uniendo `paginas` con saltos de linea."""
    ruta = pdf_escaneado(carpeta)
    sha = lectura.sha256_pdf(ruta)
    escribe_cache(cache, sha, {
        "version": lectura.VERSION_CACHE, "sha256": sha, "motor": "", "escalon": "vision_ocr",
        "paginas": ["pagina uno", "pagina dos"], "texto": "pagina uno\npagina dos",
    })

    doc = lectura.lee(ruta)

    assert doc.escalon == "cache_ocr"
    assert doc.lectura.texto == "pagina uno\npagina dos"
    assert doc.paginas_ocr == 2


def test_cache_de_otro_pdf_se_ignora(
    carpeta: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ruta = pdf_escaneado(carpeta)
    escribe_cache(cache, "0" * 64, {"sha256": "0" * 64, "texto": "DE OTRO PDF"})
    monkeypatch.setattr(lectura, "_peticion_ocr", lambda *a, **k: respuesta_vision("PROPIO"))

    doc = lectura.lee(ruta)

    assert doc.escalon == "vision_ocr"
    assert doc.lectura.texto == "PROPIO"


# ------------------------------------------------------------- firma de cache
@pytest.fixture
def con_clave(monkeypatch: pytest.MonkeyPatch) -> str:
    """Clave de firma de verdad, para los tests que comprueban el sello.

    El `cache` de la fixture anterior apaga la firma con la variable vacia; aqui
    se enciende con una clave cualquiera (el valor no importa, solo que exista).
    """
    monkeypatch.setenv(lectura.CLAVE_CACHE_ENV, "clave-de-test")
    lectura.reinicia_clave_cache()
    yield "clave-de-test"
    lectura.reinicia_clave_cache()


def test_la_cache_escrita_va_firmada(carpeta: Path, cache: Path, con_clave: str) -> None:
    ruta = pdf_escaneado(carpeta)
    monkeypatch_vision = respuesta_vision("TOTAL 1,00 EUR")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(lectura, "_peticion_ocr", lambda *a, **k: monkeypatch_vision)
        doc = lectura.lee(ruta)

    guardado = json.loads((cache / f"{doc.sha256}.json").read_text(encoding="utf-8"))
    assert guardado["hmac"] == lectura.firma_entrada(guardado)


def test_una_entrada_manipulada_no_se_sirve(
    carpeta: Path, cache: Path, con_clave: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si alguien reescribe el texto, la cache se ignora y se vuelve al OCR.

    Es el unico motivo de existir del sello: sin el, `sha256` lo copia cualquiera
    del PDF y una entrada cambiada a mano se sirve como si fuera una lectura.
    """
    ruta = pdf_escaneado(carpeta)
    sha = lectura.sha256_pdf(ruta)
    entrada = {
        "version": lectura.VERSION_CACHE, "sha256": sha, "motor": "", "escalon": "vision_ocr",
        "paginas": ["TOTAL 1,00 EUR"], "texto": "TOTAL 1,00 EUR",
    }
    entrada["hmac"] = lectura.firma_entrada(entrada)
    entrada["texto"] = "TOTAL 999.999,00 EUR"
    entrada["paginas"] = ["TOTAL 999.999,00 EUR"]
    escribe_cache(cache, sha, entrada)
    monkeypatch.setattr(lectura, "_peticion_ocr", lambda *a, **k: respuesta_vision("TOTAL 1,00 EUR"))

    doc = lectura.lee(ruta)

    assert doc.escalon == "vision_ocr"
    assert doc.lectura.texto == "TOTAL 1,00 EUR"


def test_una_entrada_manipulada_se_cuenta(carpeta: Path, cache: Path, con_clave: str) -> None:
    ruta = pdf_escaneado(carpeta)
    sha = lectura.sha256_pdf(ruta)
    entrada = {"version": lectura.VERSION_CACHE, "sha256": sha, "texto": "X", "hmac": "0" * 64}
    escribe_cache(cache, sha, entrada)

    assert lectura.verifica_cache(cache)["manipuladas"] == 1
    lectura.lee(ruta)
    assert lectura.estado_cache()["manipuladas"] == 1


def test_la_cache_sin_sello_se_sigue_leyendo(
    carpeta: Path, cache: Path, con_clave: str
) -> None:
    """Las 30 entradas heredadas valen: la firma avisa, no cierra la puerta.

    Un clon nuevo tiene que reproducir el lote sin clave ni servicio de OCR; si
    exigieramos sello, esas entradas se releerian y el lote dependeria de que el
    contenedor de vision este vivo.
    """
    ruta = pdf_escaneado(carpeta)
    sha = lectura.sha256_pdf(ruta)
    escribe_cache(cache, sha, {"sha256": sha, "texto": "LEGADO"})

    doc = lectura.lee(ruta)

    assert doc.escalon == "cache_ocr"
    assert doc.lectura.texto == "LEGADO"
    assert lectura.verifica_cache(cache)["sin_firma"] == 1


def test_sin_clave_no_se_firma_pero_se_lee(
    carpeta: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(lectura.CLAVE_CACHE_ENV, "")
    lectura.reinicia_clave_cache()
    ruta = pdf_escaneado(carpeta)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(lectura, "_peticion_ocr", lambda *a, **k: respuesta_vision("SIN CLAVE"))
        doc = lectura.lee(ruta)

    guardado = json.loads((cache / f"{doc.sha256}.json").read_text(encoding="utf-8"))
    assert "hmac" not in guardado
    assert lectura.estado_cache()["clave_configurada"] is False
    assert lectura.lee(ruta).escalon == "cache_ocr"


def test_la_firma_no_depende_del_orden_de_las_claves() -> None:
    """El sello cubre el contenido, no el orden en que se escribio el JSON."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(lectura.CLAVE_CACHE_ENV, "clave-de-test")
        lectura.reinicia_clave_cache()
        uno = lectura.firma_entrada({"a": 1, "b": [2, 3]})
        otro = lectura.firma_entrada({"b": [2, 3], "a": 1})
        con_sello = lectura.firma_entrada({"a": 1, "b": [2, 3], "hmac": "loquesea"})
    lectura.reinicia_clave_cache()

    assert uno == otro == con_sello


def test_la_clave_vacia_apaga_la_firma_aunque_el_env_tenga_otra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Variable definida y vacia = firma apagada: no se cae al `.env`."""
    monkeypatch.setenv(lectura.CLAVE_CACHE_ENV, "")
    monkeypatch.setattr(lectura, "_clave_de_fichero_env", lambda: "clave-del-env")
    lectura.reinicia_clave_cache()
    try:
        assert lectura._clave_cache() is None
    finally:
        lectura.reinicia_clave_cache()


# -------------------------------------------------------------- firma motor
def test_firma_motor_vacia_si_el_servicio_no_responde(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lectura, "_FIRMA_MOTOR", None)
    monkeypatch.setattr(lectura.requests, "get", sin_servicio)

    assert lectura.firma_motor() == ""


def test_firma_motor_lee_los_modelos_del_health(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lectura, "_FIRMA_MOTOR", None)
    monkeypatch.setattr(lectura.requests, "get", lambda *a, **k: RespuestaFalsa({
        "runtime": "onnxruntime",
        "engines": {"local": {"models": {
            "det": "PP-OCRv5_mobile", "rec": "PP-OCRv5_mobile", "cls": "PP-OCRv4_mobile",
        }}},
    }))

    assert lectura.firma_motor() == "local:PP-OCRv5_mobile/PP-OCRv5_mobile/PP-OCRv4_mobile"


def test_firma_motor_vacia_si_el_health_no_trae_modelos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lectura, "_FIRMA_MOTOR", None)
    monkeypatch.setattr(lectura.requests, "get", lambda *a, **k: RespuestaFalsa({"engines": {}}))

    assert lectura.firma_motor() == ""


# ------------------------------------------------------------- degradacion
def test_lee_degrada_sin_lanzar_si_el_ocr_esta_caido(
    carpeta: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lectura.requests, "post", sin_servicio)
    ruta = pdf_escaneado(carpeta)

    doc = lectura.lee(ruta)

    assert doc.degradado is True
    assert doc.escalon == "degradado"
    assert doc.proveedor == "ninguno"
    assert doc.error
    # Texto vacio -> `norma` (R6) lo lee como "documento sin texto legible" y escala.
    assert doc.lectura.texto == ""
    assert doc.lectura.texto_ilegible is False
    assert doc.lectura.metodo == "vision_ocr"
    # El vacio no se cachea: cuando vuelva el servicio, se reintenta.
    assert not (cache / f"{doc.sha256}.json").exists()


def test_lee_lote_devuelve_un_documento_por_ruta_en_orden(
    carpeta: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lectura.requests, "post", sin_servicio)
    rutas = [pdf_escaneado(carpeta, f"scan_{i}.pdf", marca=f"FAX{i}") for i in range(4)]

    docs = lectura.lee_lote(rutas, trabajadores=2)

    assert len(docs) == len(rutas)
    assert [doc.lectura.file_id for doc in docs] == [ruta.name for ruta in rutas]
    assert all(doc.degradado for doc in docs)


def test_lee_lote_mezcla_legibles_y_caidas(carpeta: Path, cache: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """Un PDF que revienta no arrastra a los demas ni cambia el orden."""
    monkeypatch.setattr(lectura.requests, "post", sin_servicio)
    legible = pdf_con_capa(carpeta)
    roto = carpeta / "no_es_un_pdf.pdf"
    roto.write_bytes(b"esto no es un PDF")
    escaneada = pdf_escaneado(carpeta)

    docs = lectura.lee_lote([legible, roto, escaneada], trabajadores=3)

    assert [doc.escalon for doc in docs] == ["capa_texto", "degradado", "degradado"]
    assert docs[1].error and docs[1].sha256 == ""


# --------------------------------------------------------------- reintentos
def test_reintentos_agotados_degradan(carpeta: Path, cache: Path,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAISA_OCR_REINTENTOS", "3")
    dormidas: list[float] = []
    monkeypatch.setattr(lectura, "_espera", dormidas.append)
    llamadas: list[int] = []

    def revienta_post(*args: object, **kwargs: object) -> object:
        llamadas.append(1)
        raise ConnectionError("connection refused")

    monkeypatch.setattr(lectura.requests, "post", revienta_post)

    doc = lectura.lee(pdf_escaneado(carpeta))

    assert len(llamadas) == 4  # el intento inicial + 3 reintentos
    assert doc.reintentos == 3
    assert dormidas == [0.5, 1.0, 2.0]  # backoff exponencial
    assert doc.degradado is True


def test_el_backoff_tiene_tope(carpeta: Path, cache: Path,
                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAISA_OCR_REINTENTOS", "6")
    dormidas: list[float] = []
    monkeypatch.setattr(lectura, "_espera", dormidas.append)
    monkeypatch.setattr(lectura.requests, "post", sin_servicio)

    lectura.lee(pdf_escaneado(carpeta))

    assert dormidas == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0]


def test_el_5xx_se_reintenta_y_el_4xx_no(carpeta: Path, cache: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAISA_OCR_REINTENTOS", "2")
    llamadas: list[int] = []

    def responde_con(codigo: int):
        def post(*args: object, **kwargs: object) -> RespuestaFalsa:
            llamadas.append(1)
            return RespuestaFalsa({}, codigo=codigo)

        return post

    monkeypatch.setattr(lectura.requests, "post", responde_con(503))
    doc = lectura.lee(pdf_escaneado(carpeta, "a.pdf", marca="FAX A"))

    assert len(llamadas) == 3  # el intento inicial + 2 reintentos
    assert doc.degradado is True
    assert "503" in doc.error

    llamadas.clear()
    monkeypatch.setattr(lectura.requests, "post", responde_con(400))
    doc = lectura.lee(pdf_escaneado(carpeta, "b.pdf", marca="FAX B"))

    assert len(llamadas) == 1  # el 4xx no se reintenta
    assert doc.reintentos == 0
    assert doc.degradado is True


def test_un_200_sin_texto_no_se_reintenta(carpeta: Path, cache: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAISA_OCR_REINTENTOS", "4")
    llamadas: list[int] = []

    def responde_vacio(*args: object, **kwargs: object) -> RespuestaFalsa:
        llamadas.append(1)
        return RespuestaFalsa({"results": [{"text": ""}]})

    monkeypatch.setattr(lectura.requests, "post", responde_vacio)

    doc = lectura.lee(pdf_escaneado(carpeta))

    assert len(llamadas) == 1
    assert doc.degradado is True


# ------------------------------------------------------------------- nube
def test_la_nube_se_adopta_solo_si_mejora(carpeta: Path, cache: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAISA_OCR_NUBE", "1")
    monkeypatch.setenv("MAISA_OCR_NUBE_MAX", "5")
    llamadas: list[str] = []

    def peticion(ruta: Path, motor: str, timeout: float, conexion: float) -> dict:
        llamadas.append(motor)
        if motor == "cloud":
            return respuesta_vision("\n".join(LINEAS_FACTURA))
        return respuesta_vision("nada legible")

    monkeypatch.setattr(lectura, "_peticion_ocr", peticion)

    doc = lectura.lee(pdf_escaneado(carpeta))

    assert llamadas == ["local", "cloud"]
    assert doc.escalon == "vision_nube"
    assert doc.nube is True
    assert doc.proveedor == "nube"
    assert doc.degradado is False
    assert doc.calidad >= 0.6
    assert "FACTURA 2026/11604" in doc.lectura.texto
    # La cache guarda el peldano que gano.
    guardado = json.loads((cache / f"{doc.sha256}.json").read_text(encoding="utf-8"))
    assert guardado["escalon"] == "vision_nube"


def test_la_nube_que_no_mejora_no_sustituye_al_local(carpeta: Path, cache: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAISA_OCR_NUBE", "1")
    llamadas: list[str] = []

    def peticion(ruta: Path, motor: str, timeout: float, conexion: float) -> dict:
        llamadas.append(motor)
        return respuesta_vision("TOTAL 10,00 EUR" if motor == "local" else "x")

    monkeypatch.setattr(lectura, "_peticion_ocr", peticion)

    doc = lectura.lee(pdf_escaneado(carpeta))

    assert llamadas == ["local", "cloud"]
    assert doc.escalon == "vision_ocr"
    assert doc.nube is False
    assert doc.lectura.texto == "TOTAL 10,00 EUR"
    assert "no mejoro" in doc.error


def test_el_presupuesto_de_nube_se_agota_y_no_falla(carpeta: Path, cache: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAISA_OCR_NUBE", "1")
    monkeypatch.setenv("MAISA_OCR_NUBE_MAX", "1")
    llamadas: list[str] = []

    def peticion(ruta: Path, motor: str, timeout: float, conexion: float) -> dict:
        llamadas.append(motor)
        return respuesta_vision("TOTAL 10,00 EUR" if motor == "local" else "x")

    monkeypatch.setattr(lectura, "_peticion_ocr", peticion)
    rutas = [pdf_escaneado(carpeta, f"factura_{i}.pdf", marca=f"FAX {i}") for i in range(3)]

    primera, segunda = lectura.lee(rutas[0]), lectura.lee(rutas[1])
    assert llamadas.count("cloud") == 1
    assert primera.escalon == "vision_ocr"
    assert segunda.escalon == "vision_ocr"  # se queda con lo local
    assert "presupuesto" in segunda.error
    assert segunda.degradado is False

    lectura.reinicia_presupuesto_nube()
    tercera = lectura.lee(rutas[2])
    assert llamadas.count("cloud") == 2
    assert tercera.escalon == "vision_ocr"


def test_el_parametro_nube_manda_sobre_la_variable(carpeta: Path, cache: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    llamadas: list[str] = []

    def peticion(ruta: Path, motor: str, timeout: float, conexion: float) -> dict:
        llamadas.append(motor)
        return respuesta_vision("nada" if motor == "local" else "\n".join(LINEAS_FACTURA))

    monkeypatch.setattr(lectura, "_peticion_ocr", peticion)
    monkeypatch.delenv("MAISA_OCR_NUBE", raising=False)

    forzada = lectura.lee(pdf_escaneado(carpeta, "uno.pdf", marca="FAX 1"), nube=True)
    assert forzada.escalon == "vision_nube"

    monkeypatch.setenv("MAISA_OCR_NUBE", "1")
    apagada = lectura.lee(pdf_escaneado(carpeta, "dos.pdf", marca="FAX 2"), nube=False)
    assert apagada.escalon == "vision_ocr"
    assert llamadas == ["local", "cloud", "local"]


def test_una_nube_caida_no_pierde_lo_local(carpeta: Path, cache: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAISA_OCR_NUBE", "1")

    def peticion(ruta: Path, motor: str, timeout: float, conexion: float) -> dict:
        if motor == "cloud":
            raise lectura.OcrNoDisponible("la nube no responde")
        return respuesta_vision("TOTAL 10,00 EUR")

    monkeypatch.setattr(lectura, "_peticion_ocr", peticion)

    doc = lectura.lee(pdf_escaneado(carpeta))

    assert doc.escalon == "vision_ocr"
    assert doc.nube is False
    assert doc.lectura.texto == "TOTAL 10,00 EUR"
    assert "nube" in doc.error


# ------------------------------------------------------------ capa de texto
def test_la_capa_de_texto_buena_no_toca_la_red(carpeta: Path, cache: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lectura.requests, "post", revienta)
    monkeypatch.setattr(lectura.requests, "get", revienta)

    doc = lectura.lee(pdf_con_capa(carpeta))

    assert doc.escalon == "capa_texto"
    assert doc.cache is False
    assert doc.paginas_ocr == 0
    assert doc.proveedor == "ninguno"
    assert "TOTAL: 3.012,89 EUR" in doc.lectura.texto
    assert doc.lectura.metodo == "texto_determinista"


def test_umbral_de_calidad_obliga_a_bajar_al_ocr(carpeta: Path, cache: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lectura, "_peticion_ocr", lambda *a, **k: respuesta_vision("OCR"))

    doc = lectura.lee(pdf_con_capa(carpeta), umbral_calidad=1.1)

    assert doc.escalon == "vision_ocr"
    assert doc.paginas_ocr == 1


# -------------------------------------------------------------- compatibilidad
def test_calidad_texto_mantiene_su_comportamiento() -> None:
    assert lectura.calidad_texto("", 1) == 0.0
    assert lectura.calidad_texto("   \n ", 2) == 0.0
    # Capa de texto escasa: por debajo del umbral aunque "parezca" texto.
    assert lectura.calidad_texto("FAX", 1) <= 0.5
    # La misma escasez repartida en mas paginas no sube la calidad.
    assert lectura.calidad_texto("FAX", 3) <= lectura.calidad_texto("FAX", 1)
    # Factura completa: por encima del umbral por defecto.
    assert lectura.calidad_texto("\n".join(LINEAS_FACTURA), 1) >= 0.6
    # Sin importe ni palabra clave, se sospecha.
    assert lectura.calidad_texto("relleno " * 40, 1) < lectura.calidad_texto(
        "\n".join(LINEAS_FACTURA), 1
    )


def test_sha256_pdf_depende_del_contenido_y_no_del_nombre(carpeta: Path) -> None:
    datos = pdf_con_texto([["FACTURA"]])
    uno, otro = carpeta / "uno.pdf", carpeta / "otro.pdf"
    uno.write_bytes(datos)
    otro.write_bytes(datos)

    assert lectura.sha256_pdf(uno) == lectura.sha256_pdf(otro)
    assert lectura.sha256_pdf(uno) == hashlib.sha256(datos).hexdigest()
    assert len(lectura.sha256_pdf(uno)) == 64


def test_documento_conserva_los_campos_de_siempre() -> None:
    """Los campos nuevos tienen valor por defecto: nada de lo anterior se rompe."""
    doc = lectura.Documento(
        lectura=texto.extrae("FACTURA", "x.pdf", 1, "texto_determinista", ""),
        sha256="0" * 64, escalon="capa_texto", cache=False, segundos=0.1, calidad=1.0,
    )

    assert (doc.motor, doc.proveedor, doc.paginas_ocr) == ("", "", 0)
    assert (doc.reintentos, doc.degradado, doc.error, doc.nube) == (0, False, "", False)
    assert lectura.OCR_URL.startswith("http")
    assert lectura._ocr_url().startswith("http")


# ------------------------------------------- la calidad que viaja en la traza
def _doc(escalon: str, calidad: float) -> lectura.Documento:
    return lectura.Documento(
        lectura=texto.extrae("FACTURA", "x.pdf", 1, "texto_determinista", ""),
        sha256="0" * 64, escalon=escalon, cache=False, segundos=0.1, calidad=calidad,
    )


def test_la_calidad_de_la_traza_solo_sale_de_la_capa_de_texto() -> None:
    """`calidad_texto` mide la capa de texto, no el texto que se acabo usando.

    En un escaneo el motor cae al OCR y `doc.calidad` es el 0.0 de la capa que
    se descarto, no la calidad de lo que se leyo. Publicarlo en la traza hacia
    parecer que los escaneos se leen fatal (media 0.741 en ESCALAR frente a
    0.950 en PAGAR) cuando su OCR es indistinguible del texto embebido (0.9933
    frente a 0.9903). Sin medida, la traza publica `None` en vez de un cero que
    nadie ha medido.
    """
    assert procesa._calidad_medida(_doc("capa_texto", 0.123456)) == 0.1235
    for escalon in ("cache_ocr", "vision_ocr", "vision_nube", "degradado"):
        assert procesa._calidad_medida(_doc(escalon, 0.0)) is None


def test_no_se_usa_la_calidad_del_texto_ocr_como_sustituto() -> None:
    """El sustituto evidente miente al alza y no arregla la metrica.

    `calidad_texto` responde "esto son letras imprimibles y hay un total", que
    es justo lo que garantiza un OCR: devuelve 1.0 con un documento destrozado.
    Cambiar un 0.0 falso por un 1.0 falso no es medir, asi que la traza declara
    que no hay medida en vez de rellenarla con esto.
    """
    destrozado = "\n".join([
        "FACTURA 2026/11604",
        "Suministros Levante S.L. NIF:B9023341",
        "CuentdeabOno (BAN En1 Sro0 015",
        "Pedido PO-206-0724  Fecha 05/01/2026",
        "Base 2489.99  IVA (21%) 522.90  TOTAL 3012.89",
        "Cliente Banco Miralmar S.A. CIF A58231074",
    ])
    assert lectura.calidad_texto(destrozado, 1) == 1.0
    assert procesa._calidad_medida(_doc("cache_ocr", 0.0)) is None


# ------------------------------------------------------------------- lento
@pytest.mark.lento
def test_escalera_sobre_el_corpus_real(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recorre los 500 PDFs de verdad (necesita cache o contenedor de vision)."""
    rutas = sorted(procesa.FACTURAS_POR_DEFECTO.glob("*.pdf"))
    if not rutas:
        pytest.skip("no esta el corpus")
    # El test solo lee: la cache del repo no se toca.
    monkeypatch.setattr(lectura, "_escribe_cache", lambda *a, **k: None)

    docs = lectura.lee_lote(rutas)

    assert len(docs) == len(rutas)
    assert [doc.lectura.file_id for doc in docs] == [ruta.name for ruta in rutas]
    escalones = {"capa_texto", "cache_ocr", "vision_ocr", "vision_nube", "degradado"}
    assert {doc.escalon for doc in docs} <= escalones
    assert sum(1 for doc in docs if doc.degradado) == 0


@pytest.mark.lento
def test_un_escaneo_real_del_corpus() -> None:
    from conftest import exige_lectura

    doc = exige_lectura("scan_001.pdf")

    assert doc.escalon in ("cache_ocr", "vision_ocr")
    assert doc.lectura.texto.strip()
    assert doc.calidad < 0.6


@pytest.mark.lento
def test_firma_motor_con_el_contenedor() -> None:
    firma = lectura.firma_motor()
    if not firma:
        pytest.skip("no hay contenedor de vision en MAISA_OCR_URL")

    assert firma.startswith("local:")
