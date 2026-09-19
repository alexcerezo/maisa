# -*- coding: utf-8 -*-
"""Tests del contrato de entrega JSONL. Solo stdlib (unittest + subprocess).

Se lanza el validador como subproceso real para comprobar codigos de salida y
la salida impresa de verdad. Los fixtures se crean en un directorio temporal,
nunca dentro del repositorio.

Ejecucion (desde hackspain-ocr):
    python -m unittest tests.test_validate_jsonl -v
    python tests\\test_validate_jsonl.py -v
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
VALIDADOR = RAIZ / "validate_jsonl.py"


class BaseValidador(unittest.TestCase):
    """Directorio temporal con un directorio de PDFs y un JSONL de entrega."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.pdfs = self.dir / "pdfs"
        self.pdfs.mkdir()
        self.jsonl = self.dir / "entrega.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    # --- utilidades ------------------------------------------------------
    def crear_pdf(self, nombre):
        (self.pdfs / nombre).write_bytes(b"%PDF-1.4\n")

    def escribir(self, contenido):
        if isinstance(contenido, bytes):
            self.jsonl.write_bytes(contenido)
        else:
            self.jsonl.write_bytes(contenido.encode("utf-8"))
        return self.jsonl

    def escribir_objetos(self, *objetos, salto_final=True):
        texto = "\n".join(json.dumps(obj) for obj in objetos)
        if salto_final:
            texto += "\n"
        return self.escribir(texto)

    def ejecutar(self, ruta=None, pdf_dir=None, quiet=False):
        comando = [
            sys.executable,
            str(VALIDADOR),
            str(ruta if ruta is not None else self.jsonl),
            "--pdf-dir",
            str(pdf_dir if pdf_dir is not None else self.pdfs),
        ]
        if quiet:
            comando.append("--quiet")
        return subprocess.run(
            comando,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(RAIZ),
        )

    def asertar_fallo(self, proceso, fragmento):
        """Fallo = codigo 1 y el mensaje esperado en stdout."""
        self.assertEqual(proceso.returncode, 1, f"stdout={proceso.stdout!r}")
        self.assertIn(fragmento, proceso.stdout)


class TestEntregaValida(BaseValidador):
    def test_entrega_valida_exit_0(self):
        for nombre in ("factura_1.pdf", "factura_2.pdf", "factura_3.pdf"):
            self.crear_pdf(nombre)
        self.escribir_objetos(
            {"file_id": "factura_1.pdf", "result": "PAGAR"},
            {"file_id": "factura_2.pdf", "result": "NO_PAGAR"},
            {"file_id": "factura_3.pdf", "result": "ESCALAR"},
        )
        proceso = self.ejecutar()
        self.assertEqual(proceso.returncode, 0, f"stdout={proceso.stdout!r}")
        self.assertIn("OK", proceso.stdout)

    def test_fichero_vacio_con_directorio_vacio_es_valido(self):
        self.escribir("")
        proceso = self.ejecutar()
        self.assertEqual(proceso.returncode, 0, f"stdout={proceso.stdout!r}")

    def test_sin_salto_de_linea_final_es_valido(self):
        self.crear_pdf("factura_1.pdf")
        self.escribir_objetos({"file_id": "factura_1.pdf", "result": "PAGAR"}, salto_final=False)
        proceso = self.ejecutar()
        self.assertEqual(proceso.returncode, 0, f"stdout={proceso.stdout!r}")


class TestFormatoFichero(BaseValidador):
    """Checks 1 (UTF-8), 2 (BOM) y CRLF."""

    def test_bytes_no_utf8_es_error(self):
        self.escribir(b'{"file_id": "a.pdf", "result": "PAGAR", "x": "\xff\xfe"}')
        self.asertar_fallo(self.ejecutar(), "no es UTF-8 valido")

    def test_bom_al_inicio_es_error(self):
        valido = '{"file_id": "a.pdf", "result": "PAGAR"}\n'
        self.escribir(b"\xef\xbb\xbf" + valido.encode("utf-8"))
        self.asertar_fallo(self.ejecutar(), "BOM")

    def test_crlf_es_aviso_no_error(self):
        self.crear_pdf("factura_1.pdf")
        texto = '{"file_id": "factura_1.pdf", "result": "PAGAR"}\r\n'
        self.escribir(texto)
        proceso = self.ejecutar()
        self.assertEqual(proceso.returncode, 0, f"stdout={proceso.stdout!r}")
        self.assertIn("CRLF", proceso.stdout)


