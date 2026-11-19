"""Pydantic models mirroring the YAML files in `config/`."""

import re
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aiva_gateway.contracts.messages import CriticalFigure, DocumentType, EvidenceItem
from aiva_gateway.core.enums import Classification, Role
from aiva_gateway.core.money import MICROS_PER_USD


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Tenant(_Strict):
    id: str
    name: str
    currency: Literal["USD"] = "USD"


class CostCenter(_Strict):
    id: str
    code: str
    name: str


class Department(_Strict):
    id: str
    name: str
    business_function: str
    cost_center_id: str


class TrustedNetwork(_Strict):
    id: str
    name: str
    cidr: str


class Application(_Strict):
    id: str
    name: str
    department_id: str
    trusted_network_ids: list[str]
    allowed_tasks: list[str]
    allowed_models: list[str]
    allowed_tools: list[str] = Field(default_factory=list)
    classification_floor: Classification = Classification.INTERNAL
    enabled: bool = True
    # Accepts calls made with one of its keys from any source, including the null source of a proxied
    # request (ADR-0019), so keys work on the deployed gateway (ADR-0027). Users still need a trusted network
    # and every other rule still applies. Not a network: a 0.0.0.0/0 tenant network would contain every
    # visitor's address and void the proxy's client attestation.
    internet_callers: bool = False
    # Response-cache sharing (LLD §11): `per_user` scopes every cached answer to the end user, for
    # customer-facing applications; `shared` lets non-personal answers serve the application's role set.
    cache_sharing: Literal["per_user", "shared"] = "shared"


class User(_Strict):
    id: str
    display_name: str
    roles: list[Role]
    department_id: str | None = None
    owned_application_ids: list[str] = Field(default_factory=list)


class Budget(_Strict):
    scope_type: Literal[
        "tenant",
        "department",
        "application",
        "user",
        "model",
        "request",
        "workflow",
        "gpu_pool",
        "public_demo",
        "internet_gpu",
    ]
    scope_id: str
    period: Literal["daily", "monthly", "request", "workflow"]
    cap_usd: Decimal
    alert_thresholds: list[float]
    on_exhaustion: Literal["block", "approval_required", "lower_cost_route"]


class TenantConfig(_Strict):
    tenant: Tenant
    cost_centers: list[CostCenter]
    departments: list[Department]
    trusted_networks: list[TrustedNetwork]
    applications: list[Application]
    users: list[User]
    budgets: list[Budget]


# A Hub repository pinned to a revision: `owner/name@<hex sha>`.
PINNED_REPO = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{7,40}$")


class Hosting(_Strict):
    """Where a model runs, as the live demo page's info balloon shows it (ADR-0021): the platform and the
    machine, never an endpoint URL."""

    platform: str
    instance: str | None = None
    precision: str | None = None
    serving: str | None = None
    scale_to_zero_minutes: int | None = Field(default=None, ge=1)


class FineTuning(_Strict):
    """How fine-tuned weights were made. Recorded facts only: what cannot be verified is left out, and an
    evaluation figure names where it is recorded."""

    method: str
    label: str | None = Field(
        default=None,
        max_length=60,
        description="The weights in a few words for the info balloon, e.g. 'Qwen3-32B + GCT LoRA'.",
    )
    adapter: str | None = Field(default=None, description="The LoRA adapter, `repo@revision`.")
    dataset: str | None = Field(default=None, description="The training dataset, `repo@revision`.")
    dataset_size: str | None = None
    data_generator: str | None = Field(default=None, description="The model that generated the data.")
    evaluation: str | None = None
    evaluation_source: str | None = Field(default=None, description="Where `evaluation` is recorded.")

    @model_validator(mode="after")
    def _cited(self) -> "FineTuning":
        if (self.evaluation is None) != (self.evaluation_source is None):
            raise ValueError("an evaluation figure and its source are given together")
        for name in ("adapter", "dataset"):
            value = getattr(self, name)
            if value is not None and not PINNED_REPO.match(value):
                raise ValueError(f"{name} must be a pinned `repo@revision`")
        return self


# What an answer is grounded in (ADR-0009, 2026-10-22 grounded-answers amendment): no evidence; knowledge
# passages only (every passage has a citation header: KB sections and web results); or operational records
# (at least one passage without one: incident and site records, alarms, tool outputs).
Grounding = Literal["none", "knowledge", "records"]


class GroundedAnswers(_Strict):
    """How a model answers a request grounded in one kind of evidence. `qualified: false` takes it out of
    RLCD's qualified set for that kind (it still serves when no other eligible model qualifies); `temperature`
    is its answer temperature there (null: the provider's default)."""

    qualified: bool = True
    temperature: float | None = Field(default=None, ge=0, le=2)


