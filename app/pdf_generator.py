"""PDF Report Generator for Repository Code Analysis Reports."""

from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.pdfgen import canvas


class NumberedCanvas(canvas.Canvas):
    """Two-pass canvas to dynamically compute and render total page count and running headers."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._saved_page_states: list[dict[str, Any]] = []
        self.report_title = "Code Analysis Report"
        self.report_date = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    def showPage(self) -> None:
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_header_footer(num_pages)
            super().showPage()
        super().save()

    def draw_header_footer(self, page_count: int) -> None:
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#64748b"))

        # Footer (all pages)
        page_text = f"Page {self._pageNumber} of {page_count}"
        self.drawRightString(letter[0] - 36, 22, page_text)
        self.drawString(36, 22, "Jira AI • Automated Code Architecture & Deep Analysis Report")

        self.setStrokeColor(colors.HexColor("#e2e8f0"))
        self.setLineWidth(0.5)
        self.line(36, 32, letter[0] - 36, 32)

        # Running Header (pages 2+)
        if self._pageNumber > 1:
            self.drawString(36, letter[1] - 24, self.report_title)
            self.drawRightString(letter[0] - 36, letter[1] - 24, self.report_date)
            self.line(36, letter[1] - 28, letter[0] - 36, letter[1] - 28)

        self.restoreState()


def _format_cell_text(text: str) -> str:
    """Format table cell text and convert markdown inline markup cleanly."""
    if not text:
        return "-"
    # Markdown formatting
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`([^`]+)`", r"<font face='Courier' color='#334155'><b>\1</b></font>", text)
    text = text.replace("<br>", "<br/>").replace("<br />", "<br/>")
    return text


def _compute_col_widths(raw_headers: list[str], total_width: float = 540) -> list[float]:
    """Smart column width calculator based on header semantics."""
    h_lower = [h.lower() for h in raw_headers]
    n = len(raw_headers)

    # 1. Two-column tables (e.g. Field | Analysis or Metric | Value)
    if n == 2:
        if "field" in h_lower[0] or "metric" in h_lower[0] or "item" in h_lower[0] or "risk" in h_lower[0]:
            return [140, 400]
        if "dependency" in h_lower[0] or "filename" in h_lower[0]:
            return [360, 180]
        return [150, 390]

    # 2. Three-column tables
    if n == 3:
        if "directory" in h_lower[0] and "responsibility" in h_lower[1]:
            return [110, 370, 60]
        if "pattern" in h_lower[0]:
            return [130, 325, 85]
        if "path" in h_lower[0] and "lines" in h_lower[1]:
            return [355, 65, 120]
        if "node" in h_lower[0] and "metadata" in h_lower[2]:
            return [95, 245, 200]
        if "filename" in h_lower[0] and "examples" in h_lower[2]:
            return [120, 50, 370]
        if "caller" in h_lower[0]:
            return [150, 150, 240]
        return [130, 280, 130]

    # 3. Four-column tables
    if n == 4:
        if "commit" in h_lower[0] and "summary" in h_lower[3]:
            return [65, 75, 95, 305]
        if "node" in h_lower[0] and "centrality" in h_lower[3]:
            return [140, 220, 90, 90]
        return [110, 190, 120, 120]

    # 4. Five-column tables (e.g. Kind | Name | Path | Lines | Cyclomatic estimate)
    if n == 5:
        if "kind" in h_lower[0] and "cyclomatic" in h_lower[4]:
            return [48, 110, 232, 45, 105]
        return [100, 110, 170, 60, 100]

    # 5. Six-column tables (Repository Inventory)
    if n == 6:
        if "repository" in h_lower[0]:
            return [105, 65, 80, 50, 75, 165]
        return [total_width / n] * n

    return [total_width / n] * n


def markdown_to_pdf(markdown_text: str, repo_name: str = "Repository") -> bytes:
    """Convert Markdown Code Analysis Report into a styled, professional PDF."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=36,
    )

    styles = getSampleStyleSheet()

    # Typography styles
    title_style = ParagraphStyle(
        "DocTitle",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        textColor=colors.HexColor("#1e1b4b"),
        spaceAfter=2,
    )
    subtitle_style = ParagraphStyle(
        "DocSub",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9.5,
        leading=13,
        textColor=colors.HexColor("#64748b"),
        spaceAfter=10,
    )
    h1_style = ParagraphStyle(
        "H1",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=13,
        leading=17,
        textColor=colors.HexColor("#3730a3"),
        spaceBefore=14,
        spaceAfter=5,
        keepWithNext=True,
    )
    h2_style = ParagraphStyle(
        "H2",
        parent=styles["Heading3"],
        fontName="Helvetica-Bold",
        fontSize=10.5,
        leading=14,
        textColor=colors.HexColor("#0f172a"),
        spaceBefore=10,
        spaceAfter=4,
        keepWithNext=True,
    )
    body_style = ParagraphStyle(
        "Body",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=12.5,
        textColor=colors.HexColor("#334155"),
        spaceAfter=5,
        wordWrap="CJK",
    )
    bullet_style = ParagraphStyle(
        "Bullet",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=12,
        textColor=colors.HexColor("#334155"),
        leftIndent=10,
        spaceAfter=2.5,
        wordWrap="CJK",
    )
    code_block_style = ParagraphStyle(
        "CodeBlock",
        parent=styles["Normal"],
        fontName="Courier",
        fontSize=7.5,
        leading=10.5,
        textColor=colors.HexColor("#0f172a"),
        spaceBefore=2,
        spaceAfter=6,
        wordWrap="CJK",
    )
    table_cell_style = ParagraphStyle(
        "TableCell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7.5,
        leading=10.5,
        textColor=colors.HexColor("#1e293b"),
        wordWrap="CJK",
    )
    table_cell_bold = ParagraphStyle(
        "TableCellBold",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=7.5,
        leading=10.5,
        textColor=colors.HexColor("#0f172a"),
        wordWrap="CJK",
    )
    table_header_style = ParagraphStyle(
        "TableHeader",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=11,
        textColor=colors.white,
    )

    story = []

    # Title Banner
    story.append(Paragraph(f"Code Analysis Report: {repo_name}", title_style))
    story.append(
        Paragraph(
            f"Generated by Jira AI • {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
            subtitle_style,
        )
    )
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#4f46e5"), spaceAfter=10))

    # Parse Markdown blocks
    lines = markdown_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if not line:
            i += 1
            continue

        # Headers
        if line.startswith("# "):
            # Skip main title if duplicate
            i += 1
            continue
        elif line.startswith("## "):
            text = line[3:].strip()
            story.append(Spacer(1, 4))
            story.append(Paragraph(text, h1_style))
            story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cbd5e1"), spaceAfter=5))
            i += 1
            continue
        elif line.startswith("### "):
            text = line[4:].strip()
            story.append(Spacer(1, 3))
            story.append(Paragraph(text, h2_style))
            i += 1
            continue

        # Sub-section labels ending with ':'
        if (
            line.endswith(":")
            and len(line) < 60
            and not line.startswith("|")
            and not line.startswith("-")
            and not line.startswith("*")
        ):
            story.append(Spacer(1, 2))
            story.append(Paragraph(line, h2_style))
            i += 1
            continue

        # Code Blocks (```text ... ```)
        if line.startswith("```"):
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if i < len(lines) and lines[i].strip().startswith("```"):
                i += 1  # consume closing backticks

            code_text = "\n".join(code_lines).strip()
            if code_text:
                # Render code box with clean light slate styling
                code_p = Preformatted(
                    code_text[:2000],
                    code_block_style,
                    maxLineLength=95,
                )
                code_table = Table([[code_p]], colWidths=[540])
                code_table.setStyle(
                    TableStyle([
                        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
                        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                        ("TOPPADDING", (0, 0), (-1, -1), 6),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ])
                )
                story.append(KeepTogether([code_table]))
                story.append(Spacer(1, 4))
            continue

        # Markdown Table
        if line.startswith("|") and i + 1 < len(lines) and "---" in lines[i + 1]:
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1

            if len(table_lines) >= 2:
                # Parse headers
                raw_headers = [c.strip() for c in table_lines[0].strip("|").split("|")]
                headers = [
                    Paragraph(re.sub(r"<[^>]+>", " ", h), table_header_style)
                    for h in raw_headers
                ]

                table_data = [headers]
                # Parse rows (skip separator row at index 1)
                for r_line in table_lines[2:]:
                    raw_cells = [c.strip() for c in r_line.strip("|").split("|")]
                    while len(raw_cells) < len(raw_headers):
                        raw_cells.append("-")
                    row_cells = []
                    for idx, c in enumerate(raw_cells[: len(raw_headers)]):
                        formatted_c = _format_cell_text(c)
                        style_to_use = (
                            table_cell_bold
                            if idx == 0 and len(raw_headers) <= 2
                            else table_cell_style
                        )
                        row_cells.append(Paragraph(formatted_c, style_to_use))
                    table_data.append(row_cells)

                col_widths = _compute_col_widths(raw_headers, total_width=540)

                t = Table(table_data, colWidths=col_widths, repeatRows=1)
                t.setStyle(
                    TableStyle([
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#3730a3")),
                        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
                        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
                        ("LEFTPADDING", (0, 0), (-1, -1), 5),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                        (
                            "ROWBACKGROUNDS",
                            (0, 1),
                            (-1, -1),
                            [colors.HexColor("#ffffff"), colors.HexColor("#f8fafc")],
                        ),
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
                    ])
                )
                story.append(t)
                story.append(Spacer(1, 6))
            continue

        # Bullet List
        if line.startswith("- ") or line.startswith("* ") or line.startswith("• "):
            text = line.lstrip("-*• ").strip()
            text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
            text = re.sub(r"`([^`]+)`", r"<font face='Courier' color='#334155'><b>\1</b></font>", text)
            story.append(Paragraph(f"• {text}", bullet_style))
            i += 1
            continue

        # Standard Paragraph
        text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", line)
        text = re.sub(r"`([^`]+)`", r"<font face='Courier' color='#334155'><b>\1</b></font>", text)
        story.append(Paragraph(text, body_style))
        i += 1

    # Build with custom canvas for page numbers and running headers
    def _canvas_factory(*args: Any, **kwargs: Any) -> NumberedCanvas:
        nc = NumberedCanvas(*args, **kwargs)
        nc.report_title = f"Code Analysis Report: {repo_name}"
        nc.report_date = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        return nc

    doc.build(story, canvasmaker=_canvas_factory)
    buffer.seek(0)
    return buffer.getvalue()