class TestEstructuraJson(BaseValidador):
    """Checks 3 (objeto JSON) y 11 (lineas vacias)."""

    def test_linea_no_objeto_json_es_error(self):
        self.escribir('[{"file_id": "a.pdf", "result": "PAGAR"}]\n')
        self.asertar_fallo(self.ejecutar(), "no es un objeto JSON")

    def test_json_invalido_es_error(self):
        self.escribir('{"file_id": "a.pdf", "result": "PAGAR"\n')
        self.asertar_fallo(self.ejecutar(), "no es JSON valido")

    def test_linea_vacia_en_medio_es_error(self):
        self.crear_pdf("a.pdf")
        self.escribir('{"file_id": "a.pdf", "result": "PAGAR"}\n\n')
        self.asertar_fallo(self.ejecutar(), "linea vacia")

    def test_linea_solo_espacios_es_error(self):
        self.crear_pdf("a.pdf")
        self.escribir('{"file_id": "a.pdf", "result": "PAGAR"}\n   \n')
        self.asertar_fallo(self.ejecutar(), "linea vacia")


class TestClaves(BaseValidador):
    """Checks 4, 5, 6 y 9, y los campos extra (12)."""

    def test_falta_result_es_error(self):
        self.escribir_objetos({"file_id": "a.pdf"})
        self.asertar_fallo(self.ejecutar(), "falta la clave 'result'")

    def test_clave_resultado_menciona_ambas_claves(self):
        self.escribir_objetos({"file_id": "a.pdf", "resultado": "PAGAR"})
        proceso = self.ejecutar()
        self.asertar_fallo(proceso, "result")
        self.assertIn("resultado", proceso.stdout)
        self.assertIn("'result'", proceso.stdout)

    def test_result_fuera_del_conjunto_es_error(self):
        self.crear_pdf("a.pdf")
        self.escribir_objetos({"file_id": "a.pdf", "result": "QUIZAS"})
        self.asertar_fallo(self.ejecutar(), "no es uno de")

    def test_result_en_minusculas_es_error(self):
        self.crear_pdf("a.pdf")
        self.escribir_objetos({"file_id": "a.pdf", "result": "pagar"})
        self.asertar_fallo(self.ejecutar(), "'result' = 'pagar'")

    def test_falta_file_id_es_error(self):
        self.escribir_objetos({"result": "PAGAR"})
        self.asertar_fallo(self.ejecutar(), "falta la clave 'file_id'")

    def test_file_id_vacio_o_no_texto_es_error(self):
        self.escribir_objetos(
            {"file_id": "", "result": "PAGAR"},
            {"file_id": 12, "result": "PAGAR"},
        )
        self.asertar_fallo(self.ejecutar(), "'file_id' debe ser una cadena no vacia")

    def test_file_id_con_separador_de_ruta_es_error(self):
        self.escribir_objetos(
            {"file_id": "sub/a.pdf", "result": "PAGAR"},
            {"file_id": "sub\\b.pdf", "result": "PAGAR"},
        )
        self.asertar_fallo(self.ejecutar(), "no debe contener rutas")

    def test_campos_extra_son_solo_avisos(self):
        self.crear_pdf("a.pdf")
        self.escribir_objetos(
            {"file_id": "a.pdf", "result": "PAGAR", "importe": 12.5, "motivo": "x", "pedido": 7}
        )
        proceso = self.ejecutar()
        self.assertEqual(proceso.returncode, 0, f"stdout={proceso.stdout!r}")
        self.assertIn("campos de traza extra", proceso.stdout)
        self.assertNotIn("ERRORES", proceso.stdout)


