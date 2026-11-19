"""Retrieval (LLD §3): BM25 + pgvector, reciprocal-rank fusion, and an optional cross-encoder rerank
(off by default, `AIVA_RERANKER_BACKEND=onnx` enables it; ADR-0009).

Access control is applied once, in Python, before ranking: tenant, application grant, validity dates,
classification, DLP and prompt-injection screening: a chunk with indicators is not eligible, nor is one
not screened by the running detector version (`injection_detector`). A retrieval that holds chunks back
for screening records how many on its span (`aiva.retrieval.injection_held_back`). Only public and
internal, DLP-clean chunks are ever eligible, so confidential documents can never be pulled into a request
bound for CIM or an external model. Retrieval runs on the CIM-assisted path and, for context profiles that
opt in, on the sovereign-direct path, where its passages reach only the sovereign model and only those whose
cosine similarity to the question reaches the profile's floor are kept (ADR-0020). Retrieved text is
evidence (untrusted data), never instructions.

The BM25 index is held in memory per tenant and rebuilt when the knowledge-base version changes. The
version (documents, versions, content hashes, embedding model) is part of every response-cache key.
"""

import asyncio
import math
import time
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime

from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aiva_gateway.catalog.models import ContextProfile
from aiva_gateway.contracts.cim import CimEvidence
from aiva_gateway.core.canonical import sha256_hex
from aiva_gateway.core.enums import Classification
from aiva_gateway.db.models import KnowledgeChunkRow, KnowledgeDocumentRow
from aiva_gateway.optimize.citations import passage_header
from aiva_gateway.optimize.embeddings import Embedder, Reranker, embed_async, terms

RETRIEVABLE = frozenset({Classification.PUBLIC.value, Classification.INTERNAL.value})
RRF_K = 60


class NullRetriever:
    async def knowledge_version(self, tenant_id: str) -> str:
        return "kb-none"

    async def retrieve(
        self,
        *,
        query: str,
        tenant_id: str,
        application_id: str,
        profile: ContextProfile | None,
        limit: int,
        min_cosine: float | None = None,
    ) -> list[CimEvidence]:
        return []


@dataclass(frozen=True)
class IndexedChunk:
    evidence_id: str
    text: str
    classification: str
    application_ids: tuple[str, ...]
    effective_from: date | None
    expires_on: date | None
    dlp_clean: bool
    injection_clean: bool

    def permitted(self, application_id: str, today: date) -> bool:
        return self.injection_clean and self.accessible(application_id, today)

    def accessible(self, application_id: str, today: date) -> bool:
        """Every check but prompt-injection screening."""
        return (
            self.dlp_clean
            and self.classification in RETRIEVABLE
            and (not self.application_ids or application_id in self.application_ids)
            and (self.effective_from is None or self.effective_from <= today)
            and (self.expires_on is None or today <= self.expires_on)
        )


@dataclass
class Bm25Index:
    """Okapi BM25 (k1=1.5, b=0.75) over chunk terms."""

    chunks: list[IndexedChunk]
    k1: float = 1.5
    b: float = 0.75
    _tf: list[Counter[str]] = field(default_factory=list)
    _df: Counter[str] = field(default_factory=Counter)
    _lengths: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        for chunk in self.chunks:
            counts = Counter(terms(chunk.text))
            self._tf.append(counts)
            self._lengths.append(sum(counts.values()))
            self._df.update(counts.keys())

    def search(self, query: str, eligible: set[int], k: int) -> list[int]:
        if not self.chunks:
            return []
        n, average = len(self.chunks), (sum(self._lengths) / len(self.chunks)) or 1.0
        query_terms = set(terms(query))
        scores: list[tuple[float, int]] = []
        for i in eligible:
            tf, length, score = self._tf[i], self._lengths[i], 0.0
            for term in query_terms:
                freq = tf.get(term, 0)
                if not freq:
                    continue
                idf = math.log(1 + (n - self._df[term] + 0.5) / (self._df[term] + 0.5))
                score += (
                    idf * freq * (self.k1 + 1) / (freq + self.k1 * (1 - self.b + self.b * length / average))
                )
            if score > 0:
                scores.append((score, i))
        return [i for _, i in sorted(scores, key=lambda s: (-s[0], self.chunks[s[1]].evidence_id))[:k]]


