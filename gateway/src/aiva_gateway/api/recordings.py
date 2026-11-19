"""Recorded provider exchanges (`GET /v1/admin/recordings`, ADR-0009).

A gateway in record mode saves every provider exchange to `recordings_dir`. This route hands a platform
administrator those recordings as one JSON bundle, each entry a fixture file's name and content, so a record
run on a Space can be turned into committed replay fixtures (`tests/e2e/record_scenarios.py`). It answers
only in record mode (404 otherwise, after authorization), and every export is audited.
"""

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import SecretStr

from aiva_gateway.api.deps import CallerDep, ServicesDep, SessionDep
from aiva_gateway.api.errors import ERROR_RESPONSES
from aiva_gateway.auth.principal import CallerReferenceError
from aiva_gateway.auth.rbac import Permission, require
from aiva_gateway.config import Settings
from aiva_gateway.contracts.recordings import (
    RecordedExchange,
    RecordedRequest,
    RecordedResponse,
    RecordingBundle,
    RecordingContent,
)
from aiva_gateway.core.errors import NotFoundError, PermissionDeniedError
from aiva_gateway.db.models import AuditLogRow
from aiva_gateway.inference.recording import read_recordings

router = APIRouter(tags=["admin"])


def configured_secrets(settings: Settings) -> list[str]:
    """Every secret the gateway holds (provider keys, signing keys, connection URLs): none may be exported."""
    values = (getattr(settings, name) for name in type(settings).model_fields)
    return [value.get_secret_value() for value in values if isinstance(value, SecretStr)]


@router.get(
    "/v1/admin/recordings",
    response_model=RecordingBundle,
    responses=ERROR_RESPONSES,
    summary="Export the recorded provider exchanges of a record-mode gateway (platform admins; audited)",
)
async def export_recordings(caller: CallerDep, services: ServicesDep, session: SessionDep) -> RecordingBundle:
    require(caller, Permission.RECORDINGS_EXPORT)
    settings = services.settings
    if settings.inference_mode != "record":
        raise NotFoundError(
            "Recordings are exported only by a gateway in record mode.",
            remediation="Run the gateway with AIVA_INFERENCE_MODE=record to record provider exchanges.",
        )
    try:
        actor = caller.actor_ref
    except CallerReferenceError as exc:
        raise PermissionDeniedError("Exports need a pseudonymous caller.") from exc
    stored, withheld = await asyncio.to_thread(
        read_recordings, settings.recordings_dir, secrets=configured_secrets(settings)
    )
    labels: dict[str, int] = {}
    for item in stored:
        labels[item.label] = labels.get(item.label, 0) + 1
    session.add(
        AuditLogRow(
            tenant_id=caller.tenant_id,
            actor_kind="user",
            actor_id=actor,
            action="recordings.export",
            target_type="recordings",
            target_id=f"{len(stored)} exchanges",
            details={"exported": len(stored), "withheld": withheld, "labels": labels},
        )
    )
    await session.commit()
    return RecordingBundle(
        inference_mode="record",
        data_designation=settings.data_designation,
        exported_at=datetime.now(UTC),
        withheld=withheld,
        recordings=[
            RecordedExchange(
                name=item.name,
                content=RecordingContent(
                    request=RecordedRequest(
                        method=item.recording.method,
                        url=item.recording.url,
                        body=item.recording.request_body,
                    ),
                    response=RecordedResponse(
                        status_code=item.recording.status_code,
                        content_type=item.recording.content_type,
                        body=item.recording.response_body,
                    ),
                ),
            )
            for item in stored
        ],
    )
