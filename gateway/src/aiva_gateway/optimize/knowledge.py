"""Knowledge-base ingestion for retrieval (BM25 + pgvector + reranker).

Input is the ML & Data export (`data/synthetic/kb/*.jsonl`): JSON Lines, one row per document section.

    {"doc_id": "KB-ROAM-001", "version": "2026-10-01", "title": "...", "section": "Europe",
     "text": "...", "classification": "internal", "applications": ["gct-support-chatbot"],
     "effective_from": "2026-10-01", "expires_on": null}

`applications` empty = every application of the tenant; `effective_from` / `expires_on` are optional.
Rows of one document must agree on its metadata. Sections are split into chunks of about `CHUNK_TOKENS`
tokens on paragraph boundaries. Every chunk is DLP-scanned at ingestion, prompt-injection detection
included (LLD §7, "scan retrieved text"); a chunk with findings or injection indicators is stored but never
retrieved, and neither is a chunk not screened by the running injection detector version (one stored before
screening, or screened by another version). Evidence ids are `<doc_id>#<ordinal>`.
Re-ingesting a document replaces its chunks.

Each chunk starts with its citation header, `[<doc_id> §<n>] <title> - <heading>` (`citations.py`), the
format the CIM is trained on. A section named `§<n> <heading>` keeps its number; an unnumbered section is
numbered by its position in the document. The rendering and the scanner version are part of each
document's content hash, so a change to either changes the knowledge-base version and with it every
response-cache key and the retrieval index.
"""

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from aiva_gateway.core.canonical import estimate_tokens, sha256_hex
from aiva_gateway.core.enums import Classification
from aiva_gateway.db.models import KnowledgeChunkRow, KnowledgeDocumentRow
from aiva_gateway.optimize.citations import citation_header, numbered_section
from aiva_gateway.optimize.embeddings import Embedder, embed_async
from aiva_gateway.pipeline.interfaces import DlpScanner

CHUNK_TOKENS = 350
# Bump when `evidence_text` changes: it is hashed with every document (knowledge-base version, cache keys).
EVIDENCE_RENDERING = "citation-header-v1"


class KbSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section: str
    text: str


class KbRow(BaseModel):
    """One exported row: a section of a document."""

    model_config = ConfigDict(extra="forbid")
    doc_id: str = Field(max_length=128)
    version: str
    title: str
    section: str
    text: str
    classification: Classification
    applications: list[str] = Field(default_factory=list)
    effective_from: date | None = None
    expires_on: date | None = None


class KbDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str = Field(max_length=128)
    title: str
    classification: Classification
    application_ids: list[str] = Field(default_factory=list)
    version: str
    effective_from: date | None = None
    expires_on: date | None = None
    sections: list[KbSection]


@dataclass(frozen=True)
class IngestReport:
    documents: int
    chunks: int
    dlp_flagged_chunks: int
    injection_flagged_chunks: int


def group_rows(rows: list[KbRow]) -> list[KbDocument]:
    """Group section rows into documents, in first-seen order."""
    documents: dict[str, KbDocument] = {}
    for row in rows:
        document = documents.get(row.doc_id)
        metadata = (
            row.version,
            row.title,
            row.classification,
            sorted(row.applications),
            row.effective_from,
            row.expires_on,
        )
        if document is None:
            documents[row.doc_id] = KbDocument(
                document_id=row.doc_id,
                title=row.title,
                classification=row.classification,
                application_ids=row.applications,
                version=row.version,
                effective_from=row.effective_from,
                expires_on=row.expires_on,
                sections=[KbSection(section=row.section, text=row.text)],
            )
            continue
        existing = (
            document.version,
            document.title,
            document.classification,
            sorted(document.application_ids),
            document.effective_from,
            document.expires_on,
        )
        if metadata != existing:
            raise ValueError(f"rows of {row.doc_id} disagree on document metadata")
        document.sections.append(KbSection(section=row.section, text=row.text))
    return list(documents.values())


