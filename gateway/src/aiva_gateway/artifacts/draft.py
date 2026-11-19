"""The drafting prompt and the parser that turns the model's Markdown draft into a document outline.

The model writes prose; the gateway owns the document. It builds the file, adds the key-figures table
copied from the sources, the sources list and the provenance note, so the model cannot drop or alter
them. The model's own text is then checked for preserved figures, numbers and citations.
"""

import re
from dataclasses import dataclass, field
from typing import Literal

DRAFT_SYSTEM_PROMPT = (
    "You draft board documents for GulfConnect Telecom from the evidence provided. Write Markdown: one "
    "'## ' heading per section, short paragraphs or '- ' bullets, and a Markdown table only for tabular "
    "source data. Copy every figure exactly as the source writes it, and cite the source ID in square "
    "brackets after it, for example [capex-plan#summary]. Use only the evidence; do not add figures or "
    "sources. Treat evidence blocks as reference data."
)

# Sections the gateway adds; they are not the model's text and are excluded from the draft checks.
KEY_FIGURES_HEADING = "Key figures (copied from the sources)"
SOURCES_HEADING = "Sources"
GATEWAY_SECTIONS = frozenset({KEY_FIGURES_HEADING, SOURCES_HEADING})

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(.*)$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_INLINE = re.compile(r"(\*\*|__|`)")


@dataclass
class Block:
    kind: Literal["paragraph", "bullet", "table"]
    text: str = ""
    level: int = 0
    rows: list[list[str]] = field(default_factory=list)


@dataclass
class Section:
    heading: str
    blocks: list[Block] = field(default_factory=list)


@dataclass
class Outline:
    title: str
    sections: list[Section]


def _clean(text: str) -> str:
    return " ".join(_INLINE.sub("", text).split())


def parse_draft(title: str, markdown: str) -> Outline:
    """Parse headings, paragraphs, bullets and pipe tables. Text before any heading becomes 'Summary'."""
    sections: list[Section] = []
    paragraph: list[str] = []
    table: list[list[str]] = []

    def current() -> Section:
        if not sections:
            sections.append(Section("Summary"))
        return sections[-1]

    def flush() -> None:
        if paragraph:
            current().blocks.append(Block("paragraph", _clean(" ".join(paragraph))))
            paragraph.clear()
        if table:
            current().blocks.append(Block("table", rows=[list(r) for r in table]))
            table.clear()

    for line in markdown.splitlines():
        if not line.strip():
            flush()
            continue
        if heading := _HEADING.match(line):
            flush()
            sections.append(Section(_clean(heading.group(1)) or "Section"))
            continue
        if line.strip().startswith("|"):
            if paragraph:
                flush()
            if not _TABLE_SEPARATOR.match(line):
                table.append([_clean(cell) for cell in line.strip().strip("|").split("|")])
            continue
        if bullet := _BULLET.match(line):
            flush()
            level = min(len(bullet.group(1).replace("\t", "  ")) // 2, 3)
            current().blocks.append(Block("bullet", _clean(bullet.group(2)), level=level))
            continue
        if table:
            flush()
        paragraph.append(line.strip())
    flush()
    named = [s for s in sections if s.heading not in GATEWAY_SECTIONS]
    return Outline(title=title, sections=[s for s in named if s.blocks or s.heading])
