"""`POST /v1/runs/{run_id}/quality-check`: a presenter's answer-quality comparison of a live run (ADR-0023).

The pinned frontier reference answers the run's question on its full, untrimmed context; the pinned grader
judges the served answer against it. Run records keep no text, so the request resends the run's request and
the answer it served; both are checked against the run before anything is called.
"""

from typing import Literal

from pydantic import Field

from aiva_gateway.contracts.chat import ChatRequest
from aiva_gateway.contracts.common import ContractModel
from aiva_gateway.core.enums import Provenance


class QualityCheckRequest(ContractModel):
    request: ChatRequest = Field(
        description="The run's original request, resent as it was: it must match the run's request hash."
    )
    answer: str = Field(
        max_length=200_000,
        description="The answer the run served, exactly: it must match the hash recorded when it was served.",
    )


class ReferencePassages(ContractModel):
    """The evidence the reference was given: the request's own passages, the knowledge-base passages the run
    retrieved (the CIM path), and the screened web passages the served model received (market research)."""

    supplied: int = 0
    knowledge_base: int = 0
    web: int = 0


class QualityReference(ContractModel):
    model_id: str
    display_name: str = Field(
        description="The model's name for people, from the catalog (e.g. 'GPT-6.1 Sol'); never its id."
    )
    answer: str | None = Field(
        default=None,
        description="The reference model's answer. In this response only: never stored, so a repeated "
        "request returns null.",
    )
    passages: ReferencePassages = Field(default_factory=ReferencePassages)
    note: str | None = Field(
        default=None,
        description="What the reference did not see, e.g. 'Reference without the web results (they are no "
        "longer held)'. The grader is told the same. Web results are never searched again.",
    )
    input_tokens: int = 0
    output_tokens: int = 0
    provenance: Provenance | None = None


class QualityGrade(ContractModel):
    model_id: str
    display_name: str = Field(
        description="The model's name for people, from the catalog (e.g. 'GPT-6.1 Sol'); never its id."
    )
    verdict: Literal["match", "partial", "miss"] = Field(
        description="From the score: match from 90, partial from 40, miss below 40."
    )
    score: int = Field(
        ge=0,
        le=100,
        description="Whether the answer gives the information the question needs: 90-100 every key point, "
        "70-89 minor gaps, 40-69 important gaps, 0-39 wrong, refused or failed.",
    )
    missed_points: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Information the question needs that the served answer misses or gets wrong (at most 5). "
        "In this response only: never stored, so a repeated request returns an empty list.",
    )
    provenance: Literal["estimated"] = Field(
        default="estimated", description="AI-judged by the grader model: an estimate, never measured."
    )


class QualityCost(ContractModel):
    reference_micros: int = 0
    grading_micros: int = 0
    total_micros: int = 0
    direct_frontier_measured_micros: int | None = Field(
        default=None,
        description="What answering this request on the frontier model cost, measured: the reference call. "
        "Null when no reference answer was produced.",
    )
    rate_card_version: str
    provenance: Provenance = Field(description="`measured` from provider usage.")


class QualityCheckResponse(ContractModel):
    run_id: str
    status: Literal["compared", "denied", "not_reserved", "failed"] = Field(
        description="compared: the grader judged the answer. denied: policy refused a comparison call; "
        "nothing was called. not_reserved: the budget refused the comparison's envelope; nothing was called. "
        "failed: the reference or grader cannot be called here (nothing was reserved or called), or a call "
        "failed after the reservation (what did run is reported and metered).",
    )
    detail: str | None = Field(
        default=None,
        description="UI copy, e.g. 'grader not configured', 'Grader unavailable: no credentials configured', "
        "'comparison not reserved', 'Comparison is available for the synthetic sample only.'",
    )
    repeated: bool = Field(
        default=False,
        description="This run was already compared: the recorded verdict, score and costs are returned and "
        "nothing is called or reserved again. Answers and missed points are never stored.",
    )
    exception_applied: bool = Field(
        default=False,
        description="The run's confidential synthetic sample left AiVa for the comparison under ADR-0023's "
        "one exception (scenario 11). The served answer did not.",
    )
    rule_ids: list[str] = Field(default_factory=list, description="Policy rules that denied a call.")
    reference: QualityReference | None = None
    grade: QualityGrade | None = None
    cost: QualityCost
