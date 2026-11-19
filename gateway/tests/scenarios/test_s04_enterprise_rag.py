"""Scenario 4 — Enterprise RAG (LLD §15).

Primary proof: approved documents, citations, retrieval/context sufficiency.

- Approved documents: retrieval applies access control before ranking. Only internal documents granted
  to the knowledge portal, and in force today, can be retrieved. Confidential, expired and other
  applications' documents never are.
- Citations: retrieved passages carry the header the CIM is trained on, `[DOC-ID §N] Title - Heading`. Every
  passage the package keeps reaches the model inside its evidence wrapper with its ID and its header
  (Context Guard `citations`, `citation-headers`), every citation resolves to a passage of the request
  (`citation-resolution`), and the answer cites the policy document. The assurance check
  `answer-citations` reports any section the answer cites that the model was not given: the recorded answer
  cites only passages it was given, so the check reports every citation resolved.
- Context sufficiency: the `enterprise-rag` profile requires `document_id` and `section` as citation
  fields: checked for integrity, not pinned to every passage. Evidence without them fails the Guard, and
  the full eligible context is used instead ("Context sufficiency check failed; safe fallback applied"). So
  does a package with a dangling citation.
- Honest labelling: a passage the CIM omits is left out on its relevance judgement alone, which the Guard
  verifies only by the question-term rule (no content term shared with the question or the required facts,
  no sibling of a kept passage). Omissions it cannot verify are put back, and only verified ones are dropped
  (`verified`); with none verified, the full context is sent (ADR-0009, option A). The two top-ranked
  retrieved passages are always kept (recall floor).
"""

from dataclasses import replace

import pytest
from sqlalchemy import select

from aiva_gateway.catalog.loader import Catalog
from aiva_gateway.contracts.cim import CimMessage, CimResponse, RequiredFact
from aiva_gateway.db.models import ContextPackageRow
from aiva_gateway.optimize.cim import EVIDENCE_PREAMBLE, SimulatedContextIntelligence, evidence_block
from aiva_gateway.optimize.cim_live import build_package
from aiva_gateway.optimize.citations import passage_header
from aiva_gateway.optimize.retrieval import NullRetriever
from tests.conftest import console, with_overrides
from tests.scenarios.conftest import PROVIDER, capturing, scenario_fixture

APP = "gct-knowledge-portal"
DANGLING = "[GCT-POL-014 §9]"


class LowestRankedCim(SimulatedContextIntelligence):
    """Keeps only the lowest-ranked passage and cites it; omits the rest. A deterministic stand-in."""

    revision = "lowest-ranked-cim (test)"

    async def package(self, request, model):  # type: ignore[no-untyped-def]
        result = await super().package(request, model)
        kept = request.evidence[-1]
        header = passage_header(kept.text)
        assert result.response is not None and header is not None
        response = CimResponse(
            intent="policy question",
            required_facts=[
                RequiredFact(field="document_id", value=header[0].document, source_id=kept.id),
                RequiredFact(field="section", value=header[0].section, source_id=kept.id),
            ],
            selected_evidence_ids=[kept.id],
            omitted_evidence_ids=[e.id for e in request.evidence[:-1]],
            missing_fields=[],
            upstream_messages=[
                CimMessage(
                    role="system", content=f"{EVIDENCE_PREAMBLE}\n{evidence_block(kept.id, kept.text)}"
                ),
                *request.messages,
            ],
            self_reported_complete=True,
        )
        return replace(result, response=response)


class PrimaryOnlyCim(SimulatedContextIntelligence):
    """Keeps only the primary document's request-process passage and cites it; omits the rest."""

    revision = "primary-only-cim (test)"

    async def package(self, request, model):  # type: ignore[no-untyped-def]
        result = await super().package(request, model)
        kept = next(e for e in request.evidence if e.text.startswith("[GCT-POL-014 §3]"))
        response = CimResponse(
            intent="policy question",
            required_facts=[
                RequiredFact(field="document_id", value="GCT-POL-014", source_id=kept.id),
                RequiredFact(field="section", value="§3", source_id=kept.id),
            ],
            selected_evidence_ids=[kept.id],
            omitted_evidence_ids=[e.id for e in request.evidence if e.id != kept.id],
            missing_fields=[],
            upstream_messages=build_package(request, [kept.id]),
            self_reported_complete=True,
        )
        return replace(result, response=response)


class DanglingCitationCim(SimulatedContextIntelligence):
    """Adds a citation of a section that no retrieved passage holds."""

    revision = "dangling-citation-cim (test)"

    async def package(self, request, model):  # type: ignore[no-untyped-def]
        result = await super().package(request, model)
        assert result.response is not None
        system, *rest = result.response.upstream_messages
        tampered = CimMessage(role="system", content=f"{system.content}\nSee also {DANGLING}.")
        return replace(
            result, response=result.response.model_copy(update={"upstream_messages": [tampered, *rest]})
        )


async def _package(db, run_id: str) -> ContextPackageRow:  # type: ignore[no-untyped-def]
    return (
        await db.execute(select(ContextPackageRow).where(ContextPackageRow.run_id == run_id))
    ).scalar_one()


