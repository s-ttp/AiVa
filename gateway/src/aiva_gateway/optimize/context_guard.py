"""Context Sufficiency Guard (LLD §9): deterministic checks independent of the CIM's own claims.

A package passes only if nothing the answering model needs was lost or altered:
- evidence-references: every referenced id exists; nothing is both selected and omitted;
- non-empty-selection: a request with evidence keeps at least one passage. A package that omits every
  passage leaves the answering model nothing to ground on, and the Guard cannot tell that apart from a CIM
  that kept nothing because it was never trained on the request's shape (S6's notice step: no required
  fields, 13 tool outputs, all omitted, and the model invented the incident). It fails, so the full eligible
  context is sent instead;
- required-fields: every required field is reported, and a field reported missing is really absent;
- verbatim-values: each fact value appears verbatim in its (selected) source passage;
- payload-preservation: each fact value reaches the upstream payload;
  (values match as whole tokens everywhere: "2" is not found in "2026", nor "major" in "majority");
- citations: every selected passage is carried into the upstream payload as `id="<escaped id>"`;
- evidence-wrapper: one `<evidence>` block per selected passage and no stray open/close tags, so evidence
  copied by the CIM cannot break out of its data block (the gateway itself escapes it in `evidence_block`);
- citation-headers: every selected passage that has a citation header (`[DOC-ID §N] ...`, `citations.py`)
  keeps it inside its evidence block;
- citation-resolution: every citation the package emits resolves to what the package keeps. A citation
  fact names its source passage's own document or section. Inside an evidence block, a `[DOC-ID §N]` must
  be one that block's own source passage holds; outside blocks, it must name a kept passage (its header)
  or appear in the conversation. A dangling citation fails;
- omitted-facts: no omitted passage holds a required-field value that the payload lacks (context loss).
  Citation fields (`ContextProfile.citation`) name a passage's source, not a fact: they are checked by the
  two citation checks above and pin no passage. A profile that keeps all passages accepts no omission;
- chronology: every timestamp of the profile's chronology field in the evidence survives, unparsed or
  collapsed timestamps fail;
- numeric-fidelity: the payload introduces no number absent from the request and evidence, and (when the
  profile preserves numbers) every number of a selected passage survives;
- token-budget: the payload stays within the profile budget.
On failure the orchestrator restores the full eligible context; an unchecked package is never sent.

The Guard verifies that nothing the profile requires was lost, not that what the CIM left out was
irrelevant, except where the question-term rule holds (`omission_verified`, ADR-0009 2026-10-22 option A): a
left-out passage that shares no content term with the question or the CIM's required facts, and is no
sibling of a kept passage (the same document family, any entity or section), is reported in
`relevance_verified`. Every other passage left out on the CIM's judgement alone is reported in
`relevance_unverified`: under a profile with citation fields every passage the package does not keep,
otherwise every such knowledge passage (one with a citation header), whether the CIM listed it as omitted or
did not mention it. The orchestrator never serves a package with any (ADR-0009, 2026-10-22 amendment).
"""

import html
import re
from collections.abc import Iterable, Sequence
from datetime import datetime

from aiva_gateway.catalog.models import CitationFields, ContextProfile
from aiva_gateway.contracts.cim import CimEvidence, CimRequest, CimResponse
from aiva_gateway.core.canonical import estimate_tokens
from aiva_gateway.optimize.citations import CITATION, citations_in, passage_header
from aiva_gateway.pipeline.interfaces import GuardResult

BUDGET_TOLERANCE = 1.10
# Content terms for the question-term rule: words of three or more letters and any number, case-folded and
# plural-stemmed, minus function words. "gct" (the tenant's name) and "synthetic" (the POC's data label) are
# on nearly every passage and question, so they tell no passage apart. Citation brackets are not content:
# they name a source (`[GCT-POL-014 §2]`).
_TERM = re.compile(r"[a-z0-9]+")
_FUNCTION_WORDS = """
    a about above after again against all also am an and any are as at be because been before being below
    between both but by can could did do does doing down during each etc few for from further had has have
    having he her here hers him his how i if in into is it its itself just may me might more most must my no
    nor not now of off on once only or other our ours out over own per please same shall she should so some
    such tell than that the their theirs them then there these they this those through to too under until up
    very via was we were what when where which while who whom why will with would you your yours yourself
"""
STOPWORDS = frozenset([*_FUNCTION_WORDS.split(), "gct", "synthetic"])
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_OPEN_TAG = re.compile(r"<\s*evidence\b", re.IGNORECASE)
_CLOSE_TAG = re.compile(r"<\s*/\s*evidence\s*>", re.IGNORECASE)
_BLOCK = re.compile(r'<\s*evidence\s+id="([^"]*)"\s*>(.*?)<\s*/\s*evidence\s*>', re.IGNORECASE | re.DOTALL)

