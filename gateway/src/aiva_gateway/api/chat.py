from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from aiva_gateway.api import idempotency
from aiva_gateway.api.deps import RunCallerDep, ServicesDep, client_ip
from aiva_gateway.api.errors import ERROR_RESPONSES
from aiva_gateway.auth.principal import Caller
from aiva_gateway.auth.run_identity import resolve_run_identity
from aiva_gateway.contracts.chat import ChatRequest, ChatResponse
from aiva_gateway.contracts.common import ErrorEnvelope
from aiva_gateway.db.models import IdempotencyRecordRow
from aiva_gateway.govern.approvals import claim_approval, releasable, release_approval
from aiva_gateway.pipeline.context import RunContext
from aiva_gateway.pipeline.orchestrator import Orchestrator
from aiva_gateway.pipeline.recorder import persist_run
from aiva_gateway.pipeline.response import build_chat_response
from aiva_gateway.services import Services

router = APIRouter(tags=["chat"])

# How a route renders a finished run: its HTTP status and JSON body. The body is what an Idempotency-Key
# replay returns, so each route stores its own shape.
Render = Callable[[RunContext, ChatResponse], tuple[int, dict[str, Any]]]


@dataclass(frozen=True)
class ChatOutcome:
    status_code: int
    content: dict[str, Any]
    replayed: bool = False


def _native(ctx: RunContext, response: ChatResponse) -> tuple[int, dict[str, Any]]:
    return ctx.http_status, response.model_dump(mode="json")


async def execute_chat(
    body: ChatRequest,
    request: Request,
    caller: Caller,
    services: Services,
    idempotency_key: str | None,
    *,
    render: Render = _native,
    required_model: str | None = None,
) -> ChatOutcome:
    """Run one chat request through the pipeline, persist it and render it: the native API and the
    OpenAI-compatible API (ADR-0026) share every step, so neither can bypass policy, metering or records."""
    identity = resolve_run_identity(
        caller, body.test_profile, client_ip(request, services), services.settings
    )
    orchestrator = Orchestrator(services)
    ctx = orchestrator.new_context(body, identity, caller.tenant_id)
    ctx.required_model = required_model
    scope = f"{caller.tenant_id}:{identity.application_id}:{identity.principal_pseudonym or 'app'}"
    if body.approval_id:
        # A resumed request is a new execution, never a replay of the approval_required response.
        scope = f"{scope}:{body.approval_id}"
    if idempotency_key:
        replay = await idempotency.begin(services.redis, scope, idempotency_key, ctx.request_hash)
        if replay is not None:
            return ChatOutcome(replay["status_code"], replay["body"], replayed=True)
    try:
        if body.approval_id:
            async with services.sessionmaker() as session, session.begin():
                ctx.approval = await claim_approval(
                    session,
                    approval_id=body.approval_id,
                    tenant_id=caller.tenant_id,
                    identity=identity,
                    request_hash=ctx.request_hash,
                    run_id=ctx.run_id,
                )
        spans = await orchestrator.run(ctx)
        response = build_chat_response(ctx, spans, prices=services.prices)
        async with services.sessionmaker() as session, session.begin():
            await persist_run(session, services.catalog, ctx, spans)
            if ctx.approval is not None and releasable(ctx):
                await release_approval(session, ctx.approval, run_id=ctx.run_id)
            if idempotency_key:
                session.add(
                    IdempotencyRecordRow(
                        tenant_id=caller.tenant_id,
                        application_id=identity.application_id,
                        idempotency_key=idempotency_key,
                        request_hash=ctx.request_hash,
                        run_id=ctx.run_id,
                    )
                )
    except BaseException:
        if idempotency_key:
            await idempotency.abandon(services.redis, scope, idempotency_key)
        raise
    status_code, content = render(ctx, response)
    if idempotency_key:
        await idempotency.complete(
            services.redis, scope, idempotency_key, ctx.request_hash, status_code, content
        )
    return ChatOutcome(status_code, content)


@router.post(
    "/v1/ai/chat",
    response_model=ChatResponse,
    summary="Govern, optimize and execute a chat request (LLD §6, §8)",
    responses={
        **ERROR_RESPONSES,
        409: {"model": ErrorEnvelope, "description": "Idempotency-Key conflict."},
        503: {"model": ChatResponse, "description": "A dependency was unavailable; the run failed closed."},
    },
)
async def chat(
    body: ChatRequest,
    request: Request,
    caller: RunCallerDep,
    services: ServicesDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
) -> JSONResponse:
    outcome = await execute_chat(body, request, caller, services, idempotency_key)
    headers = {"Idempotent-Replayed": "true"} if outcome.replayed else None
    return JSONResponse(status_code=outcome.status_code, content=outcome.content, headers=headers)