async def test_answers_from_approved_documents_and_flags_sections_it_was_not_given(
    scenario_services, tester_client, db
) -> None:  # type: ignore[no-untyped-def]
    fixture = scenario_fixture("enterprise-rag")
    svc, captured = capturing(scenario_services)
    async with tester_client(svc=svc) as client:
        body = (await client.post("/v1/ai/chat", json={**fixture["request"], **console(APP)})).json()
    assert body["status"] == "completed", body.get("error")
    assert body["execution_path"] == "cim-assisted"
    assert body["cim"]["sufficiency"] == "verified"
    assert body["cim"]["message"] == "Context package generated; sufficiency verified"

    package = await _package(db, body["run_id"])
    documents = {ref.split("#")[0] for ref in package.evidence_refs}
    assert fixture["primary_doc"] in documents
    assert documents <= set(fixture["approved_docs"])
    assert not documents & set(
        fixture["never_retrieved"]
    )  # confidential, expired, not yet in force, other app
    checks = set(package.guard_details["checks"])
    assert {"citations", "citation-headers", "citation-resolution"} <= checks
    assert package.guard_details["failures"] == []
    assert package.guard_details["sufficiency_basis"] == "guard-verified"  # this package omitted nothing

    [request] = captured.requests
    payload = "\n".join(m["content"] for m in request.messages)
    for evidence_id in package.retained_ids:
        assert f'id="{evidence_id}"' in payload
    assert f"[{fixture['primary_doc']} §" in payload  # passages reach the model with their citation header
    # Grounded in knowledge passages only, so the cheapest qualified model answers (ADR-0009, 2026-10-22
    # grounded-answers amendment), at its grounded-answer temperature.
    assert body["selected_model"] == "deepseek-v4-1-flash" and body["response"]["provenance"] == PROVIDER
    assert request.temperature == 0
    assert any(document in body["response"]["content"] for document in documents)  # cites a kept document
    # `answer-citations` reports the sections the answer cites that the model was not given. The answer
    # recorded on DeepSeek V4.1 Flash at temperature 0 (2026-10-22) cites three sections and their three
    # evidence IDs, every one a passage it was given. (An earlier pin's answer took three section numbers from
    # the evidence id's chunk ordinal: issue #155.)
    citations = body["assurance"]["answer_citations"]
    assert citations == {
        "status": "resolved",
        "cited": 6,
        "unresolved": 0,
        "unresolved_refs": [],
    }, "answer-citations must report exactly the sections the model was not given"


async def test_insufficient_context_falls_back_to_the_full_eligible_context(
    scenario_services, tester_client, db
) -> None:  # type: ignore[no-untyped-def]
    fixture = scenario_fixture("enterprise-rag")
    svc, captured = capturing(with_overrides(scenario_services, retriever=NullRetriever()))
    payload = {**fixture["request"], "evidence": fixture["insufficient_evidence"], **console(APP)}
    async with tester_client(svc=svc) as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed", body.get("error")
    assert body["cim"]["sufficiency"] == "failed-fallback-applied"
    assert body["cim"]["message"] == "Context sufficiency check failed; safe fallback applied"
    package = await _package(db, body["run_id"])
    assert package.validation == "failed-fallback-applied"
    assert set(package.missing_fields) == {"document_id", "section"}
    # The unchecked package is never sent: the model got the request and its evidence, as supplied.
    [request] = captured.requests
    sent = "\n".join(m["content"] for m in request.messages)
    assert fixture["insufficient_evidence"][0]["text"] in sent
    # Evidence without a citation header is an operational record: Gemini Flash-Lite still qualifies and
    # answers at its record-grounded temperature (ADR-0009, 2026-10-22 grounded-answers amendment).
    assert (body["selected_model"], request.temperature) == ("gemini-flash-lite", 0.3)
    assert request.messages[-1]["content"] == fixture["request"]["messages"][-1]["content"]


