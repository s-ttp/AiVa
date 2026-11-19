"""Turn one run's records into aggregate lines (ADR-0015). Pure functions; integer micro-USD only.

A run yields one `run` line, one `attempt` line per model attempt and one line per ledger charge that is
not an attempt's own charge (`adjustment`, or `charge` for an attempt-less charge). Measures of different
line kinds never overlap, so summing any grouping of lines reproduces the attempt-level ledger exactly.

Savings waterfall (LLD §21.4, §22.7), per served run, against the pinned frontier baseline F. Every price is
taken on one token basis: the served attempt's own metered input tokens S (provider-counted, replayed or
simulated), with the caller's original context scaled to that basis by the gateway's estimates of the
original (O) and constructed (K) context, O' = S + (O - K) * r with r = S / K (tokens per estimated token)
capped at MAX_TOKENS_PER_ESTIMATE, so O' = S * O / K whenever r is a tokenizer ratio. Mixing the estimate
with the metered count would make the routing step negative whenever the provider counts more tokens.
  baseline  B = F(O', output tokens)
  context   C = B - F(S, output)
  routing   R = F(S, output) - list price of the served attempt on its own model (0 on the frontier model)
  prompt    P = list price - actual cost of the served attempt (provider prompt-cache discount)
so B - C - R - P equals the served attempt's actual cost exactly. A full response-cache hit avoids the
whole frontier cost of its source run's answer (K). CIM calls, failed or retried calls, ledger adjustments
and local GPU estimates are added back from the attempt lines. These are modelled values (`estimated`),
never quality-adjusted. Calls that measure a run rather than serve it (MEASUREMENT_STAGES: the opt-in
baseline comparison and a presenter's answer-quality comparison, ADR-0023) are a measurement cost, reported
separately and never part of the savings.
"""

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from aiva_gateway.core.enums import (
    AttemptStage,
    ChargeType,
    Classification,
    ExecutionPath,
    Outcome,
    Provenance,
)
from aiva_gateway.finops.pricing import PriceBook
from aiva_gateway.finops.scopes import least_trusted
from aiva_gateway.reporting.dimensions import DIMENSIONS

RUN, ATTEMPT, ADJUSTMENT, CHARGE = "run", "attempt", "adjustment", "charge"

# Providers count 0.9-2.1 tokens per estimated token on the recorded exchanges. A larger gap is fixed
# overhead (a template around a tiny context), which must not scale the removed context with it.
MAX_TOKENS_PER_ESTIMATE = 3

# A presenter's answer-quality comparison of a served run (ADR-0023): the frontier reference and the grader.
QUALITY_STAGES = (AttemptStage.QUALITY_REFERENCE.value, AttemptStage.QUALITY_GRADING.value)
# Calls that measure a run rather than serve it: metered on the run, never a cost of serving or a saving.
MEASUREMENT_STAGES = (AttemptStage.BASELINE.value, *QUALITY_STAGES)
# ADR-0023 §5: the one exception lets a comparison send scenario 11's synthetic confidential sample out.
QUALITY_EXCEPTION_SCENARIO = "live-sovereign-routing"
QUALITY_EXCEPTION_TENANT = "gct"

