"""Knowledge passages in the CIM's training format, and what the enterprise-rag profile's citation fields
mean: citation integrity (kept passages keep their header, every citation resolves), not per-passage
pinning. Omissions the Guard cannot verify are reported, never called verified. The recall floor and
`keep_all_passages` are profile settings."""

import pytest

from aiva_gateway.catalog import load_catalog
from aiva_gateway.catalog.models import ModelSpec
from aiva_gateway.config import REPO_ROOT
from aiva_gateway.contracts.cim import CimEvidence, CimMessage, CimRequest, CimResponse, RequiredFact
from aiva_gateway.optimize.cim import (
    EVIDENCE_PREAMBLE,
    PassageCounts,
    SimulatedContextIntelligence,
    evidence_block,
    passage_counts,
    with_passages,
)
from aiva_gateway.optimize.cim_live import NOTHING_TO_OMIT, cim_bypass_reason
from aiva_gateway.optimize.citations import Citation, numbered_section, passage_header
from aiva_gateway.optimize.context_guard import (
    DeterministicContextGuard,
    content_terms,
    document_family,
    omission_verified,
    passages_to_keep,
)
from aiva_gateway.optimize.knowledge import KbDocument, KbSection, evidence_text, section_citations

CATALOG = load_catalog(REPO_ROOT / "config")
RAG = CATALOG.context_profile("enterprise-rag")
NOC = CATALOG.context_profile("network-alarm-correlation")
assert RAG is not None and NOC is not None
TITLE = "Enterprise Roaming Bundle Discount Approval (GCT Group)"
PASSAGES = [
    CimEvidence(
        id="GCT-POL-014#2",
        text=evidence_text("GCT-POL-014", 3, TITLE, "Request process", "Requests use Form DA-6."),
    ),
    CimEvidence(
        id="GCT-POL-014#1",
        text=evidence_text(
            "GCT-POL-014", 2, TITLE, "Approval tiers", "Up to 10% the Sales Manager approves, as §3 sets out."
        ),
    ),
    CimEvidence(
        id="GCT-POL-215#1",
        text=evidence_text("GCT-POL-215", 2, "Network Vendor Service Levels", "Penalties", "2% per hour."),
    ),
]
QUESTION = [
    {"role": "user", "content": "Who approves a 10% roaming discount? Cite the document and section."}
]
guard = DeterministicContextGuard()


def request(evidence: list[CimEvidence] = PASSAGES) -> CimRequest:
    assert RAG is not None
    return CimRequest(
        task="rag",
        messages=[CimMessage.model_validate(m) for m in QUESTION],
        evidence=evidence,
        context_profile=RAG.id,
        target_budget_tokens=RAG.target_budget_tokens,
        required_fields=RAG.required_fields,
    )


def package(
    selected: list[CimEvidence],
    *,
    section: str = "§2",
    source: str = "GCT-POL-014#1",
    extra: str = "",
    texts: dict[str, str] | None = None,
) -> CimResponse:
    blocks = "\n".join(evidence_block(e.id, (texts or {}).get(e.id, e.text)) for e in selected)
    return CimResponse(
        intent="policy question",
        required_facts=[
            RequiredFact(field="document_id", value="GCT-POL-014", source_id=source),
            RequiredFact(field="section", value=section, source_id=source),
        ],
        selected_evidence_ids=[e.id for e in selected],
        omitted_evidence_ids=[e.id for e in PASSAGES if e not in selected],
        missing_fields=[],
        upstream_messages=[
            CimMessage(role="system", content=f"{EVIDENCE_PREAMBLE}\n{blocks}{extra}"),
            *request().messages,
        ],
        self_reported_complete=True,
    )


def test_passages_render_in_the_training_header_format() -> None:
    text = PASSAGES[0].text
    assert text.startswith(f"[GCT-POL-014 §3] {TITLE} - Request process\nRequests")
    header = passage_header(text)
    assert header is not None and header[0] == Citation("GCT-POL-014", "§3")
    assert passage_header("event_timestamp: 2026-10-14T06:02:11Z") is None


def test_kb_sections_keep_their_number_or_are_numbered_by_position() -> None:
    assert numbered_section("§4 Validity and renewal", 1) == (4, "Validity and renewal")
    assert numbered_section("Traveller 10 - Europe", 2) == (2, "Traveller 10 - Europe")
    document = KbDocument(
        document_id="D",
        title="T",
        classification="internal",
        version="1",
        sections=[KbSection(section="Eligibility", text="x"), KbSection(section="§1 Scope", text="y")],
    )
    with pytest.raises(ValueError, match="share citation numbers"):
        section_citations(document)


def test_citation_fields_pin_no_passage_whatever_the_rendering() -> None:
    assert RAG is not None
    assert passages_to_keep(PASSAGES, QUESTION, RAG) == []
    assert cim_bypass_reason(PASSAGES, QUESTION, RAG) is None
    # Field lines are citation values too: under this profile they pin nothing either.
    lines = [CimEvidence(id="a", text="document_id: GCT-POL-014\nsection: §3\nRequests use Form DA-6.")]
    assert passages_to_keep(lines, QUESTION, RAG) == []