def section_heading(text: str) -> str:
    """The heading of a passage's citation header line (`[DOC §N] Title - Heading`), or ""."""
    header = passage_header(text)
    return header[1].split(" - ", 1)[1] if header and " - " in header[1] else ""


def reciprocal_rank_fusion(*rankings: list[str], k: int = RRF_K) -> list[str]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores, key=lambda item: (-scores[item], item))


@dataclass
class _TenantIndex:
    version: str
    bm25: Bm25Index
    by_id: dict[str, int]
    screened_out: bool  # any chunk held back by prompt-injection screening
    headings: Bm25Index  # each chunk's section heading alone, for heading-aware fusion


class HybridRetriever:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        embedder: Embedder,
        reranker: Reranker | None,
        injection_detector: str,
        candidates: int = 16,
        version_ttl_seconds: float = 30.0,
        heading_fusion: bool = False,
        heading_fusion_profiles: Collection[str] = (),
    ) -> None:
        self._sessionmaker = sessionmaker
        self.embedder = embedder
        self.reranker = reranker
        self.injection_detector = injection_detector  # the running detector's version
        self.candidates = candidates
        # A third ranking over section headings alone (ADR-0009, 2026-10-18 amendment): for every request, or
        # for the context profiles that opt in (`ContextProfile.heading_fusion`, from the catalog).
        self.heading_fusion = heading_fusion
        self.heading_fusion_profiles = frozenset(heading_fusion_profiles)
        self._version_ttl = version_ttl_seconds
        self._versions: dict[str, tuple[float, str]] = {}
        self._indexes: dict[str, _TenantIndex] = {}
        self._lock = asyncio.Lock()

    async def knowledge_version(self, tenant_id: str) -> str:
        cached = self._versions.get(tenant_id)
        if cached and time.monotonic() - cached[0] < self._version_ttl:
            return cached[1]
        async with self._sessionmaker() as session:
            rows = await session.execute(
                select(
                    KnowledgeDocumentRow.document_id,
                    KnowledgeDocumentRow.version,
                    KnowledgeDocumentRow.content_hash,
                )
                .where(KnowledgeDocumentRow.tenant_id == tenant_id)
                .order_by(KnowledgeDocumentRow.document_id)
            )
            documents = [list(row) for row in rows]
        key: dict[str, object] = {"docs": documents, "embedder": self.embedder.model_id}
        # Answers retrieved under another ranking are cached apart.
        if self.heading_fusion:
            key["ranking"] = "heading-fusion"
        elif self.heading_fusion_profiles:
            key["heading_fusion_profiles"] = sorted(self.heading_fusion_profiles)
        version = f"kb-{sha256_hex(key)[:16]}"
        self._versions[tenant_id] = (time.monotonic(), version)
        return version

    def invalidate(self, tenant_id: str) -> None:
        self._versions.pop(tenant_id, None)

    async def _index(self, tenant_id: str) -> _TenantIndex:
        version = await self.knowledge_version(tenant_id)
        current = self._indexes.get(tenant_id)
        if current is not None and current.version == version:
            return current
        async with self._lock:
            current = self._indexes.get(tenant_id)
            if current is not None and current.version == version:
                return current
            async with self._sessionmaker() as session:
                rows = await session.execute(
                    select(KnowledgeChunkRow, KnowledgeDocumentRow)
                    .join(KnowledgeDocumentRow, KnowledgeChunkRow.document_pk == KnowledgeDocumentRow.id)
                    .where(KnowledgeChunkRow.tenant_id == tenant_id)
                    .order_by(KnowledgeChunkRow.evidence_id)
                )
                chunks = [
                    IndexedChunk(
                        evidence_id=chunk.evidence_id,
                        text=chunk.text,
                        classification=document.classification,
                        application_ids=tuple(document.application_ids),
                        effective_from=document.effective_from,
                        expires_on=document.expires_on,
                        dlp_clean=not chunk.dlp_finding_types,
                        injection_clean=chunk.injection_indicators == []
                        and chunk.injection_detector == self.injection_detector,
                    )
                    for chunk, document in rows.all()
                ]
            index = _TenantIndex(
                version=version,
                bm25=Bm25Index(chunks),
                by_id={c.evidence_id: i for i, c in enumerate(chunks)},
                screened_out=not all(c.injection_clean for c in chunks),
                headings=Bm25Index([replace(c, text=section_heading(c.text)) for c in chunks]),
            )
            self._indexes[tenant_id] = index
            return index

    async def _vector_ranking(
        self, tenant_id: str, vector: Sequence[float], permitted: list[str], k: int
    ) -> list[str]:
        async with self._sessionmaker() as session:
            rows = await session.execute(
                select(KnowledgeChunkRow.evidence_id)
                .where(
                    KnowledgeChunkRow.tenant_id == tenant_id,
                    KnowledgeChunkRow.embedding_model == self.embedder.model_id,
                    KnowledgeChunkRow.evidence_id.in_(permitted),
                )
                .order_by(KnowledgeChunkRow.embedding.cosine_distance([float(v) for v in vector]))
                .limit(k)
            )
            return list(rows.scalars())

    async def _cosines(self, tenant_id: str, vector: Sequence[float], ids: list[str]) -> dict[str, float]:
        """Cosine similarity of each chunk's pinned embedding to the query (1 - pgvector cosine distance)."""
        distance = KnowledgeChunkRow.embedding.cosine_distance([float(v) for v in vector])
        async with self._sessionmaker() as session:
            rows = await session.execute(
                select(KnowledgeChunkRow.evidence_id, distance).where(
                    KnowledgeChunkRow.tenant_id == tenant_id,
                    KnowledgeChunkRow.embedding_model == self.embedder.model_id,
                    KnowledgeChunkRow.evidence_id.in_(ids),
                )
            )
            return {evidence_id: 1.0 - float(d) for evidence_id, d in rows.all()}

    async def retrieve(
        self,
        *,
        query: str,
        tenant_id: str,
        application_id: str,
        profile: ContextProfile | None,
        limit: int,
        min_cosine: float | None = None,
    ) -> list[CimEvidence]:
        """The `limit` best permitted passages in fused order. With `min_cosine` (sovereign-direct retrieval,
        ADR-0020), only passages whose cosine similarity to the query reaches it, in the same order: the
        limit is never filled with passages under the floor, and the span records how many were held back."""
        if not query.strip() or limit <= 0:
            return []
        index = await self._index(tenant_id)
        today = datetime.now(UTC).date()
        chunks = index.bm25.chunks
        eligible = {i for i, chunk in enumerate(chunks) if chunk.permitted(application_id, today)}
        if index.screened_out:
            held_back = sum(
                1 for c in chunks if not c.injection_clean and c.accessible(application_id, today)
            )
            if held_back:
                trace.get_current_span().set_attribute("aiva.retrieval.injection_held_back", held_back)
        if not eligible:
            return []
        lexical = [chunks[i].evidence_id for i in index.bm25.search(query, eligible, self.candidates)]
        vector = (await embed_async(self.embedder, [query], query=True))[0]
        semantic = await self._vector_ranking(
            tenant_id, vector, [chunks[i].evidence_id for i in eligible], self.candidates
        )
        rankings = [lexical, semantic]
        if self.heading_fusion or (profile is not None and profile.id in self.heading_fusion_profiles):
            rankings.append(
                [chunks[i].evidence_id for i in index.headings.search(query, eligible, self.candidates)]
            )
        fused = reciprocal_rank_fusion(*rankings)[: self.candidates]
        if self.reranker is not None and fused:
            texts = [chunks[index.by_id[e]].text for e in fused]
            scores = await asyncio.to_thread(self.reranker.score, query, texts)
            order = sorted(range(len(fused)), key=lambda i: (-scores[i], fused[i]))
            fused = [fused[i] for i in order if scores[i] > self.reranker.min_relevance]
        if min_cosine is not None and fused:
            cosines = await self._cosines(tenant_id, vector, fused)
            relevant = [e for e in fused if cosines.get(e, -1.0) >= min_cosine]
            trace.get_current_span().set_attribute("aiva.retrieval.below_floor", len(fused) - len(relevant))
            fused = relevant
        return [CimEvidence(id=e, text=chunks[index.by_id[e]].text) for e in fused[:limit]]
