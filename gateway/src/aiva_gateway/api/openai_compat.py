"""OpenAI-compatible API (ADR-0026): `POST /v1/chat/completions`, `GET /v1/models` and `GET /v1/models/{id}`.

A facade, not a second pipeline: a request is mapped onto a `ChatRequest` and executed by `execute_chat`, the
native route's own executor, so policy, DLP, the quota reservation, RLCD, the CIM, assurance, metering,
tracing and the run record all apply unchanged. What it adds is the translation:
- `model`: `aiva-auto` (or omitted) lets RLCD choose; a registry id is required (`RunContext.required_model`),
  served only if policy and the execution path leave it eligible, never substituted.
- Governance fields come from `X-AiVa-*` headers or `aiva_*` metadata keys; the application's own floor still
  applies, and the most restrictive classification signal wins.
- Parameters AiVa cannot honour are refused with a 400; sampling parameters it sets itself are accepted, not
  applied, and listed in `aiva.ignored_parameters`.
- `stream: true` is buffered: the run completes, so output DLP and assurance see the whole answer, and only
  then is it sent as `chat.completion.chunk` events.
"""

import json
import re
import uuid
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import ValidationError

from aiva_gateway.api.chat import execute_chat
from aiva_gateway.api.deps import RunCallerDep, ServicesDep, client_ip
from aiva_gateway.api.errors import openai_error, openai_error_type
from aiva_gateway.auth.principal import Caller
from aiva_gateway.auth.run_identity import resolve_run_identity
from aiva_gateway.contracts.chat import ChatRequest, ChatResponse, RoutingOptions
from aiva_gateway.contracts.messages import ChatMessage, TestProfile
from aiva_gateway.contracts.openai_compat import (
    AUTO_MODEL,
    AivaCompletionInfo,
    AivaErrorInfo,
    ChatCompletion,
    ChatCompletionRequest,
    CompletionChoice,
    CompletionMessage,
    CompletionTokensDetails,
    CompletionUsage,
    OpenAIError,
    OpenAIErrorEnvelope,
    OpenAIModel,
    OpenAIModelList,
    PromptTokensDetails,
)
from aiva_gateway.core.enums import Classification, Outcome, PolicyAction, RunStatus
from aiva_gateway.core.errors import PermissionDeniedError
from aiva_gateway.pipeline.context import RunContext
from aiva_gateway.pipeline.orchestrator import Orchestrator
from aiva_gateway.services import Services

router = APIRouter(tags=["openai-compatible"])

# Governance fields: the `X-AiVa-*` header, else the `aiva_*` metadata key (SDKs that cannot set headers).
GOVERNANCE = {
    "data_classification": "X-AiVa-Data-Classification",
    "quality_tier": "X-AiVa-Quality-Tier",
    "routing_tags": "X-AiVa-Routing-Tags",
    "task": "X-AiVa-Task",
    "scenario": "X-AiVa-Scenario",
    "application": "X-AiVa-Application",
    # The Test Console's simulated ingress (ADR-0007): testers only, where the environment allows it.
    "simulated_source_ip": "X-AiVa-Simulated-Source-IP",
}
QUALITY_TIERS = ("standard", "high", "critical")
DEFAULT_MAX_OUTPUT_TOKENS = 1024
IDEMPOTENCY_PREFIX = "oai:"  # its own key space: an OpenAI-shaped replay never answers a native request
# Accepted, not applied: AiVa sets sampling per model and grounding (ADR-0009). `user` is never stored.
IGNORED = (
    "temperature",
    "top_p",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "reasoning_effort",
    "service_tier",
    "store",
    "user",
)
_STREAM_PIECE = re.compile(r"\S+\s*|\s+")
STREAM_PIECE_CHARS = 120
# Statuses Space 2's front door intercepts (gateway-proxy.sh: proxy_intercept_errors, error_page 502 504):
# a run the gateway finished never answers with them, or its body would never reach the caller.
GATEWAY_ERRORS = frozenset({502, 504})
MODEL_NOT_FOUND = "The model `{model}` does not exist or you do not have access to it."


