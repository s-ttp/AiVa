"""AiVa Govern policy APIs: dry-run evaluation and the policy lifecycle (LLD §8, §22.6; ADR-0012)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from aiva_gateway.api.deps import CallerDep, RunCallerDep, ServicesDep, SessionDep, client_ip
from aiva_gateway.api.errors import ERROR_RESPONSES
from aiva_gateway.auth.principal import Caller
from aiva_gateway.auth.rbac import Permission, require
from aiva_gateway.auth.run_identity import resolve_run_identity
from aiva_gateway.contracts.chat import ChatRequest
from aiva_gateway.contracts.policy import (
    ModelSwitchRequest,
    ModelSwitchResponse,
    PolicyDraftRequest,
    PolicyEvaluateRequest,
    PolicyEvaluateResponse,
    PolicyPublishRequest,
    PolicyPublishResponse,
    PolicyTestRequest,
    PolicyTestResponse,
    PolicyVersionDetail,
    PolicyVersionSummary,
)
from aiva_gateway.db.models import PolicyVersionRow
from aiva_gateway.govern import lifecycle, model_switch
from aiva_gateway.govern.policy_input import dlp_facts
from aiva_gateway.pipeline.orchestrator import Orchestrator

router = APIRouter(tags=["policy"])


@router.post(
    "/v1/policy/evaluate",
    response_model=PolicyEvaluateResponse,
    responses=ERROR_RESPONSES,
    summary="Dry-run the request-stage AI-PEL decision without executing a model",
)
async def evaluate(
    body: PolicyEvaluateRequest, request: Request, caller: RunCallerDep, services: ServicesDep
) -> PolicyEvaluateResponse:
    identity = resolve_run_identity(
        caller, body.test_profile, client_ip(request, services), services.settings
    )
    chat = ChatRequest(
        messages=body.messages,
        evidence=body.evidence,
        data_classification=body.data_classification,
        task=body.task,
        scenario_id=body.scenario_id,
        test_profile=body.test_profile,
    )
    chat.routing.tags = body.tags
    orchestrator = Orchestrator(services)
    ctx = orchestrator.new_context(chat, identity, caller.tenant_id)
    decision = await orchestrator.dry_run_policy(ctx)
    assert ctx.classification is not None and ctx.dlp is not None
    return PolicyEvaluateResponse(
        decision=decision,
        classification=ctx.classification,
        dlp=dlp_facts(ctx.dlp, ctx.evidence_dlp),
    )


def _actor(caller: Caller) -> str:
    return caller.actor_ref


@router.get(
    "/v1/admin/policies",
    response_model=list[PolicyVersionSummary],
    responses=ERROR_RESPONSES,
    summary="List policy versions",
)
async def list_policies(caller: CallerDep, session: SessionDep) -> list[PolicyVersionSummary]:
    require(caller, Permission.POLICIES_READ)
    rows = (
        await session.execute(
            select(PolicyVersionRow)
            .where(PolicyVersionRow.tenant_id == caller.tenant_id)
            .order_by(PolicyVersionRow.version.desc())
        )
    ).scalars()
    return [lifecycle.summary(r) for r in rows]


@router.get(
    "/v1/admin/policies/{version}",
    response_model=PolicyVersionDetail,
    responses={**ERROR_RESPONSES, 404: ERROR_RESPONSES[403]},
    summary="Policy version with its documents and changes from the published version",
)
async def get_policy_version(version: int, caller: CallerDep, session: SessionDep) -> PolicyVersionDetail:
    require(caller, Permission.POLICIES_READ)
    row = await lifecycle.get_version(session, caller.tenant_id, version)
    return await lifecycle.detail(session, row, viewer_actor=_actor(caller))


@router.post(
    "/v1/admin/policies",
    response_model=PolicyVersionDetail,
    status_code=201,
    responses=ERROR_RESPONSES,
    summary="Create a draft policy version (validated; not enforced until published)",
)
async def create_policy_draft(
    body: PolicyDraftRequest, caller: CallerDep, session: SessionDep, services: ServicesDep
) -> PolicyVersionDetail:
    require(caller, Permission.POLICIES_WRITE)
    row = await lifecycle.create_draft(
        services, session, body.policies, actor=_actor(caller), notes=body.notes
    )
    result = await lifecycle.detail(session, row, viewer_actor=_actor(caller))
    await session.commit()
    return result


@router.post(
    "/v1/admin/policies/test",
    response_model=PolicyTestResponse,
    responses={**ERROR_RESPONSES, 404: ERROR_RESPONSES[403], 503: ERROR_RESPONSES[403]},
    summary="Simulate a policy version (draft, published or archived) against test cases",
)
async def test_policies(
    body: PolicyTestRequest, caller: CallerDep, session: SessionDep, services: ServicesDep
) -> PolicyTestResponse:
    require(caller, Permission.POLICIES_WRITE)
    result = await lifecycle.simulate(
        services, session, body, tenant_id=caller.tenant_id, actor=_actor(caller)
    )
    await session.commit()
    return result


@router.post(
    "/v1/admin/policies/publish",
    response_model=PolicyPublishResponse,
    responses={
        **ERROR_RESPONSES,
        404: ERROR_RESPONSES[403],
        409: ERROR_RESPONSES[403],
        503: ERROR_RESPONSES[403],
    },
    summary="Publish a draft, or roll back by re-publishing an archived version (same bundle hash)",
)
async def publish_policy(
    body: PolicyPublishRequest, caller: CallerDep, session: SessionDep, services: ServicesDep
) -> PolicyPublishResponse:
    require(caller, Permission.POLICIES_PUBLISH)
    result = await lifecycle.publish(
        services,
        session,
        body.version,
        actor=_actor(caller),
        approval_note=body.approval_note,
        tenant_id=caller.tenant_id,
    )
    await session.commit()
    return result


async def get_policy_writer(caller: CallerDep) -> Caller:
    """An administrator, authorized before the request body is validated (401, then 403, then 422)."""
    require(caller, Permission.POLICIES_WRITE)
    return caller


PolicyWriterDep = Annotated[Caller, Depends(get_policy_writer)]


@router.post(
    "/v1/admin/policies/model-switch",
    response_model=ModelSwitchResponse,
    status_code=201,
    responses={
        **ERROR_RESPONSES,
        404: ERROR_RESPONSES[403],
        409: ERROR_RESPONSES[403],
        503: ERROR_RESPONSES[403],
    },
    summary="Turn a granted model off or back on for one application, as a policy draft (ADR-0027)",
)
async def switch_model(
    body: ModelSwitchRequest, caller: PolicyWriterDep, session: SessionDep, services: ServicesDep
) -> ModelSwitchResponse:
    result = await model_switch.switch_model(services, session, caller, body)
    await session.commit()
    return result