def test_omitted_rag_passages_pass_and_only_the_question_term_rule_verifies_them() -> None:
    result = guard.verify(request(), package([PASSAGES[1]]), RAG)
    assert result.passed, result.failures
    assert {"citation-headers", "citation-resolution"} <= set(result.checks)
    # §3 of the kept policy is its sibling: unverified. The vendor penalties share no term with the question
    # and are another document: verified irrelevant.
    assert result.relevance_unverified == ("GCT-POL-014#2",)
    assert result.relevance_verified == ("GCT-POL-215#1",)
    # Nothing omitted: nothing left unverified.
    assert guard.verify(request(), package(PASSAGES), RAG).relevance_unverified == ()


def test_a_passage_the_cim_left_out_without_listing_it_is_unverified_too() -> None:
    # A v2 selection may name a passage in neither list (CIM v7 did, on 4 of ML's 150 held-out RAG cases).
    silent = package([PASSAGES[1]]).model_copy(update={"omitted_evidence_ids": ["GCT-POL-215#1"]})
    result = guard.verify(request(), silent, RAG)
    assert result.passed, result.failures
    assert result.relevance_unverified == ("GCT-POL-014#2",)  # not mentioned, and a sibling
    assert result.relevance_verified == ("GCT-POL-215#1",)


def test_a_kept_passage_must_keep_its_header() -> None:
    body = PASSAGES[1].text.split("\n", 1)[1]
    result = guard.verify(request(), package([PASSAGES[1]], texts={PASSAGES[1].id: body}), RAG)
    assert "citation header lost: selected evidence GCT-POL-014#1 lacks its header" in result.failures


def test_a_dangling_citation_in_the_package_fails() -> None:
    result = guard.verify(request(), package([PASSAGES[1]], extra="\nSee [GCT-POL-014 §5]."), RAG)
    assert not result.passed
    assert any(f.startswith("dangling citation: 1 citation(s)") for f in result.failures)


def test_a_citation_of_an_omitted_passage_does_not_resolve() -> None:
    # The request holds [GCT-POL-215 §2], but the package omits that passage: citing it is dangling.
    result = guard.verify(request(), package([PASSAGES[1]], extra="\nSee [GCT-POL-215 §2]."), RAG)
    assert any(f.startswith("dangling citation") for f in result.failures)


def test_probe_a_an_omitted_passage_citation_over_cim_written_text_fails() -> None:
    written = (
        "\n[GCT-POL-215 §2] Network Vendor Service Levels - Penalties\nVendors pay 2% per hour of delay."
    )
    result = guard.verify(request(), package([PASSAGES[1]], extra=written), RAG)
    assert any(f.startswith("dangling citation") for f in result.failures)


def test_probe_b_an_omitted_passage_citation_inside_a_kept_block_fails() -> None:
    kept = PASSAGES[1]
    texts = {kept.id: f"{kept.text}\nSee also [GCT-POL-215 §2]."}
    result = guard.verify(request(), package([kept], texts=texts), RAG)
    assert any(f.startswith("dangling citation") for f in result.failures)


def test_citations_of_kept_passages_and_of_the_conversation_resolve() -> None:
    kept = guard.verify(request(), package([PASSAGES[1]], extra="\nSee [GCT-POL-014 §2]."), RAG)
    assert kept.passed, kept.failures
    asked = request().model_copy(
        update={
            "messages": [CimMessage(role="user", content="Does [GCT-POL-215 §2] apply? Cite the section.")]
        }
    )
    response = package([PASSAGES[1]]).model_copy(
        update={"upstream_messages": [package([PASSAGES[1]]).upstream_messages[0], *asked.messages]}
    )
    said = guard.verify(asked, response, RAG)
    assert not any(f.startswith("dangling citation") for f in said.failures), said.failures
    # A passage's own cross-reference, copied verbatim in its own block, resolves.
    cross = CimEvidence(
        id="GCT-POL-014#4", text=evidence_text("GCT-POL-014", 5, TITLE, "Limits", "See [GCT-POL-215 §2].")
    )
    evidence = [*PASSAGES, cross]
    with_cross = package([PASSAGES[1], cross]).model_copy(
        update={"omitted_evidence_ids": [e.id for e in evidence if e not in (PASSAGES[1], cross)]}
    )
    result = guard.verify(request(evidence), with_cross, RAG)
    assert not any(f.startswith("dangling citation") for f in result.failures), result.failures


def test_a_citation_fact_must_name_its_source_passage() -> None:
    # "§3" appears verbatim in the §2 passage's text, but it is not that passage's citation.
    result = guard.verify(request(), package([PASSAGES[1]], section="§3"), RAG)
    assert "dangling citation: section is not the citation of GCT-POL-014#1" in result.failures


def test_a_citation_field_reported_missing_is_checked_against_headers() -> None:
    missing = package([PASSAGES[1]]).model_copy(update={"missing_fields": ["section"]})
    assert (
        "section: reported missing but present in the evidence"
        in guard.verify(request(), missing, RAG).failures
    )


