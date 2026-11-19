"""Assurance check `answer-citations`: an answer may cite only what was actually in its context package.

A citation in the answer is either:
- a knowledge-base citation `[DOC-ID §N]` in the KB's document-ID grammar (uppercase segments joined by
  hyphens, at most 40 characters) and a section number of at most three digits. A source note,
  `(Source: DOC-ID §N)` or `(Source: DOC-ID §N and §M)`, cites each of its sections as `[DOC-ID §N]` would;
- a reference written without brackets, such as a heading `**GCT-POL-014 §2 (GCT Group)**` or
  `POL-118 §3-§4`, when its document is one of the passages the model was given: the same ID, or the only
  one ending in `-<ID>` (the prefix dropped). A list (`§2/§3`, `§2, §3`, `§2 and §3`) cites each section and
  a range (`§3-§4`, `§3-5`, or with an en or em dash) each section in it. A bare reference to a document the
  model was not given is ordinary text, not a citation; or
- the ID of one of the run's candidate evidence passages (supplied or retrieved), matched as an exact whole
  token. Only identifier-shaped IDs of at least three characters with a letter are matched, so a client's
  "1" or "2" never turns an ordinary number in the answer into a citation. A record's own identifier counts
  as its evidence ID: the last segment of a path-shaped ID (`ALM-7790` for `noc/ALM-7790`), when it has a
  letter and a digit, at least three characters, and no other candidate ends in or is that segment. So S6's
  `INC-7781/incident.lookup` is never matched by a word, and an ambiguous segment by nothing.

It resolves when the answering model was given it: a `[DOC-ID §N]` (in any form) that appears in the
upstream payload (a kept passage's header, or the user's own message), or an evidence ID (or a record's own
identifier) whose `<evidence id="…">` block was sent. A citation of a passage the CIM omitted, or one that
was never retrieved, is unresolved.

The check never blocks and never rewrites the answer: it reports `resolved`, `unresolved` (with the count and
the identifiers), or `none` when the answer cites nothing. Because the patterns are strict, the reported
identifiers can never carry free model text (every form is reported as `[DOC-ID §N]`, with a shortened ID
expanded to the given document's). Evidence coverage =
resolved citations ÷ cited, recorded as `assurance_result.evidence_coverage` (null when nothing is cited). The
upstream payload is used in-run only and never persisted.
"""

import html
import re

from aiva_gateway.pipeline.interfaces import AnswerContext, CitationCheck

CHECK = "answer-citations"
MAX_REFS = 10
_DOC_ID = r"[A-Z][A-Z0-9]{1,11}(?:-[A-Z0-9]{1,12}){1,3}"
_SECTION = r"§\d{1,3}"
_KB_CITATION = re.compile(rf"\[(?P<document>{_DOC_ID}) (?P<section>{_SECTION})\]")
# The same citation as a source note: "(Source: GCT-POL-014 §3)" or "(Source: GCT-POL-014 §3 and §4)".
_SOURCE_NOTE = re.compile(
    rf"\(Source: (?P<document>{_DOC_ID}) (?P<sections>{_SECTION}(?: and {_SECTION})?)\)"
)
# Without brackets: not part of a longer identifier or of a bracketed citation, then the sections. After the
# first `§N`, a range (a hyphen, en or em dash, or `to`) may omit its `§`; a list item (`/`, `,`, `&`, `and`)
# may not, so "§3, 12 months" stays one section.
_RANGE, _LIST = r"[\u2013\u2014-]|to", r"/|,|&|and"
_BARE = re.compile(
    rf"(?<![A-Za-z0-9_\[/.-])(?P<document>{_DOC_ID})[ \u00a0]?"
    rf"(?P<sections>{_SECTION}(?:\s*(?:(?:{_RANGE})\s*§?|(?:{_LIST})\s*§)\d{{1,3}})*)"
)
_SECTION_PART = re.compile(rf"(?P<separator>{_RANGE}|{_LIST})?\s*§?(?P<number>\d{{1,3}})")
_MAX_RANGE = 10
_MAX_DOC_ID = 40
_EVIDENCE_ID = re.compile(r"(?=[^A-Za-z]*[A-Za-z])[A-Za-z0-9][A-Za-z0-9._:#/-]{2,127}")
# A whole token: not glued to another identifier character on either side. Sentence punctuation (".", ":")
# may follow an ID, so it does not count.
_ID_CHAR = r"[A-Za-z0-9_#/-]"
# A record's own identifier, the last segment of a path-shaped evidence ID: a letter, a digit, 3+ characters.
_OWN_ID = re.compile(r"(?=[^A-Za-z]*[A-Za-z])(?=[^0-9]*[0-9])[A-Za-z0-9][A-Za-z0-9._:#-]{2,127}")


