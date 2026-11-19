"""Confidential DOCX/PPTX copilot (LLD §14 Artifacts, §15 #10, §16 Document outputs, §17).

Artifacts are owner-only: other users, applications and tenants get 404, and the attempt is audited.
"""

from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response

from aiva_gateway.api.deps import CallerDep, RunCallerDep, ServicesDep, SessionDep, client_ip
from aiva_gateway.api.errors import ERROR_RESPONSES
from aiva_gateway.artifacts import sandbox
from aiva_gateway.artifacts.service import DocumentCopilot
from aiva_gateway.artifacts.store import (
    artifact_content,
    artifact_out,
    owned_artifacts,
    readable_artifact,
    save_artifact,
)
from aiva_gateway.auth.run_identity import resolve_run_identity
from aiva_gateway.contracts.artifacts import (
    DOCUMENT_MEDIA_TYPES,
    ArtifactCreateRequest,
    ArtifactCreateResponse,
    ArtifactList,
    ArtifactOut,
    ArtifactPreview,
    PreviewBlock,
    PreviewPage,
    SandboxReport,
)
from aiva_gateway.contracts.common import ErrorEnvelope, PageInfo
from aiva_gateway.core.errors import AivaError
from aiva_gateway.pipeline.recorder import persist_run
from aiva_gateway.pipeline.response import build_chat_response

router = APIRouter(tags=["artifacts"])
_RESPONSES: dict[int | str, dict[str, object]] = {
    **ERROR_RESPONSES,
    404: {"model": ErrorEnvelope, "description": "Not found, or not owned by the caller (audited)."},
}


class ArtifactIntegrityError(AivaError):
    status_code = 409
    code = "ARTIFACT_INTEGRITY_FAILED"


@router.post(
    "/v1/artifacts",
    response_model=ArtifactCreateResponse,
    summary="Draft an editable DOCX or PPTX through a governed run (sovereign direct when confidential)",
    responses={
        **_RESPONSES,
        503: {"model": ArtifactCreateResponse, "description": "A dependency was unavailable; failed closed."},
    },
)
async def create_artifact(
    body: ArtifactCreateRequest, request: Request, caller: RunCallerDep, services: ServicesDep
) -> JSONResponse:
    identity = resolve_run_identity(
        caller, body.test_profile, client_ip(request, services), services.settings
    )
    draft = await DocumentCopilot(services).draft(body, identity, caller.tenant_id)
    ctx = draft.ctx
    run = build_chat_response(ctx, draft.spans, prices=services.prices)
    async with services.sessionmaker() as session, session.begin():
        await persist_run(session, services.catalog, ctx, draft.spans)
        row = await save_artifact(session, draft.artifact, ctx, identity) if draft.artifact else None
        await session.flush()
        artifact = await artifact_out(session, row, ctx.data_designation) if row is not None else None
    content = ArtifactCreateResponse(run=run, artifact=artifact).model_dump(mode="json")
    return JSONResponse(status_code=ctx.http_status, content=content)


@router.get(
    "/v1/artifacts",
    response_model=ArtifactList,
    summary="Artifacts owned by the caller, newest first",
    responses=_RESPONSES,
)
async def list_artifacts(
    caller: CallerDep,
    services: ServicesDep,
    session: SessionDep,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> ArtifactList:
    rows = await owned_artifacts(session, caller, cursor=cursor, limit=limit)
    page = rows[:limit]
    items = [await artifact_out(session, row, services.settings.data_designation) for row in page]
    next_cursor = page[-1].id if len(rows) > limit else None
    return ArtifactList(items=items, page=PageInfo(next_cursor=next_cursor, limit=limit))


@router.get(
    "/v1/artifacts/{artifact_id}",
    response_model=ArtifactOut,
    summary="Artifact metadata and document-output checks",
    responses=_RESPONSES,
)
async def get_artifact(
    artifact_id: str, caller: CallerDep, services: ServicesDep, session: SessionDep
) -> ArtifactOut:
    row = await readable_artifact(services, session, caller, artifact_id, purpose="metadata")
    return await artifact_out(session, row, services.settings.data_designation)


@router.get(
    "/v1/artifacts/{artifact_id}/content",
    response_class=Response,
    summary="Download the editable document",
    responses={
        **_RESPONSES,
        200: {
            "description": "The generated document.",
            "content": {
                media_type: {"schema": {"type": "string", "format": "binary"}}
                for media_type in DOCUMENT_MEDIA_TYPES.values()
            },
        },
    },
)
async def get_artifact_content(
    artifact_id: str, caller: CallerDep, services: ServicesDep, session: SessionDep
) -> Response:
    row = await readable_artifact(services, session, caller, artifact_id, purpose="content")
    data = await artifact_content(session, row)
    if sandbox.sha256(data) != row.sha256:
        raise ArtifactIntegrityError("The stored document no longer matches its recorded hash.")
    return Response(
        content=data,
        media_type=row.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{row.file_name}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.get(
    "/v1/artifacts/{artifact_id}/preview",
    response_model=ArtifactPreview,
    summary="Structured preview rendered from the stored file in a network-less sandbox",
    responses=_RESPONSES,
)
async def get_artifact_preview(
    artifact_id: str, caller: CallerDep, services: ServicesDep, session: SessionDep
) -> ArtifactPreview:
    row = await readable_artifact(services, session, caller, artifact_id, purpose="preview")
    data = await artifact_content(session, row)
    rendered = await sandbox.render(data, row.document_type, expected_sha256=row.sha256)
    pages = [
        PreviewPage(
            index=page["index"],
            kind=page["kind"],
            title=page.get("title"),
            blocks=[PreviewBlock.model_validate(block) for block in page["blocks"]],
        )
        for page in rendered.pages
    ]
    return ArtifactPreview(
        artifact_id=row.id,
        document_type=row.document_type,
        pages=pages,
        sandbox=SandboxReport(
            renderer=sandbox.RENDERER_VERSION,
            isolated_interpreter=True,
            timeout_ms=int(sandbox.TIMEOUT_SECONDS * 1000),
            duration_ms=rendered.duration_ms,
            verified_sha256=rendered.verified_sha256,
        ),
    )