RUN_MEASURES = (
    "request_count",
    "cache_hit_count",
    "avoided_call_count",
    "avoided_tokens",
    "avoided_micros",
    "context_tokens_before",
    "context_tokens_after",
    "removed_passages",
    "passages_retrieved",
    "passages_kept",
    "quality_checked_count",
    "quality_passed_count",
    "reserved_micros",
    "committed_micros",
    "run_latency_ms_sum",
    "approval_exception_count",
    "confidential_external_attempts",
    "confidential_cim_attempts",
    "confidential_comparison_attempts",
    "frontier_baseline_micros",
    "savings_cache_micros",
    "savings_context_micros",
    "savings_routing_micros",
    "savings_prompt_cache_micros",
    "baseline_runs",
    "baseline_micros",
    "baseline_actual_micros",
    "baseline_passed_runs",
    "baseline_passed_savings_micros",
    # The /live "This session" counters, per run (value_measures): the Story mode's hero figures.
    "priced_request_count",
    "priced_api_micros",
    "priced_frontier_micros",
    "optimizable_tokens",
    "cache_tokens_saved",
    "context_tokens_saved",
)
ATTEMPT_MEASURES = (
    "attempt_count",
    "retry_count",
    "failed_attempt_count",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
    "retry_input_tokens",
    "retry_output_tokens",
    "api_micros",
    "retry_micros",
    "gpu_estimated_micros",
    "attempt_latency_ms_sum",
    "attempt_latency_count",
)
MEASURES = RUN_MEASURES + ATTEMPT_MEASURES
FLOAT_MEASURES = frozenset({"run_latency_ms_sum", "attempt_latency_ms_sum"})

_GPU = ChargeType.GPU_ESTIMATE.value
_ADJUSTMENT = ChargeType.ADJUSTMENT.value


@dataclass(frozen=True)
class AttemptFact:
    attempt_id: str
    stage: str
    model_id: str
    provider: str
    status: str
    retry_of: str | None
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    reasoning_tokens: int
    latency_ms: float | None
    provenance: str


@dataclass(frozen=True)
class LedgerFact:
    attempt_id: str | None
    charge_type: str
    amount_micros: int
    provenance: str
    rate_card_version: str


@dataclass(frozen=True)
class SourceAnswer:
    """The answer a full response-cache hit reused: its source run's served attempt."""

    model_id: str
    input_tokens: int
    output_tokens: int


@dataclass
class RunFacts:
    run_id: str
    dimensions: dict[str, str]
    execution_path: str
    provenance: str
    latency_ms: float | None
    attempts: list[AttemptFact] = field(default_factory=list)
    ledger: list[LedgerFact] = field(default_factory=list)
    cache_hit: bool = False
    avoided_call: bool = False
    avoided_tokens: int = 0
    avoided_micros: int = 0
    context_tokens_before: int = 0
    context_tokens_after: int = 0
    removed_passages: int = 0
    # Of the passages the CIM was given, those its package kept, on runs that sent the package.
    passages_retrieved: int = 0
    passages_kept: int = 0
    quality_passed: bool | None = None
    reserved_micros: int = 0
    committed_micros: int = 0
    approvals_consumed: int = 0
    cache_source: SourceAnswer | None = None
    # The run meets the gates of ADR-0023's exception that its record holds (quality_exception_run).
    quality_exception: bool = False
    # Comparison attempts whose own egress policy events, under the comparison's reservation, all allowed
    # them. Policy allows such a call on a confidential run only when the whole exception held, including the
    # synthetic sample and the presenter's request, which the run record cannot show.
    allowed_comparisons: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Pricing:
    """Prices for modelled savings: the pinned frontier baseline and each model's list price."""

    book: PriceBook
    frontier_model_id: str
    external_models: frozenset[str]

    @property
    def version(self) -> str:
        return self.book.version

    def frontier(self, input_tokens: int, output_tokens: int) -> int:
        return self.book.cost_micros(
            self.frontier_model_id, input_tokens=input_tokens, output_tokens=output_tokens
        )

    def list_price(self, model_id: str, input_tokens: int, output_tokens: int) -> int:
        return self.book.cost_micros(model_id, input_tokens=input_tokens, output_tokens=output_tokens)


Line = dict[str, Any]


def line_key(dimensions: Mapping[str, str]) -> str:
    material = "\x1f".join(dimensions[name] for name in DIMENSIONS)
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def _empty(dimensions: Mapping[str, str], **line: str) -> Line:
    values: Line = {**dimensions, **line}
    for name in MEASURES:
        values[name] = 0.0 if name in FLOAT_MEASURES else 0
    return values