class Refused(Exception):
    """A request the facade cannot run: answered with an OpenAI-shaped error, no run started."""

    def __init__(self, status_code: int, code: str, message: str, param: str | None = None) -> None:
        super().__init__(message)
        self.status_code, self.code, self.message, self.param = status_code, code, message, param

    def response(self) -> JSONResponse:
        return openai_error(self.status_code, self.code, self.message, param=self.param)


def refusals(body: ChatCompletionRequest) -> None:
    """Refuse what AiVa cannot honour, naming the parameter. Tools go through ADR-0012, not this API."""
    unsupported = {
        "tools": bool(body.tools),
        "tool_choice": body.tool_choice not in (None, "none"),
        "functions": bool(body.functions),
        "function_call": body.function_call not in (None, "none"),
        "n": body.n not in (None, 1),
        "logprobs": bool(body.logprobs),
        "top_logprobs": body.top_logprobs is not None,
        "response_format": body.response_format is not None and body.response_format.get("type") != "text",
        "stop": bool(body.stop),
        "logit_bias": bool(body.logit_bias),
        "modalities": body.modalities is not None and body.modalities != ["text"],
        "audio": body.audio is not None,
        "prediction": body.prediction is not None,
        "web_search_options": body.web_search_options is not None,
    }
    for param, refused in unsupported.items():
        if refused:
            raise Refused(400, "unsupported_parameter", f"Unsupported parameter: '{param}'.", param)


def ignored_parameters(body: ChatCompletionRequest) -> list[str]:
    return [p for p in IGNORED if getattr(body, p) is not None]


def to_messages(body: ChatCompletionRequest) -> list[ChatMessage]:
    """Text-only messages. `developer` is the newer name of `system`; tool turns are refused (no tools)."""
    messages: list[ChatMessage] = []
    for i, m in enumerate(body.messages):
        param = f"messages.{i}"
        if m.role in ("tool", "function") or m.tool_calls or m.function_call:
            raise Refused(
                400, "unsupported_parameter", "Tool and function messages are not supported.", param
            )
        if m.content is None:
            raise Refused(400, "invalid_value", "A message needs text content.", f"{param}.content")
        if isinstance(m.content, str):
            text = m.content
        else:
            if any(part.type != "text" or part.text is None for part in m.content):
                raise Refused(
                    400,
                    "unsupported_content",
                    "Only text content is supported; images, audio and files are not.",
                    f"{param}.content",
                )
            text = "\n".join(part.text or "" for part in m.content)
        role = "system" if m.role == "developer" else m.role
        messages.append(ChatMessage(role=role, content=text))
    return messages


def governance(headers: Any, metadata: dict[str, str] | None) -> dict[str, str]:
    """Each governance field from its header, else its `aiva_<field>` metadata key."""
    values: dict[str, str] = {}
    for field, header in GOVERNANCE.items():
        value = headers.get(header) or (metadata or {}).get(f"aiva_{field}")
        if value:
            values[field] = value.strip()
    return values


def run_profile(caller: Caller, fields: dict[str, str]) -> TestProfile | None:
    """An application credential runs as its own application; a user token names one (`X-AiVa-Application`)
    and may simulate its source (`X-AiVa-Simulated-Source-IP`). `resolve_run_identity` then checks that
    profile exactly as it checks the Test Console's: testers only, where simulated ingress is allowed."""
    application = fields.get("application")
    if caller.kind == "application":
        if application is not None and application != caller.application_id:
            raise PermissionDeniedError(
                "An application credential runs only as its own application.",
                remediation="Remove X-AiVa-Application, or use that application's credential.",
            )
        if "simulated_source_ip" in fields:
            raise PermissionDeniedError(
                "Simulated ingress is only available to testers.",
                remediation="Remove X-AiVa-Simulated-Source-IP when calling with an application credential.",
            )
        return None
    if application is None:
        raise Refused(
            400,
            "missing_application",
            "A user token must name its application in the X-AiVa-Application header.",
            "X-AiVa-Application",
        )
    return TestProfile(application_id=application, simulated_source_ip=fields.get("simulated_source_ip"))