CHECKS = (
    "evidence-references",
    "non-empty-selection",
    "required-fields",
    "verbatim-values",
    "payload-preservation",
    "citations",
    "evidence-wrapper",
    "citation-headers",
    "citation-resolution",
    "omitted-facts",
    "chronology",
    "numeric-fidelity",
    "token-budget",
)


def _normalise(text: str) -> str:
    return " ".join(text.split())


def holds(text: str, value: str) -> bool:
    """`value` appears in `text` as a whole token, not glued to a letter or digit on either side: "2" is not
    found in "2026", nor "major" in "majority". Nor may it start or stop inside a number: "3,101" and "101.00"
    are not found in "USD 3,101.00", which still matches at the end of a sentence. Both are
    whitespace-normalised."""
    pattern = rf"(?<!\w)(?<!\d[.,]){re.escape(_normalise(value))}(?!\w)(?![.,]\d)"
    return re.search(pattern, _normalise(text)) is not None


def _parse_timestamp(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _field_values(field: str, text: str) -> list[str]:
    """Values written as `field: value` or `field = value` in a passage."""
    pattern = re.compile(rf"(?im)\b{re.escape(field)}\s*[:=]\s*([^\n;|<]+)")
    return [_normalise(m.group(1)) for m in pattern.finditer(text)]


def passage_values(field: str, text: str, citation: CitationFields | None) -> list[str]:
    """Values of `field` in a passage: its `field: value` lines, plus, for a citation field, the document or
    section of the passage's citation header."""
    values = _field_values(field, text)
    header = passage_header(text)
    if citation is not None and header is not None:
        if field == citation.document:
            values.append(header[0].document)
        if field == citation.section:
            values.append(header[0].section)
    return values


def pinned_fields(fields: Iterable[str], profile: ContextProfile | None) -> list[str]:
    """The required fields whose values no omitted passage may take with it: all but the citation fields."""
    exempt = profile.citation_fields if profile else frozenset()
    return [field for field in fields if field not in exempt]


def unpreserved_facts(text: str, fields: Iterable[str], kept: str) -> list[tuple[str, str]]:
    """The (field, value) pairs a passage holds that `kept` does not contain: what omitting it would lose.

    The Guard's omitted-facts check rejects a package for any of them; the CIM bypass uses the same function
    to find the passages no verifiable package can leave out (`passages_to_keep`).
    """
    return [
        (field, value) for field in fields for value in _field_values(field, text) if not holds(kept, value)
    ]


def passages_to_keep(
    evidence: Sequence[CimEvidence], messages: Sequence[dict[str, str]], profile: ContextProfile | None
) -> list[CimEvidence]:
    """Passages every package must keep to pass this Guard: each holds a pinned required-field or chronology
    value that appears in no other passage and no message (all passages, if the profile keeps them all)."""
    if profile is None:
        return []
    if profile.keep_all_passages:
        return list(evidence)
    pinned = pinned_fields(profile.required_fields, profile)
    fields = list(dict.fromkeys([*pinned, *filter(None, [profile.chronology_field])]))
    texts = [e.text for e in evidence]
    said = "\n".join(m["content"] for m in messages)
    return [
        passage
        for index, passage in enumerate(evidence)
        if unpreserved_facts(passage.text, fields, "\n".join([said, *texts[:index], *texts[index + 1 :]]))
    ]


def _stem(term: str) -> str:
    for suffix, replacement in (("ies", "y"), ("es", ""), ("s", ""), ("ing", ""), ("ed", "")):
        if len(term) > len(suffix) + 2 and term.endswith(suffix) and not term.endswith("ss"):
            return term[: -len(suffix)] + replacement
    return term


def content_terms(text: str) -> set[str]:
    """The terms the question-term rule compares: citation brackets removed, function words dropped."""
    words = _TERM.findall(CITATION.sub(" ", text.lower()))
    return {_stem(w) for w in words if w not in STOPWORDS and (len(w) > 2 or w.isdigit())}


def document_family(header_line: str) -> str:
    """A passage's document family: its header's title without the citation and the trailing entity. So
    `[GCT-POL-114 §3] Enterprise Roaming Bundle Discount Approval (GCT Qatar) - Request process` belongs to
    the family of every section of every entity's version of that policy."""
    title = CITATION.sub("", header_line).strip().split(" - ")[0]
    return re.sub(r"\s*\([^)]*\)\s*$", "", title).strip().casefold()


def omission_verified(text: str, asked: set[str], kept_families: set[str]) -> bool:
    """The question-term rule: a left-out passage is verified irrelevant only if it has a citation header,
    shares no content term with the question or the required facts (`asked`), and is no sibling of a kept
    passage (`kept_families`). Calibrated on ML & Data's held-out RAG cases: no needed passage passed it."""
    header = passage_header(text)
    if header is None or document_family(header[1]) in kept_families:
        return False
    return not content_terms(text) & asked


def _numbers(text: str) -> set[str]:
    return set(_NUMBER.findall(text))


def _id_attribute(evidence_id: str) -> str:
    """The ID as `evidence_block` writes it into the wrapper attribute."""
    return html.escape(" ".join(evidence_id.split()), quote=True)


class DeterministicContextGuard:
    version = "context-guard-v3"

    def verify(
        self, request: CimRequest, response: CimResponse, profile: ContextProfile | None
    ) -> GuardResult:
        failures: list[str] = []
        codes: list[str] = []

        def fail(code: str, message: str) -> None:
            failures.append(message)
            codes.append(code)

        citation = profile.citation if profile else None
        raw = {item.id: item.text for item in request.evidence}
        evidence = {evidence_id: _normalise(text) for evidence_id, text in raw.items()}
        headers = {evidence_id: passage_header(text) for evidence_id, text in raw.items()}
        selected, omitted = set(response.selected_evidence_ids), set(response.omitted_evidence_ids)
        upstream_text = _normalise("\n".join(m.content for m in response.upstream_messages))

        unknown = (selected | omitted) - evidence.keys()
        if unknown:
            fail("unknown_reference", f"unknown evidence references: {sorted(unknown)}")
        if selected & omitted:
            fail("selected_and_omitted", f"evidence both selected and omitted: {sorted(selected & omitted)}")
        if raw and not selected & raw.keys():
            fail("empty_selection", f"empty selection: all {len(raw)} evidence passages omitted")

        reported = {fact.field for fact in response.required_facts}
        for field in request.required_fields:
            if field not in reported:
                fail("required_field_missing", f"required field missing: {field}")
        for field in response.missing_fields:
            if any(passage_values(field, text, citation) for text in raw.values()):
                fail("reported_missing_but_present", f"{field}: reported missing but present in the evidence")

        for fact in response.required_facts:
            source = evidence.get(fact.source_id)
            value = _normalise(fact.value)
            if source is None or fact.source_id not in selected:
                fail("source_not_selected", f"{fact.field}: source {fact.source_id} not in selected evidence")
            elif not holds(source, value):
                fail(
                    "value_not_verbatim", f"{fact.field}: value not found verbatim in source {fact.source_id}"
                )
            if value and not holds(upstream_text, value):
                fail("value_not_preserved", f"{fact.field}: value not preserved in the upstream payload")
            if (
                profile is not None
                and fact.field in profile.citation_fields
                and fact.source_id in raw
                and value not in passage_values(fact.field, raw[fact.source_id], citation)
            ):
                fail(
                    "dangling_citation",
                    f"dangling citation: {fact.field} is not the citation of {fact.source_id}",
                )

        for evidence_id in sorted(selected & evidence.keys()):
            if f'id="{_id_attribute(evidence_id)}"' not in upstream_text:
                fail(
                    "citation_lost",
                    f"citation lost: selected evidence {evidence_id} not in the upstream payload",
                )

        wrapped = len(selected & evidence.keys())
        opened, closed = len(_OPEN_TAG.findall(upstream_text)), len(_CLOSE_TAG.findall(upstream_text))
        if opened != closed or opened != wrapped:
            fail(
                "wrapper_integrity",
                f"evidence wrapper integrity: {opened} open / {closed} close tags for {wrapped} passages",
            )

        blocks: dict[str, list[str]] = {}
        for block in _BLOCK.finditer(upstream_text):
            blocks.setdefault(block.group(1), []).append(block.group(2))
        for evidence_id in sorted(selected & raw.keys()):
            header = headers[evidence_id]
            if header and not any(header[1] in body for body in blocks.get(_id_attribute(evidence_id), [])):
                fail(
                    "citation_header_lost",
                    f"citation header lost: selected evidence {evidence_id} lacks its header",
                )
        # A citation resolves only to what the package keeps: inside a block, to that block's own source;
        # outside blocks, to a kept passage's header or to the conversation.
        sources = {_id_attribute(evidence_id): evidence_id for evidence_id in raw}
        dangling = 0
        for block in _BLOCK.finditer(upstream_text):
            source = sources.get(block.group(1))
            own = citations_in(evidence[source]) if source is not None else set()
            dangling += len(citations_in(block.group(2)) - own)
        kept = {header[0] for evidence_id in selected & raw.keys() if (header := headers[evidence_id])}
        said = citations_in(_normalise("\n".join(m.content for m in request.messages)))
        dangling += len(citations_in(_BLOCK.sub(" ", upstream_text)) - kept - said)
        if dangling:
            fail(
                "dangling_citation",
                f"dangling citation: {dangling} citation(s) in the payload resolve to no kept source",
            )

        pinned = pinned_fields(request.required_fields, profile)
        for evidence_id in sorted(omitted & raw.keys()):
            if profile is not None and profile.keep_all_passages:
                fail("context_loss", f"context loss: omitted {evidence_id}; the profile keeps every passage")
                continue
            for field, _value in unpreserved_facts(raw[evidence_id], pinned, upstream_text):
                fail("context_loss", f"context loss: omitted {evidence_id} holds {field} not in the payload")

        if profile and profile.chronology_field:
            field = profile.chronology_field
            stamps = [f.value for f in response.required_facts if f.field == field]
            if any(_parse_timestamp(s) is None for s in stamps):
                fail("chronology", f"{field}: unparseable timestamp")
            timeline = {v for text in evidence.values() for v in _field_values(field, text)}
            lost = sorted(v for v in timeline if not holds(upstream_text, v))
            if lost:
                fail("chronology", f"{field}: {len(lost)} evidence timestamp(s) collapsed or dropped")

        allowed_numbers = _numbers(" ".join(m.content for m in request.messages))
        for item in request.evidence:
            allowed_numbers |= _numbers(item.text) | _numbers(item.id) | _numbers(_id_attribute(item.id))
        invented = sorted(_numbers(upstream_text) - allowed_numbers)
        if invented:
            fail(
                "numeric_fidelity",
                f"numeric fidelity: {len(invented)} number(s) not present in the request or evidence",
            )
        if profile is None or profile.preserve_numbers:
            upstream_numbers = _numbers(upstream_text)
            for evidence_id in sorted(selected & evidence.keys()):
                if _numbers(evidence[evidence_id]) - upstream_numbers:
                    fail("numeric_fidelity", f"numeric fidelity: numbers of {evidence_id} altered or dropped")

        upstream_tokens = sum(estimate_tokens(m.content) for m in response.upstream_messages)
        if upstream_tokens > request.target_budget_tokens * BUDGET_TOLERANCE:
            fail(
                "token_budget_exceeded",
                f"token budget exceeded: {upstream_tokens} > {request.target_budget_tokens}",
            )

        # Passages kept out on the CIM's relevance judgement alone, listed as omitted or not mentioned at all
        # (a v2 selection can leave a passage out of both lists): verified only where the question-term rule
        # holds.
        exempt = profile.citation_fields if profile else frozenset()
        asked = content_terms(" ".join(m.content for m in request.messages if m.role == "user"))
        asked |= content_terms(" ".join(f.value for f in response.required_facts if f.field not in exempt))
        kept_families = {
            document_family(header[1]) for i in selected & raw.keys() if (header := headers[i]) is not None
        }
        left_out = sorted(
            evidence_id
            for evidence_id in raw.keys() - selected
            if citation is not None or headers[evidence_id] is not None
        )
        verified = [i for i in left_out if omission_verified(raw[i], asked, kept_families)]
        return GuardResult(
            passed=not failures,
            failures=tuple(failures),
            failure_codes=tuple(codes),
            checks=CHECKS,
            relevance_unverified=tuple(i for i in left_out if i not in verified),
            relevance_verified=tuple(verified),
        )
