"""Evaluations (`/v1/evaluations/run`), delivered by ML & Data (P1/P2). Budgets live in api/budgets."""

from fastapi import APIRouter

from aiva_gateway.api.deps import CallerDep
from aiva_gateway.api.errors import ERROR_RESPONSES
from aiva_gateway.auth.rbac import Permission, require
from aiva_gateway.contracts.admin import EvaluationRun, EvaluationRunRequest
from aiva_gateway.core.errors import NotImplementedYetError

router = APIRouter(tags=["admin"])
_NOT_YET = {**ERROR_RESPONSES, 501: ERROR_RESPONSES[403]}


@router.post(
    "/v1/evaluations/run",
    response_model=EvaluationRun,
    status_code=202,
    responses=_NOT_YET,
    summary="Start a benchmark evaluation (LLD §16)",
)
async def run_evaluation(body: EvaluationRunRequest, caller: CallerDep) -> EvaluationRun:
    require(caller, Permission.EVALUATIONS_RUN)
    raise NotImplementedYetError("Delivered by the ML & Data workstream (P1/P2).")