async def test_a_trim_whose_omissions_are_unverified_is_never_served_and_the_full_context_is_sent(
    scenario_services, services, tester_client, db
) -> None:  # type: ignore[no-untyped-def]
    # The upstream call is simulated here: this test is about which context reaches it and how it is labelled.
    fixture = scenario_fixture("enterprise-rag")
    svc, captured = capturing(
        with_overrides(scenario_services, cim=LowestRankedCim(), adapters=services.adapters)
    )
    async with tester_client(svc=svc) as client:
        body = (
            await client.post("/v1/ai/chat", json={**fixture["request"], **console(APP), "trace": True})
        ).json()
    assert body["status"] == "completed", body.get("error")
    package = await _package(db, body["run_id"])
    retrieved = package.evidence_refs
    assert len(retrieved) > 3
    # The CIM kept only the last passage; the recall floor kept the two top-ranked ones.
    assert package.retained_ids == [retrieved[-1], *retrieved[:2]]
    assert package.guard_details["recall_floor_added"] == retrieved[:2]
    omitted = len(retrieved) - 3
    assert package.removed_ids == retrieved[2:-1]
    # Verified for citations and numbers, but not for the relevance of what was left out: that trim is never
    # served (ADR-0009, 2026-10-22 amendment). It is a failed check, and the full context is sent.
    assert package.guard_details["failures"] == []
    assert body["cim"]["sufficiency"] == package.validation == "failed-fallback-applied"
    assert body["cim"]["message"] == "Context sufficiency check failed; safe fallback applied"
    assert package.guard_details["relevance_unverified"] == {
        "omitted_passages": omitted,
        "ids": sorted(retrieved[2:-1]),
    }
    assert package.guard_details["sufficiency_basis"].startswith("relevance-unverified")
    [request] = captured.requests
    payload = "\n".join(m["content"] for m in request.messages)
    assert all(f'id="{i}"' in payload for i in retrieved)  # every retrieved passage reaches the model
    spans = {span["name"]: span["attributes"] for span in body["trace"]}
    assert spans["cim"]["aiva.cim.recall_floor_added"] == 2
    assert spans["context_guard"]["aiva.guard.relevance_unverified"] == omitted


@pytest.mark.parametrize("cim_budget", [None, 400], ids=["profile-budget", "put-back-over-the-cim-budget"])
async def test_omissions_the_question_term_rule_verifies_are_dropped_and_the_rest_are_put_back(
    scenario_services, services, tester_client, db, monkeypatch, cim_budget
) -> None:  # type: ignore[no-untyped-def]
    if cim_budget is not None:
        # The CIM's package fits this budget; with its siblings put back it does not. A package with passages
        # put back is bounded by the full context, which the fallback would send, so it is still served.
        original = Catalog.context_profile

        def small(self, profile_id):  # type: ignore[no-untyped-def]
            profile = original(self, profile_id)
            return profile.model_copy(update={"target_budget_tokens": cim_budget}) if profile else profile

        monkeypatch.setattr(Catalog, "context_profile", small)
    # Every retrieved passage is a version of the roaming-discount policy; the supplied vendor passage is not.
    fixture = scenario_fixture("enterprise-rag")
    vendor = {
        "id": "GCT-POL-215#1",
        "text": "[GCT-POL-215 §2] Network Vendor Service Levels - Penalties\n2% per hour.",
    }
    svc, captured = capturing(
        with_overrides(scenario_services, cim=PrimaryOnlyCim(), adapters=services.adapters)
    )
    payload = {**fixture["request"], "evidence": [vendor], **console(APP), "trace": True}
    async with tester_client(svc=svc) as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed", body.get("error")
    package = await _package(db, body["run_id"])
    retrieved = [i for i in package.evidence_refs if i != vendor["id"]]
    details = package.guard_details
    # The siblings of the kept passage cannot be verified: they are put back. The vendor passage shares no
    # term with the question or the required facts and is another document: verified irrelevant, dropped.
    assert details["relevance_verified"] == {"omitted_passages": 1, "ids": [vendor["id"]]}
    assert details["relevance_unverified"] == {"omitted_passages": 0, "ids": []}
    assert set(details["relevance_put_back"]) | set(details.get("recall_floor_added", [])) | {
        package.retained_ids[0]
    } == set(retrieved)
    assert details["sufficiency_basis"].startswith("question-term-verified")
    assert body["cim"]["sufficiency"] == package.validation == "verified"
    assert body["cim"]["message"] == "Context package generated; sufficiency verified"
    [request] = captured.requests
    sent = "\n".join(m["content"] for m in request.messages)
    assert all(f'id="{i}"' in sent for i in retrieved) and vendor["id"] not in sent
    assert body["usage"]["constructed_context_tokens"] < body["usage"]["original_context_tokens"]
    spans = {span["name"]: span["attributes"] for span in body["trace"]}
    assert spans["context_guard"]["aiva.guard.relevance_verified"] == 1
    assert spans["context_guard"]["aiva.guard.relevance_put_back"] == len(details["relevance_put_back"])


async def test_a_dangling_citation_fails_the_guard_and_the_full_context_is_used(
    scenario_services, services, tester_client, db
) -> None:  # type: ignore[no-untyped-def]
    fixture = scenario_fixture("enterprise-rag")
    svc, captured = capturing(
        with_overrides(scenario_services, cim=DanglingCitationCim(), adapters=services.adapters)
    )
    async with tester_client(svc=svc) as client:
        body = (await client.post("/v1/ai/chat", json={**fixture["request"], **console(APP)})).json()
    assert body["status"] == "completed", body.get("error")
    assert body["cim"]["sufficiency"] == "failed-fallback-applied"
    package = await _package(db, body["run_id"])
    assert any(f.startswith("dangling citation") for f in package.guard_details["failures"])
    assert "dangling_citation" in package.guard_details["failure_codes"]  # the stable code Insights counts
    [request] = captured.requests
    payload = "\n".join(m["content"] for m in request.messages)
    assert DANGLING not in payload and fixture["primary_doc"] in payload  # the full retrieved context
