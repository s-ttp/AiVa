"""Public demo (ADR-0019): the shared refusal for live runs that cannot go ahead, and the public live budget
readout (`LiveBudget`, FinOps).

Every live-demo refusal is a blocked run in the QUOTA_EXCEEDED shape with code `LIVE_DEMO_UNAVAILABLE`
and `ErrorBody.details` = `LiveDemoUnavailable`: the reason, and when the limit resets (null for
`endpoint_cold`). The UI then offers the replay run. A live request is never silently downgraded.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from pydantic import Field

from aiva_gateway.contracts.common import ContractModel, ErrorBody
from aiva_gateway.core.enums import Provenance

LIVE_DEMO_UNAVAILABLE = "LIVE_DEMO_UNAVAILABLE"
LiveDemoReason = Literal["budget", "visitor_limit", "endpoint_cold"]


class LiveDemoUnavailable(ContractModel):
    """`ErrorBody.details` of a LIVE_DEMO_UNAVAILABLE refusal."""

    reason: LiveDemoReason
    resets_at: datetime | None = None  # null for endpoint_cold


def live_demo_unavailable(
    reason: LiveDemoReason,
    *,
    message: str,
    run_id: str | None,
    resets_at: datetime | None = None,
    rule_ids: Sequence[str] = (),
    remediation: str = "Run the scenario in replay instead.",
) -> ErrorBody:
    return ErrorBody(
        code=LIVE_DEMO_UNAVAILABLE,
        message=message,
        remediation=remediation,
        rule_ids=list(rule_ids),
        retryable=resets_at is not None,
        run_id=run_id,
        details=LiveDemoUnavailable(reason=reason, resets_at=resets_at).model_dump(mode="json"),
    )


class LiveBudget(ContractModel):
    """What the public demo shows about live runs (ADR-0019). Coarse by design: no amounts."""

    available: bool = Field(
        description="Whether a public live run would be admitted now: public budget left and, for a public "
        "visitor, live runs left this hour."
    )
    remaining_percent: int | None = Field(
        default=None,
        description="Public live budget left today, rounded down to a multiple of 10; null when no "
        "public-demo budget is configured (public live runs are then disabled).",
    )
    resets_at: datetime | None = Field(
        default=None,
        description="When the public live budget resets (next midnight UTC); null when public live runs "
        "are switched off (no budget, or a $0 cap).",
    )
    provenance: Provenance = Field(
        description="Label of the spend behind remaining_percent: `measured` provider spend, or `estimated` "
        "once GPU time is included."
    )
    visitor_runs_remaining: int | None = Field(
        default=None,
        description="Live runs this public visitor has left in the current hour; null for other callers.",
    )
    visitor_resets_at: datetime | None = Field(
        default=None,
        description="When this visitor's next live run frees up, if none are left; null when visitors' live "
        "runs are switched off (a limit of 0).",
    )
    unavailable_reason: LiveDemoReason | None = Field(
        default=None, description="Why a live run would be refused now, if it would be."
    )
