"""Convierte el plan en markdown a PDF para la entrega (`albertitos_plan.pdf`).

La entrega exige un PDF con dos secciones (Arquitectura y ADRs / trade-offs).
El contenido vive en `albertitos_plan.md` porque un markdown se revisa y se
versiona; el PDF es solo la exportacion. Este script mantiene las dos cosas
alineadas: si el markdown cambia, el PDF se regenera con un comando.

Uso:
    .venv/bin/python maisa/tools/md_a_pdf.py albertitos_plan.md salida.pdf
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import markdown
from fpdf import FPDF
from fpdf.enums import XPos, YPos
from fpdf.fonts import FontFace
from fpdf.html import HTMLMixin, TextStyle

FUENTES = Path("/usr/share/fonts/truetype/dejavu")

MONO = "DejaVuMono"
CUERPO = "DejaVu"

TITULO = "Albertitos · Plan de Entrega"
SUBTITULO = "HackSpain 2026 · Track Maisa · Equipo YEM9Q8TP"


class Plan(FPDF, HTMLMixin):
    """A4, tipografia DejaVu (cubre acentos y dibujos de caja) y pie numerado."""

    def __init__(self) -> None:
        super().__init__(orientation="P", unit="mm", format="A4")
        self.set_margins(18, 18, 18)
        self.set_auto_page_break(auto=True, margin=20)
        self.add_font(CUERPO, "", str(FUENTES / "DejaVuSans.ttf"))
        self.add_font(CUERPO, "B", str(FUENTES / "DejaVuSans-Bold.ttf"))
        self.add_font(CUERPO, "I", str(FUENTES / "DejaVuSans-Oblique.ttf"))
        self.add_font(CUERPO, "BI", str(FUENTES / "DejaVuSans-BoldOblique.ttf"))
        self.add_font(MONO, "", str(FUENTES / "DejaVuSansMono.ttf"))
        self.add_font(MONO, "B", str(FUENTES / "DejaVuSansMono-Bold.ttf"))
        self.set_font(CUERPO, size=10)

    def header(self) -> None:
        if self.page_no() == 1:
            return
        self.set_font(CUERPO, size=8)
        self.set_text_color(120)
        self.cell(0, 6, TITULO, align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_text_color(0)
        self.set_y(max(self.get_y(), 16))

    def footer(self) -> None:
        self.set_y(-14)
        self.set_font(CUERPO, size=8)
        self.set_text_color(120)
        self.cell(0, 6, f"pagina {self.page_no()}", align="C")
        self.set_text_color(0)

    def portada(self) -> None:
        self.add_page()
        self.set_font(CUERPO, "B", 22)
        self.ln(30)
        self.multi_cell(0, 11, TITULO, align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_font(CUERPO, size=11)
        self.set_text_color(90)
        self.multi_cell(0, 7, SUBTITULO, align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_text_color(0)
        self.set_font(CUERPO, size=9)
        self.ln(6)
        self.multi_cell(
            0, 5,
            "Secciones: Arquitectura (componentes, flujo de datos, reparto entre "
            "agentes, modelos y personas, observabilidad y recuperacion) y "
            "ADRs / trade-offs (contexto, alternativas, decision, consecuencias "
            "aceptadas y evidencia).",
            align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT,
        )


def estilo() -> dict:
    """Estilos por etiqueta. `write_html` exige FontFace o TextStyle, no dicts."""
    def t(**kw):
        return TextStyle(**kw)

    def f(**kw):
        return FontFace(**kw)

    return {
        "h1": t(font_family=CUERPO, font_size_pt=17, font_style="B", color=(17, 40, 70),
                t_margin=6, b_margin=3),
        "h2": t(font_family=CUERPO, font_size_pt=13, font_style="B", color=(17, 40, 70),
                t_margin=6, b_margin=2),
        "h3": t(font_family=CUERPO, font_size_pt=11, font_style="B", color=(40, 60, 90),
                t_margin=5, b_margin=2),
        "h4": t(font_family=CUERPO, font_size_pt=10, font_style="B", t_margin=4, b_margin=1),
        "p": t(font_family=CUERPO, font_size_pt=9.5, t_margin=0, b_margin=3),
        "li": t(font_family=CUERPO, font_size_pt=9.5, t_margin=0, b_margin=1),
        "code": f(family=MONO, size_pt=7.5),
        "a": f(family=CUERPO, size_pt=9.5, color=(0, 70, 150)),
        "b": f(family=CUERPO, emphasis="BOLD"),
        "strong": f(family=CUERPO, emphasis="BOLD"),
        "em": f(family=CUERPO, emphasis="ITALICS"),
        "i": f(family=CUERPO, emphasis="ITALICS"),
    }


_RE_CELDA = re.compile(r"<(th|td)([^>]*)>(.*?)</\1>", re.S)
_RE_ETIQUETA = re.compile(r"</?(?:strong|em|b|i|code|span|a)\b[^>]*>")


def limpia_tablas(html: str) -> str:
    """`write_html` no admite etiquetas anidadas dentro de celdas.

    markdown deja `**negrita**` como `<strong>` en las tablas; aqui se aplana a
    texto para que el PDF salga, que es lo que importa en la entrega.
    """
    def aplana(m: re.Match) -> str:
        return f"<{m.group(1)}{m.group(2)}>{_RE_ETIQUETA.sub('', m.group(3))}</{m.group(1)}>"

    return _RE_CELDA.sub(aplana, html)


def convierte(md: Path, pdf: Path) -> tuple[int, int]:
    html = markdown.markdown(
        md.read_text(encoding="utf-8"),
        extensions=["tables", "fenced_code", "sane_lists", "attr_list"],
    )
    html = limpia_tablas(html)
    doc = Plan()
    doc.portada()
    doc.add_page()
    doc.write_html(html, tag_styles=estilo(), li_tag_indent=5,
                   ul_bullet_char="\u2022")
    pdf.parent.mkdir(parents=True, exist_ok=True)
    doc.output(str(pdf))
    return doc.page_no(), len(html)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    md, pdf = Path(argv[1]), Path(argv[2])
    paginas, chars = convierte(md, pdf)
    print(f"{md} -> {pdf}  ({paginas} paginas, {chars} chars de HTML, "
          f"{pdf.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