class TestUnicidadYCobertura(BaseValidador):
    """Checks 7, 8, 10 y file_id sin PDF real."""

    def test_file_id_repetido_es_error(self):
        self.crear_pdf("a.pdf")
        self.escribir_objetos(
            {"file_id": "a.pdf", "result": "PAGAR"},
            {"file_id": "a.pdf", "result": "NO_PAGAR"},
        )
        self.asertar_fallo(self.ejecutar(), "repetido")

    def test_file_id_inexistente_es_error(self):
        self.escribir_objetos({"file_id": "fantasma.pdf", "result": "PAGAR"})
        self.asertar_fallo(self.ejecutar(), "no es un PDF del directorio")

    def test_case_mismatch_detected_on_windows(self):
        self.crear_pdf("factura_1.pdf")
        self.escribir_objetos({"file_id": "FACTURA_1.PDF", "result": "PAGAR"})
        self.asertar_fallo(self.ejecutar(), "el fichero real es 'factura_1.pdf'")

    def test_pdf_con_extension_en_mayusculas_coincide_exacto(self):
        self.crear_pdf("FACTURA_9.PDF")
        self.escribir_objetos({"file_id": "FACTURA_9.PDF", "result": "PAGAR"})
        proceso = self.ejecutar()
        self.assertEqual(proceso.returncode, 0, f"stdout={proceso.stdout!r}")

    def test_extension_en_minusculas_no_coincide_con_pdf_mayusculas(self):
        self.crear_pdf("FACTURA_9.PDF")
        self.escribir_objetos({"file_id": "FACTURA_9.pdf", "result": "PAGAR"})
        self.asertar_fallo(self.ejecutar(), "el fichero real es 'FACTURA_9.PDF'")

    def test_pdf_sin_linea_es_error(self):
        self.crear_pdf("a.pdf")
        self.crear_pdf("b.pdf")
        self.escribir_objetos({"file_id": "a.pdf", "result": "PAGAR"})
        self.asertar_fallo(self.ejecutar(), "el PDF 'b.pdf' no tiene ninguna linea")


class TestInforme(BaseValidador):
    def test_mensajes_identicos_se_agrupan(self):
        self.crear_pdf("a.pdf")
        lineas = [json.dumps({"file_id": "a.pdf", "result": "DUDA"}) for _ in range(500)]
        self.escribir("\n".join(lineas) + "\n")
        proceso = self.ejecutar()
        self.assertEqual(proceso.returncode, 1, f"stdout={proceso.stdout!r}")
        salida = proceso.stdout.splitlines()
        self.assertLess(len(salida), 15, f"salida no agrupada:\n{proceso.stdout}")
        self.assertIn("veces", proceso.stdout)
        self.assertIn("[500 veces]", proceso.stdout)

    def test_quiet_oculta_avisos_pero_mantiene_exit_0(self):
        self.crear_pdf("a.pdf")
        self.escribir_objetos({"file_id": "a.pdf", "result": "PAGAR", "importe": 1})
        proceso = self.ejecutar(quiet=True)
        self.assertEqual(proceso.returncode, 0, f"stdout={proceso.stdout!r}")
        self.assertNotIn("AVISOS", proceso.stdout)
        self.assertIn("OK", proceso.stdout)


class TestRobustezSalida(BaseValidador):
    """Un mensaje con caracteres fuera del codec de la consola no debe romper."""

    def test_nombre_no_codificable_en_consola_no_rompe_el_informe(self):
        self.crear_pdf("factura_\u65e5\u672c.pdf")
        self.escribir_objetos({"file_id": "factura_\u65e5\u672cX.pdf", "result": "PAGAR"})
        proceso = self.ejecutar()
        self.assertEqual(proceso.returncode, 1, f"stdout={proceso.stdout!r}")
        self.assertNotIn("Traceback", proceso.stderr)
        self.assertIn("no es un PDF del directorio", proceso.stdout)
        self.assertIn("FALLO", proceso.stdout)

    def test_pdf_con_nombre_no_codificable_aparece_en_la_cobertura(self):
        self.crear_pdf("factura_\u65e5\u672c.pdf")
        self.escribir("")
        proceso = self.ejecutar()
        self.assertEqual(proceso.returncode, 1, f"stdout={proceso.stdout!r}")
        self.assertNotIn("Traceback", proceso.stderr)
        self.assertIn("no tiene ninguna linea", proceso.stdout)


class TestEntradasInexistentes(BaseValidador):
    def test_jsonl_inexistente_devuelve_2(self):
        proceso = self.ejecutar(ruta=self.dir / "no_existe.jsonl")
        self.assertEqual(proceso.returncode, 2, f"stdout={proceso.stdout!r}")

    def test_directorio_pdf_inexistente_devuelve_2(self):
        self.escribir("")
        proceso = self.ejecutar(pdf_dir=self.dir / "no_existe")
        self.assertEqual(proceso.returncode, 2, f"stdout={proceso.stdout!r}")


if __name__ == "__main__":
    unittest.main()
