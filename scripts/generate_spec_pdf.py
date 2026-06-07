#!/usr/bin/env python3
"""Generate IG_Agent_v25_COMPLETE_SPEC_v8.pdf from the markdown source."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from fpdf import FPDF

ROOT = Path(__file__).resolve().parents[1]
MD_PATH = ROOT / "IG_Agent_v25_COMPLETE_SPEC_v8.md"
PDF_PATH = ROOT / "IG_Agent_v25_COMPLETE_SPEC_v8.pdf"


def _ascii_safe(text: str) -> str:
    replacements = {
        "\u2014": "-",
        "\u2013": "-",
        "\u2192": "->",
        "\u2265": ">=",
        "\u2264": "<=",
        "\u00d7": "x",
        "\u00a3": "GBP",
        "\u201c": '"',
        "\u201d": '"',
        "\u2018": "'",
        "\u2019": "'",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text.encode("latin-1", errors="replace").decode("latin-1")


class SpecPDF(FPDF):
    def header(self) -> None:
        if self.page_no() == 1:
            return
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(100, 100, 100)
        self.cell(
            0,
            8,
            "IG Agent v25 - Complete Final Specification v8 | June 2026 | CONFIDENTIAL",
            align="C",
        )
        self.ln(10)

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(100, 100, 100)
        self.cell(0, 8, f"Page {self.page_no()}", align="C")


def _write(
    pdf: SpecPDF,
    text: str,
    *,
    h: float = 5,
    font: tuple[str, str, int] = ("Helvetica", "", 9),
) -> None:
    pdf.set_x(pdf.l_margin)
    pdf.set_font(*font)
    pdf.multi_cell(pdf.epw, h, text)


def render_markdown(pdf: SpecPDF, md_text: str) -> None:
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.set_margins(18, 18, 18)

    in_code = False
    for raw_line in md_text.splitlines():
        line = _ascii_safe(raw_line.rstrip())

        if line.startswith("```"):
            in_code = not in_code
            continue

        if in_code:
            pdf.set_text_color(40, 40, 40)
            _write(pdf, line, h=4.5, font=("Courier", "", 8))
            pdf.ln(1)
            continue

        if not line.strip():
            pdf.ln(3)
            continue

        if line.startswith("# "):
            pdf.ln(4)
            pdf.set_text_color(20, 20, 20)
            _write(pdf, line[2:].strip(), h=8, font=("Helvetica", "B", 16))
            pdf.ln(2)
            continue

        if line.startswith("## "):
            pdf.ln(3)
            pdf.set_text_color(30, 30, 30)
            _write(pdf, line[3:].strip(), h=7, font=("Helvetica", "B", 13))
            pdf.ln(1)
            continue

        if line.startswith("### "):
            pdf.ln(2)
            pdf.set_text_color(40, 40, 40)
            _write(pdf, line[4:].strip(), h=6, font=("Helvetica", "B", 11))
            pdf.ln(1)
            continue

        if line.startswith("|") and "|" in line[1:]:
            if re.match(r"^\|[\s\-:|]+\|$", line.replace(" ", "")):
                continue  # markdown table separator
            cells = [c.strip() for c in line.strip("|").split("|")]
            row = " | ".join(cells)
            pdf.set_text_color(30, 30, 30)
            _write(pdf, row, h=4.5, font=("Helvetica", "", 7))
            pdf.ln(0.5)
            continue

        if line.startswith("- ") or line.startswith("* "):
            pdf.set_text_color(30, 30, 30)
            _write(pdf, f"- {line[2:].strip()}")
            continue

        if re.match(r"^\d+\.\s", line):
            pdf.set_text_color(30, 30, 30)
            _write(pdf, line)
            continue

        if line.startswith("**") and line.endswith("**"):
            pdf.set_text_color(30, 30, 30)
            _write(pdf, line.strip("*"), font=("Helvetica", "B", 10))
            continue

        # strip light markdown
        clean = re.sub(r"\*\*([^*]+)\*\*", r"\1", line)
        clean = re.sub(r"`([^`]+)`", r"\1", clean)
        if clean.startswith("---"):
            pdf.ln(2)
            pdf.set_draw_color(200, 200, 200)
            y = pdf.get_y()
            pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
            pdf.ln(4)
            continue

        pdf.set_text_color(30, 30, 30)
        _write(pdf, clean)


def main() -> int:
    if not MD_PATH.is_file():
        print(f"Missing source: {MD_PATH}", file=sys.stderr)
        return 1

    md_text = MD_PATH.read_text(encoding="utf-8")
    pdf = SpecPDF(orientation="P", unit="mm", format="A4")
    pdf.set_title("IG Agent v25 Complete Final Specification v8")
    pdf.set_author("IG Agent v25")
    render_markdown(pdf, md_text)
    pdf.output(str(PDF_PATH))
    print(f"Wrote {PDF_PATH} ({PDF_PATH.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
