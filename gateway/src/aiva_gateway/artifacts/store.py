"""Artifact storage: tenant- and owner-scoped, content in Postgres, every refused access audited (LLD §17).

An artifact belongs to the principal that drafted it: the tester's pseudonym, or the application for an
application credential. Nobody else can read its metadata, content or preview. That includes other users
of the tenant and every caller from another tenant. A refused read returns the same 404 as a missing
artifact, so existence is never disclosed, and it is written to the owning tenant's audit log.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aiva_gateway.artifacts.builder import GENERATOR_VERSION
from aiva_gateway.artifacts.service import GeneratedArtifact
from aiva_gateway.auth.principal import Caller, RunIdentity
from aiva_gateway.contracts.artifacts import (
    ArtifactCheck,
    ArtifactOut,
    CitationResult,
    DocumentOutline,
    FigureResult,
)
from aiva_gateway.core.enums import Classification, ExecutionPath, Provenance
from aiva_gateway.core.errors import NotFoundError
from aiva_gateway.db.models import ArtifactBlobRow, ArtifactRow, AuditLogRow, ExecutionRunRow
from aiva_gateway.pipeline.context import RunContext
from aiva_gateway.services import Services

ACCESS_POLICY = "owner-only"
_NOT_FOUND = "Artifact not found."


def owner_of_identity(identity: RunIdentity) -> tuple[str, str]:
    if identity.principal_kind == "user" and identity.principal_pseudonym:
        return "user", identity.principal_pseudonym
    return "application", identity.application_id


def owner_of_caller(caller: Caller) -> tuple[str, str | None]:
    if caller.kind == "user":
        return "user", caller.pseudonym
    return "application", caller.application_id


async def save_artifact(
    session: AsyncSession, generated: GeneratedArtifact, ctx: RunContext, identity: RunIdentity
) -> ArtifactRow:
    owner_kind, owner_id = owner_of_identity(identity)
    report = generated.report
    figures_status = next((c.status for c in report.checks if c.name == "figures"), "not-run")
    structure_status = next((c.status for c in report.checks if c.name == "structure"), "not-run")
    row = ArtifactRow(
        id=generated.artifact_id,
        run_id=ctx.run_id,
        object_ref=f"postgres:artifact_blob/{generated.artifact_id}",
        document_type=generated.document_type,
        schema_validation=structure_status,
        figure_validation=figures_status,
        access_policy=ACCESS_POLICY,
        tenant_id=ctx.tenant_id,
        application_id=identity.application_id,
        owner_kind=owner_kind,
        owner_id=owner_id,
        file_name=generated.file_name,
        media_type=generated.media_type,
        size_bytes=len(generated.content),
        sha256=generated.sha256,
        status=generated.status,
        classification=ctx.classification.effective.value if ctx.classification else "internal",
        provenance=generated.provenance.value,
        generator_version=GENERATOR_VERSION,
        checks={
            "checks": [c.model_dump(mode="json") for c in report.checks],
            "figures": [f.model_dump(mode="json") for f in report.figures],
            "citations": [c.model_dump(mode="json") for c in report.citations],
            "outline": report.outline.model_dump(mode="json"),
        },
    )
    session.add(row)
    await session.flush()  # the blob references the artifact row
    session.add(ArtifactBlobRow(artifact_id=generated.artifact_id, content=generated.content))
    session.add(
        AuditLogRow(
            tenant_id=ctx.tenant_id,
            actor_kind=owner_kind,
            actor_id=owner_id,
            action="artifact.create",
            target_type="artifact",
            target_id=generated.artifact_id,
            details={"run_id": ctx.run_id, "status": generated.status, "sha256": generated.sha256},
        )
    )
    return row


async def artifact_out(session: AsyncSession, row: ArtifactRow, data_designation: str) -> ArtifactOut:
    run = await session.get(ExecutionRunRow, row.run_id)
    assert run is not None
    stored = row.checks
    return ArtifactOut(
        artifact_id=row.id,
        run_id=row.run_id,
        trace_id=run.trace_id,
        tenant_id=row.tenant_id,
        application_id=row.application_id,
        document_type=row.document_type,
        file_name=row.file_name,
        media_type=row.media_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        status=row.status,
        classification=Classification(row.classification),
        execution_path=ExecutionPath(run.execution_path),
        selected_model=run.selected_model,
        provenance=Provenance(row.provenance),
        generator_version=row.generator_version,
        checks=[ArtifactCheck.model_validate(c) for c in stored.get("checks", [])],
        figures=[FigureResult.model_validate(f) for f in stored.get("figures", [])],
        citations=[CitationResult.model_validate(c) for c in stored.get("citations", [])],
        outline=DocumentOutline.model_validate(stored["outline"]),
        access_policy=ACCESS_POLICY,
        data_designation="actual" if data_designation == "actual" else "synthetic",
        created_at=row.created_at,
    )


async def readable_artifact(
    services: Services, session: AsyncSession, caller: Caller, artifact_id: str, *, purpose: str
) -> ArtifactRow:
    """The artifact if the caller owns it; otherwise 404, and the refusal is audited for its owner."""
    row = await session.get(ArtifactRow, artifact_id)
    if row is None:
        raise NotFoundError(_NOT_FOUND)
    kind, owner = owner_of_caller(caller)
    if row.tenant_id == caller.tenant_id and (kind, owner) == (row.owner_kind, row.owner_id):
        return row
    reason = "cross_tenant" if row.tenant_id != caller.tenant_id else "not_owner"
    async with services.sessionmaker() as audit, audit.begin():
        audit.add(
            AuditLogRow(
                tenant_id=row.tenant_id,
                actor_kind=caller.kind,
                actor_id=(caller.pseudonym or caller.application_id or "unknown")[:64],
                action="artifact.access_denied",
                target_type="artifact",
                target_id=row.id,
                details={"reason": reason, "purpose": purpose, "caller_tenant_id": caller.tenant_id},
            )
        )
    raise NotFoundError(_NOT_FOUND)


async def artifact_content(session: AsyncSession, row: ArtifactRow) -> bytes:
    blob = await session.get(ArtifactBlobRow, row.id)
    if blob is None:
        raise NotFoundError(_NOT_FOUND)
    return blob.content


async def owned_artifacts(
    session: AsyncSession, caller: Caller, *, cursor: str | None, limit: int
) -> list[ArtifactRow]:
    kind, owner = owner_of_caller(caller)
    if owner is None:
        return []
    query = select(ArtifactRow).where(
        ArtifactRow.tenant_id == caller.tenant_id,
        ArtifactRow.owner_kind == kind,
        ArtifactRow.owner_id == owner,
    )
    if cursor is not None:
        query = query.where(ArtifactRow.id < cursor)
    rows = await session.scalars(query.order_by(ArtifactRow.id.desc()).limit(limit + 1))
    return list(rows)