class ModelSpec(_Strict):
    id: str
    display_name: str
    # `grader`: judges a live answer against the frontier reference (ADR-0023); never a routing target.
    role: Literal["upstream", "cim", "grader"]
    provider: Literal["hf-endpoint", "hf-router", "google", "anthropic", "openai", "simulated"]
    provider_model_id: str
    revision: str
    sovereign: bool
    external: bool
    context_window: int
    rate_card_id: str
    quality_rank: int
    adapter: str | None = None
    endpoint_name: str | None = None
    provider_route: str | None = None
    region: str | None = None
    # Weights a dedicated endpoint serves when they differ from the base pin (`provider_model_id`@`revision`),
    # e.g. a merged fine-tune. The endpoint resolver checks them against the HF Endpoints API before a call.
    weights_repo: str | None = None
    weights_revision: str | None = None
    # The CIM's reply contract (ADR-0018): v1 returns the package (`upstream_messages`); v2 returns selection
    # and facts, and the gateway builds the package. Set by the weights a CIM endpoint serves.
    cim_contract: Literal["v1", "v2"] = "v1"
    # The task categories the CIM's weights were trained on (taxonomy `task_category` ids); a request of any
    # other category bypasses the CIM, with the full context used. Null: no restriction.
    cim_trained_tasks: list[str] | None = None
    # The live demo page's info balloon (ADR-0021): a one-line summary, where the model runs and, for
    # fine-tuned weights, how they were made. Recorded facts only; never a URL or a secret.
    summary: str | None = None
    hosting: Hosting | None = None
    fine_tuning: FineTuning | None = None
    # Per kind of grounding: whether it qualifies to answer and at what temperature. Absent: qualified, at the
    # provider's default temperature. Ungrounded requests always take the default.
    grounding: dict[Literal["knowledge", "records"], GroundedAnswers] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _weights_pin(self) -> "ModelSpec":
        if (self.cim_contract != "v1" or self.cim_trained_tasks is not None) and self.role != "cim":
            raise ValueError(f"model {self.id}: cim_contract and cim_trained_tasks apply to the CIM only")
        if (self.weights_repo is None) != (self.weights_revision is None):
            raise ValueError(f"model {self.id}: weights_repo and weights_revision are pinned together")
        if self.weights_repo is not None and self.provider != "hf-endpoint":
            raise ValueError(f"model {self.id}: served weights are pinned only for hf-endpoint models")
        if self.fine_tuning is not None and self.weights_repo is None:
            raise ValueError(f"model {self.id}: fine-tuning facts describe pinned fine-tuned weights")
        if self.grounding and self.role != "upstream":
            raise ValueError(f"model {self.id}: grounding applies to answering (upstream) models only")
        if self.provider == "anthropic" and any(g.temperature is not None for g in self.grounding.values()):
            # Thinking is always on for the Anthropic models pinned here, and it does not take a temperature.
            raise ValueError(f"model {self.id}: an Anthropic model's temperature cannot be set")
        return self

    def answers(self, grounding: Grounding) -> bool:
        """Whether RLCD may choose it to answer a request with this grounding."""
        settings = self.grounding.get(grounding) if grounding != "none" else None
        return settings is None or settings.qualified

    def answer_temperature(self, grounding: Grounding) -> float | None:
        """Its answer temperature for this grounding; None: the provider's default."""
        settings = self.grounding.get(grounding) if grounding != "none" else None
        return settings.temperature if settings is not None else None

    @property
    def served_weights(self) -> tuple[str, str]:
        """Repository and revision the model's endpoint must serve."""
        if self.weights_repo is not None and self.weights_revision is not None:
            return self.weights_repo, self.weights_revision
        return self.provider_model_id, self.revision

    @property
    def pinned_version(self) -> str:
        """Version label for a dedicated endpoint: `<repo>@<revision[:12]>` of the weights it serves."""
        repo, revision = self.served_weights
        return f"{repo}@{revision[:12]}"


class BaselineCall(_Strict):
    """The opt-in baseline comparison call to `frontier_baseline` (ADR-0009, 2026-10-18 amendment).

    Thinking on the frontier model counts against its output cap, so the request's budget can leave the
    baseline no room to answer. Its call and its reservation line use the same output budget.
    """

    max_output_tokens: int | None = Field(
        default=None, ge=1, description="Output budget, or the request's if larger; null = the request's."
    )
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = Field(
        default=None,
        description="Reasoning effort, where the provider takes one; null = the adapter's setting.",
    )


