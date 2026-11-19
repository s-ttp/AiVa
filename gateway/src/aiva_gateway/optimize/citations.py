"""Knowledge-passage citations: the header `[<document> §<n>] <title> - <heading>` on a passage's first line.

The gateway renders every knowledge-base passage this way (`knowledge.evidence_text`), the same format the
CIM is trained on (ML & Data, `aiva_ml.gct.tasks_docs.passage`). A citation `[<document> §<n>]` names one
section of one document; the Context Guard checks that kept passages keep their header and that every
citation a package emits resolves to a passage of the request.
"""

import re
from typing import NamedTuple

# A document id has no spaces or brackets; a section is `§` and its number.
_CITATION = r"\[(?P<document>[^\s\[\]]+) (?P<section>§\d+)\]"
CITATION = re.compile(_CITATION)
_HEADER = re.compile(rf"\s*{_CITATION}[^\n]*")
_NUMBERED = re.compile(r"§(\d+)\s+(.*)", re.DOTALL)


class Citation(NamedTuple):
    document: str
    section: str


def citation_header(document_id: str, number: int, title: str, heading: str) -> str:
    return f"[{document_id} §{number}] {title} - {heading}"


def numbered_section(section: str, position: int) -> tuple[int, str]:
    """A KB section's number and heading: `§4 Validity and renewal` is (4, "Validity and renewal"); an
    unnumbered section is numbered by its 1-based position in the document."""
    match = _NUMBERED.fullmatch(section.strip())
    if match:
        return int(match.group(1)), match.group(2).strip()
    return position, section.strip()


def passage_header(text: str) -> tuple[Citation, str] | None:
    """The citation and the full header line of a passage, if its first line is a citation header."""
    match = _HEADER.match(text)
    if match is None:
        return None
    return Citation(match.group("document"), match.group("section")), " ".join(match.group(0).split())


def citations_in(text: str) -> set[Citation]:
    return {Citation(m.group("document"), m.group("section")) for m in CITATION.finditer(text)}