def metered_original(sent: int, before: int, after: int) -> int:
    """The caller's original context on the served attempt's metered basis (module docstring)."""
    if after <= 0:
        return sent
    rate = min(sent, MAX_TOKENS_PER_ESTIMATE * after)  # tokens per estimated token, times `after`
    return max(0, sent + ((before - after) * rate + after // 2) // after)


def served_attempt(attempts: Sequence[AttemptFact]) -> AttemptFact | None:
    """The upstream attempt whose answer the run returned (the last successful one)."""
    ok = [a for a in attempts if a.stage == "upstream" and a.status == "ok"]
    return ok[-1] if ok else None


def quality_exception_run(
    *, tenant_id: str, scenario_id: str | None, classification: str, routing_mode: str, data_designation: str
) -> bool:
    """Whether a run meets the gates of ADR-0023's exception that its record holds, checked here apart from
    policy: the tenant and scenario, an effective classification of exactly confidential, a routing mode
    other than local-only and synthetic data. (The synthetic-sample gate is checked by policy on the bound
    request; run records keep no text, so the call's own policy event must also allow it: RunFacts.)"""
    return (
        tenant_id == QUALITY_EXCEPTION_TENANT
        and scenario_id == QUALITY_EXCEPTION_SCENARIO
        and classification == Classification.CONFIDENTIAL.value
        and routing_mode != "local-only"
        and data_designation == Provenance.SYNTHETIC.value
    )


def _confidential(dimensions: Mapping[str, str]) -> bool:
    try:
        return Classification(dimensions["sensitivity"]).requires_sovereign_path
    except ValueError:
        return False


def run_lines(facts: RunFacts, pricing: Pricing) -> list[Line]:
    """All lines of one run, ready to be summed into an aggregate bucket."""
    dims = facts.dimensions
    attempt_charges: dict[str, list[LedgerFact]] = {}
    loose: list[LedgerFact] = []
    for charge in facts.ledger:
        if charge.attempt_id and charge.charge_type != _ADJUSTMENT:
            attempt_charges.setdefault(charge.attempt_id, []).append(charge)
        else:
            loose.append(charge)

    served = served_attempt(facts.attempts)
    run_line = _empty(
        dims,
        line_kind=RUN,
        model_id=served.model_id if served else "",
        provider=served.provider if served else "",
        stage="",
        provenance=facts.provenance,
        rate_card_version=pricing.version,
    )
    run_line.update(
        request_count=1,
        cache_hit_count=int(facts.cache_hit),
        avoided_call_count=int(facts.avoided_call),
        avoided_tokens=facts.avoided_tokens,
        avoided_micros=facts.avoided_micros,
        # Context economics only for requests that submitted a context to an answering model.
        context_tokens_before=facts.context_tokens_before if served else 0,
        context_tokens_after=(facts.context_tokens_after or facts.context_tokens_before) if served else 0,
        removed_passages=facts.removed_passages,
        passages_retrieved=facts.passages_retrieved,
        passages_kept=facts.passages_kept,
        quality_checked_count=int(facts.quality_passed is not None),
        quality_passed_count=int(bool(facts.quality_passed)),
        reserved_micros=facts.reserved_micros,
        committed_micros=facts.committed_micros,
        run_latency_ms_sum=float(facts.latency_ms or 0.0),
        approval_exception_count=facts.approvals_consumed,
    )
    if _confidential(dims):
        # A comparison call always leaves: its reference and grader are external providers (ADR-0023 §1),
        # counted whether or not the catalog still lists the model.
        external = [
            a for a in facts.attempts if a.model_id in pricing.external_models or a.stage in QUALITY_STAGES
        ]
        # Comparison calls under ADR-0023's exception are reported apart, never hidden: on a run that meets
        # the gates its record holds, and allowed by their own policy events. Any other comparison call
        # counts as a leak with the rest, so the exception can never mask a breach.
        excepted = [
            a
            for a in external
            if facts.quality_exception
            and a.stage in QUALITY_STAGES
            and a.attempt_id in facts.allowed_comparisons
        ]
        run_line["confidential_external_attempts"] = len(external) - len(excepted)
        run_line["confidential_comparison_attempts"] = len(excepted)
        run_line["confidential_cim_attempts"] = sum(1 for a in facts.attempts if a.stage == "cim")

    attempt_lines = []
    api_by_attempt: dict[str, int] = {}
    for attempt in facts.attempts:
        charges = attempt_charges.get(attempt.attempt_id, [])
        api = sum(c.amount_micros for c in charges if c.charge_type != _GPU)
        api_by_attempt[attempt.attempt_id] = api
        line = _empty(
            dims,
            line_kind=ATTEMPT,
            model_id=attempt.model_id,
            provider=attempt.provider,
            stage=attempt.stage,
            provenance=attempt.provenance,
            rate_card_version=charges[0].rate_card_version if charges else "",
        )
        retry = attempt.retry_of is not None
        line.update(
            attempt_count=1,
            retry_count=int(retry),
            failed_attempt_count=int(attempt.status != "ok"),
            input_tokens=attempt.input_tokens,
            output_tokens=attempt.output_tokens,
            cached_input_tokens=attempt.cached_input_tokens,
            reasoning_tokens=attempt.reasoning_tokens,
            retry_input_tokens=attempt.input_tokens if retry else 0,
            retry_output_tokens=attempt.output_tokens if retry else 0,
            api_micros=api,
            retry_micros=api if retry else 0,
            gpu_estimated_micros=sum(c.amount_micros for c in charges if c.charge_type == _GPU),
            attempt_latency_ms_sum=float(attempt.latency_ms or 0.0),
            attempt_latency_count=int(attempt.latency_ms is not None),
        )
        attempt_lines.append(line)

    charge_lines = []
    models = {a.attempt_id: a for a in facts.attempts}
    for charge in loose:
        owner = models.get(charge.attempt_id or "")
        line = _empty(
            dims,
            line_kind=ADJUSTMENT if charge.charge_type == _ADJUSTMENT else CHARGE,
            model_id=owner.model_id if owner else "",
            provider=owner.provider if owner else "",
            stage=charge.charge_type,
            provenance=charge.provenance,
            rate_card_version=charge.rate_card_version,
        )
        if charge.charge_type == _GPU:
            line["gpu_estimated_micros"] = charge.amount_micros
        else:
            line["api_micros"] = charge.amount_micros
        charge_lines.append(line)

    _savings(run_line, facts, served, api_by_attempt, pricing)
    value_measures(run_line, facts, served, api_by_attempt)
    return [run_line, *attempt_lines, *charge_lines]


def _savings(
    line: Line,
    facts: RunFacts,
    served: AttemptFact | None,
    api_by_attempt: Mapping[str, int],
    pricing: Pricing,
) -> None:
    if facts.execution_path == ExecutionPath.CACHE_RETURN.value:
        if facts.cache_source is not None:
            source = facts.cache_source
            avoided = pricing.frontier(source.input_tokens, source.output_tokens)
        else:
            avoided = facts.avoided_micros  # source answer no longer on record: its modelled cost
        line["frontier_baseline_micros"] = avoided
        line["savings_cache_micros"] = avoided
        return
    if served is None:
        return
    before = facts.context_tokens_before
    after = facts.context_tokens_after or before
    sent = served.input_tokens
    full = pricing.frontier(metered_original(sent, before, after), served.output_tokens)
    constructed = pricing.frontier(sent, served.output_tokens)
    listed = pricing.list_price(served.model_id, sent, served.output_tokens)
    actual = api_by_attempt.get(served.attempt_id, 0)
    line["frontier_baseline_micros"] = full
    line["savings_context_micros"] = full - constructed
    line["savings_routing_micros"] = constructed - listed
    line["savings_prompt_cache_micros"] = listed - actual

    baseline = [a for a in facts.attempts if a.stage == "baseline" and a.status == "ok"]
    if baseline:
        baseline_cost = sum(api_by_attempt.get(a.attempt_id, 0) for a in baseline)
        measuring = {a.attempt_id for a in facts.attempts if a.stage in MEASUREMENT_STAGES}
        own = sum(cost for attempt_id, cost in api_by_attempt.items() if attempt_id not in measuring)
        line["baseline_runs"] = 1
        line["baseline_micros"] = baseline_cost
        line["baseline_actual_micros"] = own
        if facts.quality_passed:
            line["baseline_passed_runs"] = 1
            line["baseline_passed_savings_micros"] = baseline_cost - own


# The stages of AiVa's own route that the /live cost counter prices: the answering model and the CIM.
_ROUTE_STAGES = (AttemptStage.UPSTREAM.value, AttemptStage.CIM.value)


def value_measures(
    line: Line, facts: RunFacts, served: AttemptFact | None, api_by_attempt: Mapping[str, int]
) -> None:
    """One run's part of the /live "This session" counters (web `live/counters.ts`), so a week of runs and a
    session of the same runs agree.

    - Cost: a run is priced both ways when FinOps gives it a direct-frontier estimate
      (`finops.frontier.direct_frontier_estimate`): an answer was served, or a full response-cache hit
      returned one. That estimate is this run's `frontier_baseline_micros`. AiVa's side is the run's
      answering-model, CIM and billed tool-call spend at the rate cards: never local GPU hosting, ledger
      corrections or measurement calls (baseline and answer-quality comparisons). A cache hit whose source
      run is no longer on record has no frontier price (its baseline falls back to the source's own cost),
      so it is left out of both sides.
    - Tokens: an answered run's base is its original context and its answer, and the context the CIM pruned
      is saved; a full cache hit's base and saving are the tokens it avoided. Blocked and failed runs add 0.
    """
    cache_hit = facts.cache_hit and facts.execution_path == ExecutionPath.CACHE_RETURN.value
    if (cache_hit and facts.cache_source is not None) or served is not None:
        route = sum(api_by_attempt.get(a.attempt_id, 0) for a in facts.attempts if a.stage in _ROUTE_STAGES)
        tools = sum(c.amount_micros for c in facts.ledger if c.charge_type == ChargeType.TOOL_CALL.value)
        line["priced_request_count"] = 1
        line["priced_api_micros"] = route + tools
        line["priced_frontier_micros"] = line["frontier_baseline_micros"]
    if cache_hit:
        line["optimizable_tokens"] = line["cache_tokens_saved"] = facts.avoided_tokens
    elif facts.dimensions.get("outcome") == Outcome.SUCCESS.value:
        before = facts.context_tokens_before
        line["optimizable_tokens"] = before + (served.output_tokens if served else 0)
        line["context_tokens_saved"] = max(0, before - facts.context_tokens_after)


def add_into(totals: dict[str, Line], lines: Iterable[Line]) -> None:
    """Sum lines into `totals`, keyed by line key."""
    for line in lines:
        key = line_key(line)
        current = totals.get(key)
        if current is None:
            totals[key] = dict(line)
            continue
        for name in MEASURES:
            current[name] += line[name]


def least_trusted_label(labels: Iterable[str]) -> Provenance | None:
    """The least trustworthy provenance among stored labels (unknown labels are ignored)."""
    known = []
    for label in labels:
        try:
            known.append(Provenance(label))
        except ValueError:
            continue
    return least_trusted(known)


def run_label(provenance: str, data_designation: str) -> Provenance:
    """A run's own label in the trace list and detail: its recorded provenance, never better than
    `synthetic` for synthetic traffic, the same cap the aggregates apply (ADR-0015)."""
    synthetic = data_designation == Provenance.SYNTHETIC.value
    labels = [provenance, Provenance.SYNTHETIC.value] if synthetic else [provenance]
    return least_trusted_label(labels) or Provenance(provenance)