class ClassifierSpec(_Strict):
    """The RLCD classifier (ADR-0009): it runs inside the gateway, so it is no routing target. The gateway
    loads `weights_repo`@`weights_revision` (`optimize.rlcd_model`); the rest feeds its info balloon."""

    display_name: str
    summary: str
    weights_repo: str
    weights_revision: str
    base_model: str = Field(description="`repo@revision` of the base encoder.")
    hosting: Hosting
    fine_tuning: FineTuning | None = None

    @model_validator(mode="after")
    def _pinned(self) -> "ClassifierSpec":
        if not PINNED_REPO.match(f"{self.weights_repo}@{self.weights_revision}"):
            raise ValueError("the RLCD weights are a pinned `repo` and revision")
        if not PINNED_REPO.match(self.base_model):
            raise ValueError("the RLCD base model is a pinned `repo@revision`")
        return self


class ModelRegistry(_Strict):
    frontier_baseline: str
    baseline: BaselineCall = BaselineCall()
    cim_model: str
    # The pinned grader for answer-quality comparisons (ADR-0023), a `grader`-role model. Null: no grading.
    grader_model: str | None = None
    models: list[ModelSpec]
    rlcd: ClassifierSpec | None = None


class ToolSpec(_Strict):
    """A tool an agent may call. The registry is authoritative for access and egress (ADR-0012)."""

    id: str
    display_name: str
    description: str
    access: Literal["read", "write"] = Field(description="write = the call has side effects.")
    egress: Literal["internal", "external"] = Field(
        description="external = the call sends data outside the GCT trust boundary."
    )
    enabled: bool = True


class ToolRegistry(_Strict):
    tools: list[ToolSpec]


class RateCard(_Strict):
    id: str
    model_id: str
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cached_input_per_mtok: Decimal
    effective_from: date
    source: str
    gpu_per_hour: Decimal | None = None

    @field_validator("input_per_mtok", "output_per_mtok", "cached_input_per_mtok", "gpu_per_hour")
    @classmethod
    def _whole_micros(cls, value: Decimal | None) -> Decimal | None:
        """Prices convert to integer micro-USD exactly (ADR-0006): non-negative, at most 6 decimal places."""
        if value is None:
            return None
        micros = value * MICROS_PER_USD
        if not value.is_finite() or value < 0 or micros != micros.to_integral_value():
            raise ValueError(
                "must be a non-negative USD price with at most 6 decimal places (whole micro-USD)"
            )
        return value


class ToolRateCard(_Strict):
    """A tool's price per billed unit (Tavily: per API credit; a basic search is 1, an advanced one 2)."""

    id: str
    tool_id: str
    unit: str
    per_unit: Decimal
    effective_from: date
    source: str

    @field_validator("per_unit")
    @classmethod
    def _whole_micros(cls, value: Decimal) -> Decimal:
        """Prices convert to integer micro-USD exactly (ADR-0006): non-negative, at most 6 decimal places."""
        micros = value * MICROS_PER_USD
        if not value.is_finite() or value < 0 or micros != micros.to_integral_value():
            raise ValueError(
                "must be a non-negative USD price with at most 6 decimal places (whole micro-USD)"
            )
        return value


class RateCards(_Strict):
    version: str
    currency: Literal["USD"]
    rate_cards: list[RateCard]
    tool_rate_cards: list[ToolRateCard] = Field(default_factory=list)


class PolicyMatch(_Strict):
    classification_any: list[Classification] | None = None
    application_any: list[str] | None = None
    task_any: list[str] | None = None
    tags_any: list[str] | None = None
    dlp_findings_any: list[str] | None = None
    injection_detected: bool | None = None
    source_cidr_any: list[str] | None = None
    environment_any: list[str] | None = None
    evidence_injection_detected: bool | None = None
    tool_any: list[str] | None = None
    tool_access_any: list[Literal["read", "write"]] | None = None
    tool_egress_any: list[Literal["internal", "external"]] | None = None


class PolicyEnforce(_Strict):
    action: Literal["deny", "approval_required"] | None = None
    reason: str | None = None
    remediation: str | None = None
    execution_path: Literal["sovereign-direct"] | None = None
    allowed_models: list[str] | None = None
    cim: Literal["bypass"] | None = None
    external_providers: Literal["deny"] | None = None
    output_dlp: Literal["required"] | None = None
    audit_level: Literal["standard", "enhanced"] | None = None


class PolicyDocument(_Strict):
    """An AI-PEL policy. Policies only restrict; grants come from the application registry (ADR-0005)."""

    policy_id: str
    title: str
    priority: int
    match: PolicyMatch
    enforce: PolicyEnforce