def test_under_fact_profiles_omitted_knowledge_passages_are_unverified_and_records_are_not() -> None:
    assert NOC is not None
    note = CimEvidence(id="note-1", text="Synthetic shift note.")
    noc = request([PASSAGES[0], note]).model_copy(update={"required_fields": NOC.required_fields})
    response = CimResponse(
        intent="correlate",
        required_facts=[],
        selected_evidence_ids=[],
        omitted_evidence_ids=["GCT-POL-014#2", "note-1"],
        missing_fields=NOC.required_fields,
        upstream_messages=noc.messages,
        self_reported_complete=False,
    )
    assert guard.verify(noc, response, NOC).relevance_unverified == ("GCT-POL-014#2",)


def test_keep_all_passages_bypasses_the_cim_and_rejects_any_omission() -> None:
    assert RAG is not None
    keep_all = RAG.model_copy(update={"keep_all_passages": True})
    assert passages_to_keep(PASSAGES, QUESTION, keep_all) == PASSAGES
    assert cim_bypass_reason(PASSAGES, QUESTION, keep_all) == NOTHING_TO_OMIT
    result = guard.verify(request(), package([PASSAGES[1]]), keep_all)
    assert "context loss: omitted GCT-POL-215#1; the profile keeps every passage" in result.failures


def test_the_recall_floor_keeps_the_top_passages_and_the_guard_verifies_them() -> None:
    floored = with_passages(package([PASSAGES[1]]), PASSAGES[:1])
    assert floored.selected_evidence_ids == ["GCT-POL-014#1", "GCT-POL-014#2"]
    assert floored.omitted_evidence_ids == ["GCT-POL-215#1"]
    assert evidence_block(PASSAGES[0].id, PASSAGES[0].text) in floored.upstream_messages[0].content
    result = guard.verify(request(), floored, RAG)
    assert result.passed and result.relevance_unverified == ()
    assert result.relevance_verified == ("GCT-POL-215#1",)
    assert with_passages(floored, PASSAGES[:1]) is floored  # already kept: unchanged
    # The CIM kept nothing: the floor opens the evidence message.
    empty = package([]).model_copy(update={"upstream_messages": request().messages})
    assert with_passages(empty, PASSAGES[:1]).upstream_messages[0].content.startswith(EVIDENCE_PREAMBLE)


def test_passage_counts_follow_the_package() -> None:
    ids = [e.id for e in PASSAGES]
    floored = with_passages(package([PASSAGES[1]]), PASSAGES[:1])
    assert passage_counts(ids, floored.selected_evidence_ids) == PassageCounts(retrieved=3, kept=2, omitted=1)
    # An id that names no passage, or one selected twice, keeps nothing more.
    assert passage_counts(ids, [ids[0], ids[0], "GCT-POL-999#1"]) == (3, 1, 2)
    assert passage_counts([], []) == (0, 0, 0)


def test_the_recall_floor_counts_as_kept_for_the_bypass() -> None:
    assert RAG is not None
    assert cim_bypass_reason(PASSAGES[:1], QUESTION, RAG, floor=PASSAGES[:1]) == NOTHING_TO_OMIT
    assert cim_bypass_reason(PASSAGES, QUESTION, RAG, floor=PASSAGES[:1]) is None


async def test_the_simulated_cim_reads_citations_from_headers() -> None:
    cim = SimulatedContextIntelligence(CATALOG.context_profile)
    model = CATALOG.model(CATALOG.models.cim_model)
    assert isinstance(model, ModelSpec)
    result = await cim.package(request(), model)
    assert result.response is not None and result.response.missing_fields == []
    assert guard.verify(request(), result.response, RAG).passed


def test_the_question_term_rule() -> None:
    """An omission is verified irrelevant only with a citation header, no content term shared with the
    question or the required facts, and no kept passage of its document family (ADR-0009, option A)."""
    # Citation brackets, function words, the tenant's name and the data label are not content; plurals stem.
    assert content_terms("[GCT-POL-014 §2] Discounts for the synthetic GCT plans, up to 10%") == {
        "discount",
        "plan",
        "10",
    }
    assert content_terms("What penalties apply?") == {"penalty", "apply"}
    qatar = "[GCT-POL-114 §3] Enterprise Roaming Bundle Discount Approval (GCT Qatar) - Request process"
    family = "enterprise roaming bundle discount approval"
    assert document_family(qatar) == family
    assert document_family("[KB-ROAM §3] Roaming bundles - Traveller 10 - Europe") == "roaming bundles"
    vendor = PASSAGES[2].text
    assert omission_verified(vendor, content_terms(QUESTION[0]["content"]), set())
    assert not omission_verified(vendor, content_terms("What penalties apply?"), set())  # shares a term
    assert not omission_verified(f"{qatar}\nForm DA-7.", set(), {family})  # another entity's version: sibling
    assert not omission_verified("A passage with no citation header.", set(), set())  # nothing to cite
