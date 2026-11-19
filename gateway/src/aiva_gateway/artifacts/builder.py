"""Build editable DOCX and PPTX files from a draft outline (python-docx, python-pptx).

Everything is native, unprotected Office text: headings, paragraphs, bullets and tables. There are no
images, macros or embedded objects. The gateway adds a provenance note, a key-figures table copied from
the sources, and the list of sources, so those never depend on the model.
"""

import io
from dataclasses import dataclass

from docx import Document
from docx.shared import Pt
from pptx import Presentation
from pptx.util import Inches
from pptx.util import Pt as PptxPt

from aiva_gateway.artifacts.draft import KEY_FIGURES_HEADING, SOURCES_HEADING, Block, Outline
from aiva_gateway.contracts.messages import CriticalFigure, DocumentType

GENERATOR_VERSION = "aiva-doc-builder-v1"
_TITLE_AND_CONTENT, _TITLE_ONLY, _TITLE_SLIDE = 1, 5, 0
_MAX_BULLETS_PER_SLIDE = 8


@dataclass(frozen=True)
class DocumentFrame:
    """What the gateway states around the model's text."""

    provenance_note: str
    figures: list[CriticalFigure]
    source_ids: list[str]


def build_document(document_type: DocumentType, outline: Outline, frame: DocumentFrame) -> bytes:
    return _build_docx(outline, frame) if document_type == "docx" else _build_pptx(outline, frame)


def _figure_rows(figures: list[CriticalFigure]) -> list[list[str]]:
    rows = [["Figure", "Value", "Unit", "Source"]]
    rows += [[f.label, f.value, f.unit or "", f.source_id] for f in figures]
    return rows


# --- DOCX ------------------------------------------------------------------------------------------


def _docx_table(document: object, rows: list[list[str]]) -> None:
    width = max(len(r) for r in rows)
    table = document.add_table(rows=len(rows), cols=width)  # type: ignore[attr-defined]
    table.style = "Table Grid"
    for r, row in enumerate(rows):
        for c in range(width):
            table.cell(r, c).text = row[c] if c < len(row) else ""


def _docx_block(document: object, block: Block) -> None:
    if block.kind == "table" and block.rows:
        _docx_table(document, block.rows)
    elif block.kind == "bullet":
        style = "List Bullet" if block.level == 0 else f"List Bullet {min(block.level + 1, 3)}"
        document.add_paragraph(block.text, style=style)  # type: ignore[attr-defined]
    elif block.text:
        document.add_paragraph(block.text)  # type: ignore[attr-defined]


def _build_docx(outline: Outline, frame: DocumentFrame) -> bytes:
    document = Document()
    document.core_properties.title = outline.title
    document.core_properties.author = "AiVa document copilot (synthetic GCT data)"
    document.styles["Normal"].font.size = Pt(11)
    document.add_heading(outline.title, level=0)
    note = document.add_paragraph(frame.provenance_note)
    note.runs[0].italic = True
    for section in outline.sections:
        document.add_heading(section.heading, level=1)
        for block in section.blocks:
            _docx_block(document, block)
    if frame.figures:
        document.add_heading(KEY_FIGURES_HEADING, level=1)
        _docx_table(document, _figure_rows(frame.figures))
    document.add_heading(SOURCES_HEADING, level=1)
    for source_id in frame.source_ids:
        document.add_paragraph(source_id, style="List Bullet")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# --- PPTX ------------------------------------------------------------------------------------------


def _pptx_bullets(slide: object, lines: list[tuple[str, int]]) -> None:
    body = slide.placeholders[1].text_frame  # type: ignore[attr-defined]
    body.clear()
    for index, (text, level) in enumerate(lines):
        paragraph = body.paragraphs[0] if index == 0 else body.add_paragraph()
        paragraph.text = text
        paragraph.level = level
        paragraph.font.size = PptxPt(16)


def _pptx_table(presentation: object, title: str, rows: list[list[str]]) -> None:
    slide = presentation.slides.add_slide(presentation.slide_layouts[_TITLE_ONLY])  # type: ignore[attr-defined]
    slide.shapes.title.text = title
    width = max(len(r) for r in rows)
    shape = slide.shapes.add_table(
        len(rows), width, Inches(0.5), Inches(1.6), Inches(9), Inches(0.4) * len(rows)
    )
    for r, row in enumerate(rows):
        for c in range(width):
            cell = shape.table.cell(r, c)
            cell.text = row[c] if c < len(row) else ""
            for paragraph in cell.text_frame.paragraphs:
                paragraph.font.size = PptxPt(12)


def _build_pptx(outline: Outline, frame: DocumentFrame) -> bytes:
    presentation = Presentation()
    presentation.core_properties.title = outline.title
    presentation.core_properties.author = "AiVa document copilot (synthetic GCT data)"
    cover = presentation.slides.add_slide(presentation.slide_layouts[_TITLE_SLIDE])
    cover.shapes.title.text = outline.title
    cover.placeholders[1].text = frame.provenance_note
    for section in outline.sections:
        lines = [(b.text, b.level) for b in section.blocks if b.kind != "table" and b.text]
        for start in range(0, max(len(lines), 1), _MAX_BULLETS_PER_SLIDE):
            chunk = lines[start : start + _MAX_BULLETS_PER_SLIDE]
            if not chunk and any(b.kind == "table" for b in section.blocks):
                break
            slide = presentation.slides.add_slide(presentation.slide_layouts[_TITLE_AND_CONTENT])
            suffix = (
                f" ({start // _MAX_BULLETS_PER_SLIDE + 1})" if len(lines) > _MAX_BULLETS_PER_SLIDE else ""
            )
            slide.shapes.title.text = section.heading + suffix
            _pptx_bullets(slide, chunk or [("", 0)])
        for block in section.blocks:
            if block.kind == "table" and block.rows:
                _pptx_table(presentation, section.heading, block.rows)
    if frame.figures:
        _pptx_table(presentation, KEY_FIGURES_HEADING, _figure_rows(frame.figures))
    sources = presentation.slides.add_slide(presentation.slide_layouts[_TITLE_AND_CONTENT])
    sources.shapes.title.text = SOURCES_HEADING
    _pptx_bullets(sources, [(source_id, 0) for source_id in frame.source_ids] or [("", 0)])
    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()