def to_chat_request(
    body: ChatCompletionRequest, fields: dict[str, str], profile: TestProfile | None, services: Services
) -> tuple[ChatRequest, str | None]:
    """The native request and the required model (None for `aiva-auto`)."""
    required = None if body.model in (None, AUTO_MODEL) else body.model
    if required is not None:
        spec = services.catalog.model(required)
        if spec is None or spec.role != "upstream":
            raise Refused(404, "model_not_found", MODEL_NOT_FOUND.format(model=required), "model")
    classification = fields.get("data_classification")
    if classification is not None and classification not in {c.value for c in Classification}:
        raise Refused(
            400,
            "invalid_value",
            f"Unknown data classification '{classification}'.",
            GOVERNANCE["data_classification"],
        )
    tier = fields.get("quality_tier", "standard")
    if tier not in QUALITY_TIERS:
        raise Refused(400, "invalid_value", f"Unknown quality tier '{tier}'.", GOVERNANCE["quality_tier"])
    scenario = fields.get("scenario")
    if scenario is not None and services.catalog.scenario(scenario) is None:
        raise Refused(400, "invalid_value", f"Unknown scenario '{scenario}'.", GOVERNANCE["scenario"])
    tags = [t.strip() for t in fields.get("routing_tags", "").split(",") if t.strip()]
    try:
        request = ChatRequest(
            messages=to_messages(body),
            routing=RoutingOptions(mode="hybrid", tags=tags, preferred_model=required),
            data_classification=Classification(classification) if classification else None,
            task=fields.get("task"),
            quality_tier=tier,
            max_output_tokens=body.max_completion_tokens or body.max_tokens or DEFAULT_MAX_OUTPUT_TOKENS,
            scenario_id=scenario,
            test_profile=profile,
        )
    except ValidationError as exc:
        first = exc.errors()[0]
        param = ".".join(str(p) for p in first.get("loc", ())) or "messages"
        raise Refused(400, "invalid_value", f"Invalid '{param}': {first.get('msg', '')}.", param) from exc
    return request, required


def _finish_reason(raw: str | None) -> str:
    if raw in ("length", "max_tokens", "MAX_TOKENS"):
        return "length"
    if raw in ("refusal", "content_filter", "SAFETY"):
        return "content_filter"
    return "stop"


def render(
    ctx: RunContext, response: ChatResponse, *, ignored: list[str], key: str
) -> tuple[int, dict[str, Any]]:
    """The run as OpenAI shapes it: a `chat.completion`, or an error naming the run."""
    withheld = response.status is RunStatus.BLOCKED and response.outcome is Outcome.QUALITY_FAIL
    if (response.status is RunStatus.COMPLETED and response.response is not None) or withheld:
        message = response.response
        usage = response.usage
        completion = ChatCompletion(
            id=response.run_id,
            created=int(ctx.created_at.timestamp()),
            model=response.selected_model or AUTO_MODEL,
            choices=[
                CompletionChoice(
                    message=CompletionMessage(
                        content=None if withheld or message is None else message.content
                    ),
                    finish_reason="content_filter"
                    if withheld
                    else _finish_reason(message.finish_reason if message else None),
                )
            ],
            usage=CompletionUsage(
                prompt_tokens=usage.upstream_submitted_input_tokens,
                completion_tokens=usage.upstream_output_tokens,
                total_tokens=usage.upstream_submitted_input_tokens + usage.upstream_output_tokens,
                prompt_tokens_details=PromptTokensDetails(cached_tokens=usage.cached_input_tokens),
                completion_tokens_details=CompletionTokensDetails(reasoning_tokens=usage.reasoning_tokens),
            ),
            aiva=AivaCompletionInfo(
                run_id=response.run_id,
                trace_id=response.trace_id,
                status=response.status,
                execution_path=response.execution_path,
                selected_model=response.selected_model,
                rlcd_version=response.routing.rlcd_version if response.routing else None,
                policy_action=response.policy.action,
                effective_classification=response.policy.effective_classification,
                matched_rule_ids=response.policy.matched_rule_ids,
                cim=response.cim.message,
                cache_hit=response.cache.hit,
                output_dlp=response.assurance.output_dlp,
                provenance=message.provenance if message else usage.provenance,
                cost_micros=response.cost.total_api_micros,
                cost_provenance=response.cost.provenance,
                gpu_estimated_micros=response.cost.gpu_estimated_micros,
                ignored_parameters=ignored,
                idempotency_key=key.removeprefix(IDEMPOTENCY_PREFIX),
            ),
        )
        return 200, completion.model_dump(mode="json")
    error = response.error
    code = (error.code if error else "run_failed").lower()
    message_text = error.message if error else "The request did not complete."
    if response.outcome is Outcome.POLICY_DENIED:
        status_code = 403
        code = "model_not_allowed" if code == "model_not_allowed" else "policy_blocked"
    elif response.outcome is Outcome.APPROVAL_REQUIRED:
        status_code = 403
    elif response.outcome is Outcome.QUOTA_DENIED:
        status_code = 429
    elif ctx.http_status >= 400 and ctx.http_status not in GATEWAY_ERRORS:
        status_code = ctx.http_status
    else:
        # A run the gateway finished and refused, e.g. a live run whose endpoint is asleep (ADR-0027 §3):
        # unavailable. Never 502 or 504, which Space 2's front door replaces with its own "starting" page.
        status_code = 503
    envelope = OpenAIErrorEnvelope(
        error=OpenAIError(message=message_text, type=openai_error_type(status_code), code=code),
        aiva=AivaErrorInfo(
            run_id=response.run_id,
            status=response.status,
            rule_ids=error.rule_ids if error else [],
            remediation=error.remediation if error else None,
            retryable=error.retryable if error else False,
            approval_id=response.approval.approval_id,
            rlcd_version=response.routing.rlcd_version if response.routing else None,
            details=error.details if error else {},
        ),
    )
    return status_code, envelope.model_dump(mode="json", exclude_none=True)


