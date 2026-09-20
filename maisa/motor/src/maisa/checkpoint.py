"""Checkpoint del lote: lo que permite reanudar sin repetir la lectura.

El lote de 500 facturas tarda segundos, pero el de un millon tarda horas y el
OCR en frio es lo unico que no se puede repetir barato: la cache se firma por
sha256, asi que un lote reanudado solo paga las escaneadas que le faltan. Este
fichero guarda, documento a documento y con ``flush`` por linea, la lectura
**completa** y no solo la entrega:

- la decision mira el texto literal (``divisas_declaradas``, ``extrae_iva_pct``
  en ``norma.py``), asi que el checkpoint guarda tambien ``texto``;
- la regla de pedidos repetidos mira el lote entero, asi que reanudar tiene que
  decidir con los documentos de la pasada anterior delante, no solo con los que
  quedaban. Si no, una factura reanudada se quedaria sin marcar su duplicado y
  la entrega no seria la misma que la de una pasada entera.

El fichero se escribe en **apendice** y se borra cuando la entrega termina
validada: un checkpoint vivo es la senal de que la pasada anterior no acabo.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import emit
from .lectura import Documento

#: Cada cuantas lineas se fuerza el ``fsync``. Con ``flush`` por linea el
#: proceso que muere no pierde nada; el ``fsync`` protege de un corte de la
#: maquina, y pagarlo por documento costaria mas que la lectura de un PDF de
#: capa de texto (0,007 s). Medido: 500 documentos -> 8 ``fsync``.
CADA_FSYNC = 64


class Checkpoint:
    """Lecturas ya hechas de un lote, en apendice y reanudables."""

    def __init__(self, ruta: Path) -> None:
        self.ruta = ruta
        self.leidos: dict[str, Documento] = {}
        self.lineas_rotas = 0
        self._fh = None
        self._desde_fsync = 0
        self._carga()

    # ------------------------------------------------------------- lectura
    def _carga(self) -> None:
        """Indexa lo ya leido por `file_id` normalizado.

        Una linea a medias es exactamente el sintoma que este fichero viene a
        resolver (el proceso murio escribiendo), asi que se descarta en vez de
        tumbar la reanudacion.
        """
        if not self.ruta.exists():
            return
        for linea in self.ruta.read_text(encoding="utf-8").splitlines():
            if not linea.strip():
                continue
            try:
                datos = json.loads(linea)
            except json.JSONDecodeError:
                self.lineas_rotas += 1
                continue
            if not isinstance(datos, dict):
                self.lineas_rotas += 1
                continue
            documento = Documento.desde_dict(datos)
            if documento.lectura.file_id:
                self.leidos[emit.normaliza_file_id(documento.lectura.file_id)] = documento

    def tiene(self, pdf: Path) -> bool:
        return emit.normaliza_file_id(pdf.name) in self.leidos

    def de(self, pdf: Path) -> Documento | None:
        return self.leidos.get(emit.normaliza_file_id(pdf.name))

    def reparte(self, pdfs: list[Path]) -> tuple[list[Documento], list[Path]]:
        """(ya leidos en el orden del lote, PDFs que faltan por leer)."""
        hechos: list[Documento] = []
        faltan: list[Path] = []
        for pdf in pdfs:
            documento = self.de(pdf)
            if documento is None:
                faltan.append(pdf)
            else:
                hechos.append(documento)
        return hechos, faltan

    # ------------------------------------------------------------ escritura
    def anota(self, documento: Documento) -> None:
        """Apunta un documento leido: apendice, ``flush`` y ``fsync`` periodico."""
        if self._fh is None:
            self.ruta.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.ruta.open("a", encoding="utf-8", newline="\n")
        self._fh.write(json.dumps(documento.como_dict(), ensure_ascii=False) + "\n")
        self._fh.flush()
        self.leidos[emit.normaliza_file_id(documento.lectura.file_id)] = documento
        self._desde_fsync += 1
        if self._desde_fsync >= CADA_FSYNC:
            os.fsync(self._fh.fileno())
            self._desde_fsync = 0

    def cierra(self) -> None:
        """Cierra el fichero (con ``fsync`` de lo que quede sin sincronizar)."""
        if self._fh is None:
            return
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        self._fh = None
        self._desde_fsync = 0

    def borra(self) -> None:
        """Retira el checkpoint: la entrega ya esta completa y validada."""
        self.cierra()
        self.ruta.unlink(missing_ok=True)

    def __enter__(self) -> "Checkpoint":
        return self

    def __exit__(self, *_: object) -> None:
        self.cierra()
