"""Scenario harness (LLD §15): synthetic fixtures, a synthetic knowledge base and replayed providers.

External models (Gemini Flash-Lite, DeepSeek, Claude) run on recorded exchanges (`replayed`) and never
contact a provider in CI. GCT-Qwen (Endpoint B) and the CIM (Endpoint A) stay simulated and labelled so
until ML & Data deploys them. Re-record the external exchanges with real provider calls (spends cents;
needs HF_TOKEN, AIVA_ANTHROPIC_API_KEY and GEMINI_API_KEY):

    AIVA_RECORD_PROVIDERS=1 uv run pytest gateway/tests/scenarios -q

Recordings hold request and response bodies of synthetic prompts only, never headers or credentials.
"""

import json
import os
import uuid
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from sqlalchemy import delete

from aiva_gateway.catalog.models import ModelSpec
from aiva_gateway.config import REPO_ROOT, Settings
from aiva_gateway.contracts.cim import CimMessage, CimRequest, CimResponse
from aiva_gateway.core.canonical import estimate_tokens
from aiva_gateway.core.enums import Provenance
from aiva_gateway.db.models import KnowledgeChunkRow, KnowledgeDocumentRow
from aiva_gateway.optimize.cache import RedisResponseCache
from aiva_gateway.optimize.cim import EVIDENCE_PREAMBLE, evidence_block
from aiva_gateway.optimize.embeddings import EMBEDDING_DIMENSIONS, HashingEmbedder, Vector
from aiva_gateway.optimize.knowledge import ingest_documents, load_jsonl
from aiva_gateway.pipeline.interfaces import (
    CimCallResult,
    InferenceAdapter,
    InferenceRequest,
    InferenceResult,
    TokenUsage,
)
from aiva_gateway.services import Services, build_services
from tests.conftest import with_overrides

FIXTURES = Path(REPO_ROOT, "gateway", "tests", "fixtures", "scenarios")
RECORDINGS = FIXTURES / "recordings"
GCT_KB = Path(REPO_ROOT, "data", "synthetic", "kb", "gct_kb.jsonl")  # ML & Data's synthetic KB (#7)
RECORDING = os.environ.get("AIVA_RECORD_PROVIDERS") == "1"
MODE = "record" if RECORDING else "replay"
# What an external provider's output is labelled: `replayed` in CI, `measured` while recording.
PROVIDER = Provenance.MEASURED.value if RECORDING else Provenance.REPLAYED.value
EXTERNAL = {"gemini-flash-lite", "deepseek-v4-1-flash", "claude-flagship"}


def scenario_fixture(scenario_id: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{scenario_id}.json").read_text(encoding="utf-8"))
    return data


@pytest.fixture(scope="package")
async def scenario_kb(services: Services) -> AsyncIterator[None]:
    """ML & Data's synthetic knowledge base plus the scenario rows each primary proof cites.

    Package-scoped: the knowledge base exists only while scenario tests run, so tests elsewhere that run
    retrieval against the shared test database never see it, whatever the order."""

    async def clear() -> None:
        async with services.sessionmaker() as session, session.begin():
            await session.execute(delete(KnowledgeChunkRow))
            await session.execute(delete(KnowledgeDocumentRow))

    await clear()
    rows = load_jsonl(GCT_KB) + load_jsonl(FIXTURES / "kb_scenarios.jsonl")
    async with services.sessionmaker() as session, session.begin():
        await ingest_documents(
            session, tenant_id="gct", documents=rows, embedder=HashingEmbedder(), dlp=services.dlp
        )
    yield
    await clear()


@pytest.fixture(scope="package")
async def scenario_services(settings: Settings, scenario_kb: None) -> AsyncIterator[Services]:
    svc = build_services(
        settings.model_copy(
            update={"inference_mode": MODE, "recordings_dir": RECORDINGS, "response_cache_enabled": False}
        )
    )
    yield svc
    await svc.aclose()


# --- Test doubles (labelled `simulated`; they stand in for components not deployed in CI) -----------


class ConstantEmbedder:
    """Every text is maximally similar, isolating the cache's factual-equivalence guard from the embedding
    model (the pinned bge model is not downloaded in CI)."""

    model_id = "constant-test"
    dimensions = EMBEDDING_DIMENSIONS

    def embed(self, texts: Sequence[str], *, query: bool = False) -> Vector:
        rows = np.zeros((len(texts), self.dimensions), dtype=np.float32)
        rows[:, 0] = 1.0
        return rows


def with_cache(svc: Services) -> Services:
    """The exact + semantic response cache in a namespace of its own."""
    cache = RedisResponseCache(
        svc.redis,
        embedder=ConstantEmbedder(),
        semantic_threshold=0.95,
        namespace=f"aiva:scenario:{uuid.uuid4().hex}",
    )
    return with_overrides(svc, cache=cache)


class PruningCim:
    """A deterministic CIM stand-in that prunes: it keeps the evidence that shares terms with the question
    and the last `keep_turns` messages, omits the rest and reports what it removed. It is labelled
    `simulated`; Endpoint A replaces it once deployed. The Context Guard verifies its package like any other.
    """

    revision = "scripted-pruning-cim (test)"

    def __init__(self, keep_turns: int = 3, min_overlap: int = 3) -> None:
        self.keep_turns, self.min_overlap = keep_turns, min_overlap

    @staticmethod
    def _terms(text: str) -> set[str]:
        return {w.strip(".,?!:;()").lower() for w in text.split() if len(w.strip(".,?!:;()")) > 3}

    async def package(self, request: CimRequest, model: ModelSpec) -> CimCallResult:
        question = next((m.content for m in reversed(request.messages) if m.role == "user"), "")
        terms = self._terms(question)
        selected = [e for e in request.evidence if len(terms & self._terms(e.text)) >= self.min_overlap]
        omitted = [e.id for e in request.evidence if e not in selected]
        kept = request.messages[-self.keep_turns :]
        blocks = "\n".join(evidence_block(e.id, e.text) for e in selected)
        upstream = [CimMessage(role="system", content=f"{EVIDENCE_PREAMBLE}\n{blocks}")] if blocks else []
        upstream.extend(kept)
        response = CimResponse(
            intent=request.task,
            required_facts=[],
            selected_evidence_ids=[e.id for e in selected],
            omitted_evidence_ids=omitted,
            missing_fields=[],
            upstream_messages=upstream,
            self_reported_complete=True,
        )
        tokens = sum(estimate_tokens(m.content) for m in request.messages) + sum(
            estimate_tokens(e.text) for e in request.evidence
        )
        return CimCallResult(
            response=response,
            usage=TokenUsage(
                input_tokens=tokens,
                output_tokens=estimate_tokens(response.model_dump_json()),
                provenance=Provenance.SIMULATED,
            ),
            revision=self.revision,
            latency_ms=1.0,
            provenance=Provenance.SIMULATED,
        )


class _Spy:
    def __init__(self, inner: InferenceAdapter, seen: list[InferenceRequest]) -> None:
        self.inner, self.seen = inner, seen

    async def complete(self, request: InferenceRequest) -> InferenceResult:
        self.seen.append(request)
        return await self.inner.complete(request)


class CapturingAdapters:
    """Records every upstream and baseline request (the exact payload a model received)."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.requests: list[InferenceRequest] = []

    def for_model(self, model: ModelSpec) -> InferenceAdapter:
        return _Spy(self.inner.for_model(model), self.requests)

    async def aclose(self) -> None:
        return None


def capturing(svc: Services) -> tuple[Services, CapturingAdapters]:
    adapters = CapturingAdapters(svc.adapters)
    return with_overrides(svc, adapters=adapters), adapters