def _pieces(text: str) -> Iterator[str]:
    """The answer in pieces of about STREAM_PIECE_CHARS characters, split at whitespace."""
    piece = ""
    for token in _STREAM_PIECE.findall(text):
        piece += token
        if len(piece) >= STREAM_PIECE_CHARS:
            yield piece
            piece = ""
    if piece:
        yield piece


def stream_events(completion: dict[str, Any], *, include_usage: bool) -> Iterator[str]:
    """A finished `chat.completion` as server-sent `chat.completion.chunk` events, then `[DONE]`."""
    head = {
        "id": completion["id"],
        "object": "chat.completion.chunk",
        "created": completion["created"],
        "model": completion["model"],
    }
    choice = completion["choices"][0]
    content = choice["message"]["content"]

    def event(delta: dict[str, Any], finish: str | None = None, **extra: Any) -> str:
        chunk = {
            **head,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish, "logprobs": None}],
            **extra,
        }
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    yield event({"role": "assistant", "content": ""})
    for piece in _pieces(content or ""):
        yield event({"content": piece})
    yield event({}, choice["finish_reason"], aiva=completion["aiva"])
    if include_usage:
        yield f"data: {json.dumps({**head, 'choices': [], 'usage': completion['usage']})}\n\n"
    yield "data: [DONE]\n\n"


