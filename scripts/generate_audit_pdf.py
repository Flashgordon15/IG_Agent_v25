#!/usr/bin/env python3
"""Generate PDF from LIVE_OPERATIONS_PIPELINE_AUDIT.md for Copilot handoff."""

from __future__ import annotations

import re
from pathlib import Path

from fpdf import FPDF

ROOT = Path(__file__).resolve().parents[1]
MD_PATH = ROOT / "docs" / "LIVE_OPERATIONS_PIPELINE_AUDIT.md"
PDF_PATH = ROOT / "docs" / "LIVE_OPERATIONS_PIPELINE_AUDIT.pdf"


class AuditPDF(FPDF):
    def header(self) -> None:
        if self.page_no() == 1:
            return
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(100, 100, 100)
        self.cell(0, 8, _sanitize("IG Agent v29.1 - Live Operations Pipeline Audit"), align="L")
        self.ln(4)

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 8, f"Page {self.page_no()}", align="C")


def _sanitize(text: str) -> str:
    """Keep fpdf core-font safe; replace common unicode."""
    replacements = {
        "\u2014": "-",
        "\u2013": "-",
        "\u2192": "->",
        "\u2190": "<-",
        "\u2022": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u00a0": " ",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    return text.encode("latin-1", errors="replace").decode("latin-1")


def _write_heading(pdf: AuditPDF, line: str, level: int) -> None:
    sizes = {1: 16, 2: 13, 3: 11}
    pdf.ln(2 if level > 1 else 4)
    pdf.set_x(pdf.l_margin)
    pdf.set_font("Helvetica", "B", sizes.get(level, 11))
    pdf.set_text_color(20, 20, 40)
    pdf.multi_cell(0, 7, _sanitize(line))
    pdf.ln(1)


def _write_paragraph(pdf: AuditPDF, text: str, *, bold: bool = False) -> None:
    pdf.set_x(pdf.l_margin)
    pdf.set_font("Helvetica", "B" if bold else "", 10)
    pdf.set_text_color(30, 30, 30)
    pdf.multi_cell(0, 5, _sanitize(text))
    pdf.ln(1)


def _write_code_block(pdf: AuditPDF, lines: list[str]) -> None:
    pdf.set_x(pdf.l_margin)
    pdf.set_fill_color(245, 247, 250)
    pdf.set_font("Courier", "", 8)
    pdf.set_text_color(40, 40, 40)
    block = "\n".join(lines)
    pdf.multi_cell(0, 4.2, _sanitize(block), fill=True)
    pdf.ln(2)


def _write_table_row(pdf: AuditPDF, cols: list[str], *, header: bool = False) -> None:
    pdf.set_font("Helvetica", "B" if header else "", 8 if header else 7.5)
    pdf.set_text_color(20, 20, 20) if header else pdf.set_text_color(50, 50, 50)
    col_w = pdf.w - pdf.l_margin - pdf.r_margin
    w1 = col_w * 0.38
    w2 = col_w * 0.62
    h = 5
    y0 = pdf.get_y()
    if y0 > pdf.h - 20:
        pdf.add_page()
        y0 = pdf.get_y()
    x0 = pdf.l_margin
    pdf.set_xy(x0, y0)
    pdf.multi_cell(w1, h, _sanitize(cols[0]), border=1)
    y1 = pdf.get_y()
    h1 = y1 - y0
    pdf.set_xy(x0 + w1, y0)
    pdf.multi_cell(w2, h, _sanitize(cols[1] if len(cols) > 1 else ""), border=1)
    y2 = pdf.get_y()
    pdf.set_y(max(y1, y2))


def render_markdown_to_pdf(md_text: str, pdf: AuditPDF) -> None:
    lines = md_text.splitlines()
    i = 0
    in_code = False
    code_buf: list[str] = []
    table_mode = False
    table_header_done = False

    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()

        if line.strip().startswith("```"):
            if in_code:
                _write_code_block(pdf, code_buf)
                code_buf = []
                in_code = False
            else:
                in_code = True
            i += 1
            continue

        if in_code:
            code_buf.append(line)
            i += 1
            continue

        if not line.strip():
            if table_mode:
                table_mode = False
                table_header_done = False
                pdf.ln(2)
            else:
                pdf.ln(2)
            i += 1
            continue

        if line.strip() == "---":
            pdf.ln(2)
            pdf.set_draw_color(200, 200, 200)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
            pdf.ln(4)
            i += 1
            continue

        if line.startswith("# "):
            _write_heading(pdf, line[2:].strip(), 1)
            i += 1
            continue
        if line.startswith("## "):
            _write_heading(pdf, line[3:].strip(), 2)
            i += 1
            continue
        if line.startswith("### "):
            _write_heading(pdf, line[4:].strip(), 3)
            i += 1
            continue

        if "|" in line and line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                i += 1
                continue
            if not table_mode:
                table_mode = True
                table_header_done = False
            _write_table_row(pdf, cells[:2] if len(cells) >= 2 else cells, header=not table_header_done)
            table_header_done = True
            i += 1
            continue

        if table_mode:
            table_mode = False
            table_header_done = False
            pdf.ln(1)

        if line.startswith("**") and line.endswith("**"):
            _write_paragraph(pdf, line.strip("*"), bold=True)
        elif line.startswith("- "):
            _write_paragraph(pdf, f"  - {line[2:].strip()}")
        else:
            _write_paragraph(pdf, re.sub(r"\*\*([^*]+)\*\*", r"\1", line))

        i += 1

    if code_buf:
        _write_code_block(pdf, code_buf)


def main() -> None:
    md = MD_PATH.read_text(encoding="utf-8")
    pdf = AuditPDF(orientation="P", unit="mm", format="A4")
    pdf.set_margins(15, 15, 15)
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.set_x(pdf.l_margin)

    # Title block
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(30, 30, 80)
    pdf.multi_cell(0, 10, _sanitize("Live Operations Pipeline"))
    pdf.set_x(pdf.l_margin)
    pdf.set_font("Helvetica", "", 12)
    pdf.set_text_color(60, 60, 60)
    pdf.multi_cell(0, 7, _sanitize("Structural Audit (Read-Only) - IG Agent v29.1"))
    pdf.ln(3)
    pdf.set_x(pdf.l_margin)
    pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(
        0,
        5,
        _sanitize(
            "Prepared for Copilot review and autonomous self-healing layer planning. "
            "Audit date: 2026-07-01."
        ),
    )
    pdf.ln(6)

    render_markdown_to_pdf(md, pdf)
    PDF_PATH.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(PDF_PATH))
    print(f"Wrote {PDF_PATH}")


if __name__ == "__main__":
    main()
