"""A presenter's answer-quality comparison of a live run (ADR-0023): `POST /v1/runs/{run_id}/quality-check`.

`demo:compare` is checked before the body (401, then 403, then 422, ADR-0007). The run must be the caller's
own and served live; the resent request and answer must be the ones the run served. See
`assurance.quality_check`.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from aiva_gateway.api.deps import CallerDep, ServicesDep, client_ip
from aiva_gateway.api.errors import ERROR_RESPONSES
from aiva_gateway.assurance.quality_check import QualityCheck
from aiva_gateway.auth.principal import Caller
from aiva_gateway.auth.rbac import Permission, require
from aiva_gateway.contracts.quality import QualityCheckRequest, QualityCheckResponse

router = APIRouter(tags=["quality"])


async def get_compare_caller(caller: CallerDep) -> Caller:
    """The presenter, authorized before the request body is validated."""
    require(caller, Permission.DEMO_COMPARE)
    return caller


CompareCallerDep = Annotated[Caller, Depends(get_compare_caller)]


@router.post(
    "/v1/runs/{run_id}/quality-check",
    response_model=QualityCheckResponse,
    responses=ERROR_RESPONSES,
    summary="Compare a live answer with the frontier reference, graded by the pinned grader (ADR-0023)",
)
async def quality_check(
    run_id: str,
    body: QualityCheckRequest,
    request: Request,
    caller: CompareCallerDep,
    services: ServicesDep,
) -> QualityCheckResponse:
    return await QualityCheck(services).run(run_id, body, caller, client_ip(request, services))