def _kb_citations(text: str) -> list[str]:
    return [m.group(0) for m in _KB_CITATION.finditer(text) if len(m.group("document")) <= _MAX_DOC_ID]


def _sections(text: str) -> list[int]:
    """`§2/§3` is [2, 3]; a range `§3-§5` is [3, 4, 5] (only its ends when reversed or over ten sections)."""
    numbers: list[int] = []
    for part in _SECTION_PART.finditer(text):
        number, separator = int(part.group("number")), part.group("separator")
        start = numbers[-1] if numbers else number
        is_range = separator is not None and re.fullmatch(_RANGE, separator) is not None
        if is_range and start < number <= start + _MAX_RANGE:
            numbers.extend(range(start + 1, number + 1))
        else:
            numbers.append(number)
    return numbers


def _given_document(document: str, given: set[str]) -> str | None:
    """The given document a written ID names: the same ID, or the only one ending in `-<ID>`."""
    if document in given:
        return document
    matches = [d for d in given if d.endswith(f"-{document}")]
    return matches[0] if len(matches) == 1 else None


def _answer_kb_citations(answer: str, given: set[str]) -> list[str]:
    """The answer's knowledge-base citations in `[DOC-ID §N]` form: bracketed, source notes and bare
    references to a given document. A shortened ID that names one given document is expanded to it."""
    found = [
        (m.group("document"), [m.group("section")])
        for m in _KB_CITATION.finditer(answer)
        if len(m.group("document")) <= _MAX_DOC_ID
    ]
    found += [
        (m.group("document"), m.group("sections").split(" and "))
        for m in _SOURCE_NOTE.finditer(answer)
        if len(m.group("document")) <= _MAX_DOC_ID
    ]
    citations = [
        f"[{_given_document(doc, given) or doc} {section}]" for doc, sections in found for section in sections
    ]
    for m in _BARE.finditer(answer):
        document = _given_document(m.group("document"), given)
        if document is not None:
            citations += [f"[{document} §{number}]" for number in _sections(m.group("sections"))]
    return citations


def _mentions(text: str, evidence_id: str) -> bool:
    return re.search(rf"(?<!{_ID_CHAR}){re.escape(evidence_id)}(?!{_ID_CHAR})", text) is not None


def _own_ids(evidence_ids: tuple[str, ...]) -> dict[str, str]:
    """Each path-shaped candidate's own identifier (its last segment), where that names it unambiguously: no
    other candidate ends in the same segment or is that segment."""
    by_segment: dict[str, list[str]] = {}
    for evidence_id in evidence_ids:
        segment = evidence_id.rsplit("/", 1)[-1]
        if segment != evidence_id and _OWN_ID.fullmatch(segment):
            by_segment.setdefault(segment, []).append(evidence_id)
    return {
        ids[0]: segment
        for segment, ids in by_segment.items()
        if len(ids) == 1 and segment not in evidence_ids
    }


def check_answer_citations(answer: str, context: AnswerContext) -> CitationCheck:
    sent = " ".join(context.sent.split())
    kb_sent = set(_kb_citations(sent))
    given = {m.group("document") for m in _KB_CITATION.finditer(sent)}
    cited: list[str] = []
    unresolved: list[str] = []
    for citation in dict.fromkeys(_answer_kb_citations(answer, given)):
        cited.append(citation)
        if citation not in kb_sent:
            unresolved.append(citation)
    candidates = tuple(dict.fromkeys(context.evidence_ids))
    own_ids = _own_ids(candidates)
    for evidence_id in candidates:
        if not _EVIDENCE_ID.fullmatch(evidence_id):
            continue
        own = own_ids.get(evidence_id)
        if not _mentions(answer, evidence_id) and not (own is not None and _mentions(answer, own)):
            continue
        cited.append(evidence_id)
        if f'id="{html.escape(evidence_id, quote=True)}"' not in sent:
            unresolved.append(evidence_id)
    if not cited:
        return CitationCheck("none", 0, 0)
    return CitationCheck(
        "unresolved" if unresolved else "resolved",
        cited=len(cited),
        unresolved=len(unresolved),
        unresolved_refs=tuple(unresolved[:MAX_REFS]),
    )


def evidence_coverage(check: CitationCheck | None) -> float | None:
    """Resolved citations ÷ cited, or None when nothing is cited (or the check did not run)."""
    if check is None or check.cited == 0:
        return None
    return (check.cited - check.unresolved) / check.cited
