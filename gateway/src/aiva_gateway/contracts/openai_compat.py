"""OpenAI-compatible API contract (ADR-0026): `POST /v1/chat/completions` and `GET /v1/models`.

A facade over `POST /v1/ai/chat`: the request is mapped onto a `ChatRequest` and runs through the same
pipeline (policy, DLP, quota, RLCD, CIM, assurance, metering, tracing). Parameters AiVa cannot honour are
refused with a 400 rather than ignored silently; sampling parameters AiVa sets itself are accepted, not
applied, and listed in `aiva.ignored_parameters`.
"""

from typing import Any, Literal

from pydantic import ConfigDict, Field

from aiva_gateway.contracts.common import ContractModel
from aiva_gateway.core.enums import Classification, ExecutionPath, PolicyAction, Provenance, RunStatus

AUTO_MODEL = "aiva-auto"


class OpenAIContentPart(ContractModel):
    """One part of a message's content. Only `text` parts are supported; other parts (images, audio, files)
    are kept here so the request can be refused with a clear message."""

    model_config = ConfigDict(extra="allow")

    type: str
    text: str | None = Field(default=None, max_length=200_000)


class OpenAIMessage(ContractModel):
    role: Literal["system", "developer", "user", "assistant", "tool", "function"]
    content: str | list[OpenAIContentPart] | None = Field(default=None)
    name: str | None = Field(default=None, max_length=64, description="Accepted and not used.")
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    function_call: dict[str, Any] | None = None
    refusal: str | None = None


class StreamOptions(ContractModel):
    include_usage: bool = False


class ChatCompletionRequest(ContractModel):
    model: str | None = Field(
        default=None,
        description=f"`{AUTO_MODEL}` or omitted: RLCD chooses inside the policy-eligible set. A model id "
        "from GET /v1/models: served only if policy and the execution path leave it eligible, else 403 "
        "`model_not_allowed`; never substituted.",
    )
    messages: list[OpenAIMessage] = Field(min_length=1, max_length=500)
    max_completion_tokens: int | None = Field(default=None, ge=1, le=32_768)
    max_tokens: int | None = Field(
        default=None, ge=1, le=32_768, description="Older name of max_completion_tokens, which wins if both."
    )
    stream: bool = Field(
        default=False,
        description="Buffered: the run completes (output DLP and assurance need the whole answer), then the "
        "answer is sent as `chat.completion.chunk` server-sent events.",
    )
    stream_options: StreamOptions | None = None
    metadata: dict[str, str] | None = Field(
        default=None,
        max_length=16,
        description="Governance fields as `aiva_*` keys when headers are not available (see ADR-0026); other "
        "keys are accepted and not stored.",
    )
    # AiVa sets sampling per model and grounding (ADR-0009): accepted, not applied, listed in the response.
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    seed: int | None = None
    reasoning_effort: str | None = None
    service_tier: str | None = None
    store: bool | None = None
    user: str | None = Field(default=None, max_length=256, description="Accepted and never stored.")
    # Refused unless absent (or a value that asks for nothing): AiVa cannot honour them.
    n: int | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    functions: list[dict[str, Any]] | None = None
    function_call: str | dict[str, Any] | None = None
    logprobs: bool | None = None
    top_logprobs: int | None = None
    response_format: dict[str, Any] | None = None
    stop: str | list[str] | None = None
    logit_bias: dict[str, int] | None = None
    modalities: list[str] | None = None
    audio: dict[str, Any] | None = None
    prediction: dict[str, Any] | None = None
    web_search_options: dict[str, Any] | None = None


class CompletionMessage(ContractModel):
    role: Literal["assistant"] = "assistant"
    content: str | None = Field(description="Null when output assurance withheld the answer.")
    refusal: str | None = None


class CompletionChoice(ContractModel):
    index: int = 0
    message: CompletionMessage
    finish_reason: Literal["stop", "length", "content_filter"]
    logprobs: None = None


class PromptTokensDetails(ContractModel):
    cached_tokens: int = 0


class CompletionTokensDetails(ContractModel):
    reasoning_tokens: int = 0


class CompletionUsage(ContractModel):
    """The answering model's tokens: what it was sent and wrote (the CIM and any baseline are in the run)."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    prompt_tokens_details: PromptTokensDetails = Field(default_factory=PromptTokensDetails)
    completion_tokens_details: CompletionTokensDetails = Field(default_factory=CompletionTokensDetails)


class AivaCompletionInfo(ContractModel):
    """What AiVa did with the request. The full record is `GET /v1/runs/{run_id}`."""

    run_id: str
    trace_id: str
    status: RunStatus
    execution_path: ExecutionPath
    selected_model: str | None
    rlcd_version: str | None = Field(
        default=None,
        description="The router that chose the model: the RLCD model's version, or `heuristic-v0` when it "
        "was unavailable. Null when no routing ran (a cache return).",
    )
    policy_action: PolicyAction
    effective_classification: Classification
    matched_rule_ids: list[str] = Field(default_factory=list)
    cim: str = Field(description="The CIM's state, as the UI shows it (LLD §22.8).")
    cache_hit: bool = False
    output_dlp: Literal["clean", "redacted", "blocked", "not-run"]
    provenance: Provenance = Field(description="Label of the answer and of `usage`.")
    cost_micros: int = Field(description="Provider-billable API cost of the run, integer micro-USD.")
    cost_provenance: Provenance
    gpu_estimated_micros: int = 0
    ignored_parameters: list[str] = Field(
        default_factory=list, description="Parameters accepted but not applied, e.g. `temperature`."
    )
    idempotency_key: str = Field(
        description="The key the run was recorded under: the caller's Idempotency-Key, or one generated "
        "for it (`gen-…`), which does not de-duplicate a client's retries."
    )


class ChatCompletion(ContractModel):
    id: str = Field(description="The AiVa run id.")
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str = Field(description="The model that served the answer.")
    choices: list[CompletionChoice]
    usage: CompletionUsage
    system_fingerprint: str | None = None
    aiva: AivaCompletionInfo


class OpenAIModel(ContractModel):
    id: str
    object: Literal["model"] = "model"
    created: int = 0
    owned_by: str
    display_name: str | None = None


class OpenAIModelList(ContractModel):
    object: Literal["list"] = "list"
    data: list[OpenAIModel]


class OpenAIError(ContractModel):
    message: str
    type: str
    param: str | None = None
    code: str | None = None


class AivaErrorInfo(ContractModel):
    run_id: str | None = None
    status: RunStatus | None = None
    rule_ids: list[str] = Field(default_factory=list)
    remediation: str | None = None
    retryable: bool = False
    approval_id: str | None = None
    rlcd_version: str | None = Field(
        default=None, description="The router's version, when the run was refused after routing; else null."
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        description='The native error\'s details, e.g. `{"reason": "endpoint_cold"}` for a live run refused '
        "because its endpoint is asleep (ADR-0027 §3).",
    )


class OpenAIErrorEnvelope(ContractModel):
    error: OpenAIError
    aiva: AivaErrorInfo | None = None