def load_jsonl(path: Path) -> list[KbDocument]:
    rows = [
        KbRow.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return group_rows(rows)


def chunk_section(text: str, limit: int = CHUNK_TOKENS) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    for paragraph in (p.strip() for p in text.split("\n\n")):
        if not paragraph:
            continue
        if current and estimate_tokens("\n\n".join([*current, paragraph])) > limit:
            chunks.append("\n\n".join(current))
            current = []
        current.append(paragraph)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def evidence_text(document_id: str, number: int, title: str, heading: str, text: str) -> str:
    """A passage as the CIM is trained to read it: its citation header line, then the section text."""
    return f"{citation_header(document_id, number, title, heading)}\n{text}"


def section_citations(document: KbDocument) -> list[tuple[int, str]]:
    """Each section's (number, heading); two sections of one document never share a citation."""
    numbered = [numbered_section(s.section, position) for position, s in enumerate(document.sections, 1)]
    numbers = [n for n, _ in numbered]
    duplicated = sorted({n for n in numbers if numbers.count(n) > 1})
    if duplicated:
        raise ValueError(f"sections of {document.document_id} share citation numbers {duplicated}")
    return numbered


async def ingest_documents(
    session: AsyncSession,
    *,
    tenant_id: str,
    documents: list[KbDocument],
    embedder: Embedder,
    dlp: DlpScanner,
) -> IngestReport:
    chunk_count = flagged = injected = 0
    for document in documents:
        existing = await session.scalar(
            select(KnowledgeDocumentRow).where(
                KnowledgeDocumentRow.tenant_id == tenant_id,
                KnowledgeDocumentRow.document_id == document.document_id,
            )
        )
        if existing is not None:
            await session.execute(
                delete(KnowledgeChunkRow).where(KnowledgeChunkRow.document_pk == existing.id)
            )
            await session.delete(existing)
            await session.flush()
        row = KnowledgeDocumentRow(
            tenant_id=tenant_id,
            document_id=document.document_id,
            title=document.title,
            classification=document.classification.value,
            application_ids=document.application_ids,
            version=document.version,
            content_hash=sha256_hex(
                {
                    "document": document.model_dump(mode="json"),
                    "rendering": EVIDENCE_RENDERING,
                    "screening": dlp.version,
                }
            ),
            effective_from=document.effective_from,
            expires_on=document.expires_on,
        )
        session.add(row)
        await session.flush()
        pieces = [
            (section.section, evidence_text(document.document_id, number, document.title, heading, text))
            for section, (number, heading) in zip(document.sections, section_citations(document), strict=True)
            for text in chunk_section(section.text)
        ]
        texts = [text for _, text in pieces]
        vectors = list(await embed_async(embedder, texts)) if texts else []
        for ordinal, ((section_name, _), text, vector) in enumerate(zip(pieces, texts, vectors, strict=True)):
            scanned = dlp.scan(text, detect_injection=True)
            findings, indicators = scanned.finding_types, list(scanned.injection_indicators)
            flagged += bool(findings)
            injected += bool(indicators)
            session.add(
                KnowledgeChunkRow(
                    document_pk=row.id,
                    tenant_id=tenant_id,
                    evidence_id=f"{document.document_id}#{ordinal}",
                    ordinal=ordinal,
                    section=section_name[:300],
                    text=text,
                    token_estimate=estimate_tokens(text),
                    dlp_finding_types=findings,
                    injection_indicators=indicators,
                    injection_detector=scanned.injection_detector or None,
                    embedding_model=embedder.model_id,
                    embedding=[float(v) for v in vector],
                )
            )
        chunk_count += len(texts)
    await session.flush()
    return IngestReport(
        documents=len(documents),
        chunks=chunk_count,
        dlp_flagged_chunks=flagged,
        injection_flagged_chunks=injected,
    )
