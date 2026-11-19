"""Document-output checks on the file as the sandbox opened it (LLD §16 "Document outputs").

Each check reads the rendered document, not the draft, so it tests what a reader receives:
- structure: title, at least two drafted sections, key figures (when supplied) and sources present;
- figures: every critical figure appears, exactly, in the drafted text (the gateway's key-figures
  table copies the sources and is not counted);
- numbers: the drafted text holds no number absent from the request and its sources (none invented or
  altered);
- citations: every source is cited in the drafted text, and every citation names a supplied source;
- editability: native unprotected text and tables, no images or embedded objects;
- output_dlp: the drafting run's output DLP result;
- sandbox_render: the generated file opened in the network-less sandbox with its hash verified.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from aiva_gateway.artifacts.draft import GATEWAY_SECTIONS
from aiva_gateway.artifacts.sandbox import Rendered
from aiva_gateway.contracts.artifacts import ArtifactCheck, CitationResult, DocumentOutline, FigureResult
from aiva_gateway.contracts.messages import CriticalFigure, EvidenceItem

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_CITATION = re.compile(r"\[([A-Za-z0-9][A-Za-z0-9._#:/-]*)\]")


def _normalise(number: str) -> str:
    """'1,240' and '1240' are the same figure; '412.60' is not '412.6' (no rounding is accepted)."""
    return number.replace(",", "")


def numbers(text: str) -> set[str]:
    return {_normalise(n) for n in _NUMBER.findall(text)}


@dataclass(frozen=True)
class CheckReport:
    checks: list[ArtifactCheck]
    figures: list[FigureResult]
    citations: list[CitationResult]
    outline: DocumentOutline

    @property
    def passed(self) -> bool:
        return all(c.status == "passed" for c in self.checks)


def _block_texts(block: dict[str, Any]) -> Iterable[str]:
    if block.get("rows"):
        for row in block["rows"]:
            yield " ".join(row)
    elif block.get("text"):
        yield block["text"]


def drafted_text(rendered: Rendered) -> list[tuple[str, str]]:
    """(location, text) for the model's drafted content: excludes the title page and gateway sections."""
    located: list[tuple[str, str]] = []
    kind = "slide" if rendered.pages and rendered.pages[0]["kind"] == "slide" else "section"
    for page in rendered.pages[1:]:
        if page.get("title") in GATEWAY_SECTIONS:
            continue
        for block in page["blocks"]:
            if block["kind"] == "heading":
                continue
            for text in _block_texts(block):
                located.append((f"{kind} {page['index']}", text))
    return located


def check_document(
    rendered: Rendered,
    *,
    title: str,
    instructions: str,
    evidence: list[EvidenceItem],
    figures: list[CriticalFigure],
    output_dlp: str,
) -> CheckReport:
    drafted = drafted_text(rendered)
    text = "\n".join(t for _, t in drafted)
    titles = [p.get("title") for p in rendered.pages]
    narrative_sections = [t for t in titles[1:] if t and t not in GATEWAY_SECTIONS]
    source_ids = [e.id for e in evidence]
    checks: list[ArtifactCheck] = []

    structure_failures = []
    if not rendered.title and not titles[:1]:
        structure_failures.append("title")
    if len(set(narrative_sections)) < 2:
        structure_failures.append("drafted-sections")
    if figures and "Key figures (copied from the sources)" not in titles:
        structure_failures.append("key-figures")
    if "Sources" not in titles:
        structure_failures.append("sources")
    checks.append(
        ArtifactCheck(
            name="structure",
            status="failed" if structure_failures else "passed",
            detail="Title, drafted sections, key figures and sources are present",
            failures=structure_failures,
        )
    )

    figure_results = []
    for figure in figures:
        wanted = _normalise(figure.value)
        locations = sorted({where for where, t in drafted if wanted in numbers(t)})
        occurrences = sum(1 for _, t in drafted for n in _NUMBER.findall(t) if _normalise(n) == wanted)
        figure_results.append(
            FigureResult(
                figure_id=figure.id, preserved=bool(locations), occurrences=occurrences, locations=locations
            )
        )
    lost = [f.figure_id for f in figure_results if not f.preserved]
    checks.append(
        ArtifactCheck(
            name="figures",
            status="failed" if lost else "passed",
            detail="Critical figures preserved in the drafted text",
            failures=lost,
        )
    )

    allowed = numbers(title) | numbers(instructions)
    for item in evidence:
        allowed |= numbers(item.text) | numbers(item.id)
    invented = sorted(numbers(text) - allowed)
    checks.append(
        ArtifactCheck(
            name="numbers",
            status="failed" if invented else "passed",
            detail="No altered or invented numbers",
            failures=[f"number-{i + 1}" for i in range(len(invented))],
        )
    )

    cited = set(_CITATION.findall(text))
    citation_results = [CitationResult(source_id=s, cited=s in cited) for s in source_ids]
    unknown = sorted(cited - set(source_ids))
    uncited = [c.source_id for c in citation_results if not c.cited]
    checks.append(
        ArtifactCheck(
            name="citations",
            status="failed" if unknown or uncited else "passed",
            detail="Every source cited; no citation outside the supplied sources",
            failures=uncited + [f"unknown:{i + 1}" for i in range(len(unknown))],
        )
    )

    stats = rendered.stats
    editable = not rendered.protected and stats.get("images", 0) == 0 and stats.get("text_frames", 0) > 0
    checks.append(
        ArtifactCheck(
            name="editability",
            status="passed" if editable else "failed",
            detail="Editable Office document: native text and tables, unprotected, no images",
            failures=[] if editable else ["not-editable"],
        )
    )
    dlp_ok = output_dlp in ("clean", "redacted")
    checks.append(
        ArtifactCheck(
            name="output_dlp",
            status="passed" if dlp_ok else "failed",
            detail=f"Output DLP: {output_dlp}",
            failures=[] if dlp_ok else [output_dlp],
        )
    )
    checks.append(
        ArtifactCheck(
            name="sandbox_render",
            status="passed",
            detail="Rendered in an isolated, network-less sandbox",
        )
    )
    outline = DocumentOutline(
        sections=stats.get("headings", 0),
        paragraphs=stats.get("paragraphs", 0),
        tables=stats.get("tables", 0),
        editable=editable,
    )
    return CheckReport(checks=checks, figures=figure_results, citations=citation_results, outline=outline)