@router.post(
    "/v1/chat/completions",
    response_model=ChatCompletion,
    summary="OpenAI-compatible chat completion through the AiVa pipeline (ADR-0026)",
    responses={
        400: {"model": OpenAIErrorEnvelope, "description": "A parameter AiVa cannot honour, or invalid."},
        401: {"model": OpenAIErrorEnvelope, "description": "Missing or invalid credentials."},
        403: {
            "model": OpenAIErrorEnvelope,
            "description": "Blocked by AI policy, model not allowed, or approval required.",
        },
        404: {"model": OpenAIErrorEnvelope, "description": "Unknown model."},
        429: {"model": OpenAIErrorEnvelope, "description": "Budget or rate limit."},
        503: {
            "model": OpenAIErrorEnvelope,
            "description": "A dependency was unavailable and the run failed closed, or a live run was "
            "refused, e.g. `live_demo_unavailable` with `aiva.details.reason` `endpoint_cold`: an internet "
            "key call never wakes a sleeping endpoint (ADR-0027 §3). Never 502 or 504.",
        },
    },
)
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    caller: RunCallerDep,
    services: ServicesDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=120)] = None,
) -> Response:
    try:
        refusals(body)
        fields = governance(request.headers, body.metadata)
        profile = run_profile(caller, fields)
        chat_request, required = to_chat_request(body, fields, profile, services)
    except Refused as refused:
        return refused.response()
    # OpenAI SDKs send no Idempotency-Key: one is generated so the run is still recorded under a key, but only
    # a caller's own key de-duplicates its retries.
    key = f"{IDEMPOTENCY_PREFIX}{idempotency_key or f'gen-{uuid.uuid4().hex}'}"
    ignored = ignored_parameters(body)
    outcome = await execute_chat(
        chat_request,
        request,
        caller,
        services,
        key,
        render=lambda ctx, response: render(ctx, response, ignored=ignored, key=key),
        required_model=required,
    )
    headers = {"Idempotent-Replayed": "true"} if outcome.replayed else {}
    if outcome.status_code >= 400 and not outcome.content.get("aiva", {}).get("retryable", False):
        headers["x-should-retry"] = "false"  # OpenAI SDKs then do not resend a refusal a retry cannot change
    if body.stream and outcome.status_code == 200:
        include_usage = bool(body.stream_options and body.stream_options.include_usage)
        return StreamingResponse(
            stream_events(outcome.content, include_usage=include_usage),
            media_type="text/event-stream",
            headers={**headers, "Cache-Control": "no-cache"},
        )
    return JSONResponse(status_code=outcome.status_code, content=outcome.content, headers=headers or None)


async def _models(caller: Caller, request: Request, services: Services) -> list[OpenAIModel]:
    """`aiva-auto` and the models a request from this application may use: its grants as policy leaves them
    for a request at the application's own classification floor. A request's own classification can narrow
    them further (a confidential request takes the sovereign path)."""
    profile = run_profile(caller, governance(request.headers, None))
    probe = ChatRequest(
        messages=[ChatMessage(role="user", content="Which models may this application use?")],
        test_profile=profile,
    )
    identity = resolve_run_identity(caller, profile, client_ip(request, services), services.settings)
    orchestrator = Orchestrator(services)
    ctx = orchestrator.new_context(probe, identity, caller.tenant_id)
    decision = await orchestrator.dry_run_policy(ctx)
    if decision.action == PolicyAction.DENY:
        raise Refused(403, "policy_blocked", "Blocked by AI policy.")
    models = [AUTO_MODEL_ENTRY]
    for model_id in sorted(decision.effective.allowed_models):
        spec = services.catalog.model(model_id)
        if spec is not None and spec.role == "upstream":
            models.append(OpenAIModel(id=spec.id, owned_by=spec.provider, display_name=spec.display_name))
    return models


AUTO_MODEL_ENTRY = OpenAIModel(
    id=AUTO_MODEL, owned_by="aiva", display_name="AiVa: RLCD chooses within policy"
)


@router.get(
    "/v1/models",
    response_model=OpenAIModelList,
    summary="Models this application may request through the OpenAI-compatible API (ADR-0026)",
    responses={401: {"model": OpenAIErrorEnvelope}, 403: {"model": OpenAIErrorEnvelope}},
)
async def list_models(request: Request, caller: RunCallerDep, services: ServicesDep) -> Response:
    try:
        models = await _models(caller, request, services)
    except Refused as refused:
        return refused.response()
    return JSONResponse(OpenAIModelList(data=models).model_dump(mode="json", exclude_none=True))


@router.get(
    "/v1/models/{model_id}",
    response_model=OpenAIModel,
    summary="One model this application may request (ADR-0026)",
    responses={
        401: {"model": OpenAIErrorEnvelope},
        403: {"model": OpenAIErrorEnvelope},
        404: {"model": OpenAIErrorEnvelope},
    },
)
async def get_model(model_id: str, request: Request, caller: RunCallerDep, services: ServicesDep) -> Response:
    try:
        models = await _models(caller, request, services)
    except Refused as refused:
        return refused.response()
    found = next((m for m in models if m.id == model_id), None)
    if found is None:
        return openai_error(404, "model_not_found", MODEL_NOT_FOUND.format(model=model_id), param="model")
    return JSONResponse(found.model_dump(mode="json", exclude_none=True))