class SampleArtifact(_Strict):
    """Synthetic inputs for `POST /v1/artifacts` (scenario 10); the loader validates them against the
    request contract, and every figure against its source passage."""

    document_type: DocumentType
    title: str
    instructions: str
    evidence: list[EvidenceItem]
    figures: list[CriticalFigure] = Field(default_factory=list)


class SampleWorkflow(_Strict):
    """Synthetic inputs for `POST /v1/agents/workflows` (scenario 6)."""

    incident_id: str
    instructions: str | None = None


QualityTier = Literal["standard", "high", "critical"]
# The lowest model quality rank RLCD may choose at each quality tier (`optimize.rlcd`).
MIN_QUALITY_RANK: dict[str, int] = {"standard": 1, "high": 2, "critical": 3}


class Scenario(_Strict):
    id: str
    number: int
    name: str
    primary_proof: str
    application_id: str
    task_category: str
    business_intent: str
    declared_classification: Classification
    routing_tags: list[str]
    sample_prompt: str
    context_profile: str | None = None
    # The default request's quality tier and output budget: the Test Console's draft and the scenario
    # recordings send them. A frontier model's thinking counts against max_output_tokens.
    quality_tier: QualityTier = "standard"
    max_output_tokens: int = Field(default=1024, ge=1, le=32_768)
    # The live demo page's use case this scenario fills (ADR-0021): run live there, so kept out of the replay
    # set. Each use case has at most one scenario.
    live_demo_use_case: (
        Literal[
            "sovereign-routing",
            "frontier-optimization",
            "policy-enforcement",
            "inference-api",
            "market-research",
            "code-generation",
        ]
        | None
    ) = None
    # A live demo use case's other questions (owner decision, 2026-10-25): the page asks `sample_prompt` or
    # one of these, picked at random. Each keeps the scenario's application, classification, evidence and
    # route; `sample_prompt` stays the one the recordings and the replay set use.
    alternate_prompts: list[str] = Field(default_factory=list)
    deferred: str | None = None
    # Structured synthetic inputs for the Test Console, so the UI never has to invent content.
    # `sample_evidence` is the default chat request's `evidence` (scenario 5's alarm records).
    sample_evidence: list[EvidenceItem] | None = None
    sample_artifact: SampleArtifact | None = None
    sample_workflow: SampleWorkflow | None = None


class ScenarioCatalog(_Strict):
    scenarios: list[Scenario]


class Taxonomy(_Strict):
    version: str
    review_confidence_threshold: float
    dimensions: dict[str, list[str]]


class CitationFields(_Strict):
    """The required fields a knowledge passage header `[<document> §<n>]` supplies (`optimize/citations`)."""

    document: str
    section: str


class ContextProfile(_Strict):
    id: str
    required_fields: list[str]
    target_budget_tokens: int
    preserve_numbers: bool = True
    chronology_field: str | None = None
    # Required fields that name a passage's source rather than a fact it holds. The Context Guard checks
    # citation integrity for them (kept passages keep their header; every citation resolves to a passage of
    # the request) and does not pin a passage for carrying them.
    citation: CitationFields | None = None
    # Recall floor: the first k retrieved passages, in retrieval rank order, are always kept in the package.
    keep_top_k: int = Field(default=0, ge=0)
    # For applications that accept no omission: every passage is kept, so the CIM is never called.
    keep_all_passages: bool = False
    # Knowledge-base retrieval on the sovereign-direct path (ADR-0020): up to k passages whose cosine
    # similarity to the question reaches the floor, only when the caller supplied no evidence. 0: off.
    sovereign_retrieval_k: int = Field(default=0, ge=0)
    sovereign_retrieval_min_cosine: float = Field(default=0.62, ge=0.0, le=1.0)
    # Heading-aware fusion for this profile's retrieval (ADR-0009, 2026-10-18 amendment): enabled only where
    # it was measured.
    heading_fusion: bool = False
    # Governed live web search (ADR-0022): on the CIM-assisted path, up to k web results join the evidence,
    # after policy allows the `web.search` call and the reservation covers it. 0: off.
    web_search_k: int = Field(default=0, ge=0, le=5)

    @model_validator(mode="after")
    def _citation_fields_are_required(self) -> "ContextProfile":
        if self.citation and not {self.citation.document, self.citation.section} <= set(self.required_fields):
            raise ValueError(f"profile {self.id}: citation fields must be required fields")
        return self

    @property
    def citation_fields(self) -> frozenset[str]:
        return frozenset((self.citation.document, self.citation.section)) if self.citation else frozenset()


class ContextProfiles(_Strict):
    # Task categories that skip retrieval (knowledge base and web) on the CIM-assisted path.
    retrieval_skip_tasks: list[str] = Field(default_factory=list)
    profiles: list[ContextProfile]
