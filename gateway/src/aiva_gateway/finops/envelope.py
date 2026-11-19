"""Worst-case budget envelopes and lower-cost narrowing (LLD §6 step 10, §12).

The envelope is the maximum a run may spend: every upstream attempt (initial + bounded retry) at the
full output allowance, plus the CIM call and the opt-in baseline comparison at its own output budget, which
can exceed the request's (ADR-0009, 2026-10-18 amendment). Shared scopes reserve the total; each model the
run may call gets a line for its own model scope and, if local, its GPU pool.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from aiva_gateway.catalog.models import ModelSpec
from aiva_gateway.finops.pricing import PriceBook
from aiva_gateway.pipeline.interfaces import QuotaDeniedError, ReservationLine

LOWER_COST_ROUTE = "lower_cost_route"


@dataclass(frozen=True)
class Envelope:
    amount_micros: int
    lines: tuple[ReservationLine, ...]
    upstream_micros: dict[str, int]
    # Worst case of the billed tool calls the run plans (in `amount_micros`, never a model line).
    tool_micros: int = 0


def build_envelope(
    prices: PriceBook,
    *,
    upstream: Sequence[ModelSpec],
    upstream_attempts: int,
    input_tokens: int,
    max_output_tokens: int,
    cim: ModelSpec | None = None,
    cim_attempts: int = 1,
    baseline: ModelSpec | None = None,
    baseline_max_output_tokens: int | None = None,
    tools: Mapping[str, int] | None = None,
) -> Envelope:
    amounts: dict[str, int] = {}
    gpu: dict[str, int] = {}

    def add(model: ModelSpec, amount: int, attempts: int) -> None:
        amounts[model.id] = amounts.get(model.id, 0) + amount
        gpu[model.id] = gpu.get(model.id, 0) + prices.gpu_worst_case_micros(model.id, attempts=attempts)

    upstream_micros = upstream_costs(
        prices,
        upstream,
        upstream_attempts=upstream_attempts,
        input_tokens=input_tokens,
        max_output_tokens=max_output_tokens,
    )
    for model in upstream:
        add(model, upstream_micros[model.id], upstream_attempts)
    total = max(upstream_micros.values(), default=0)
    if cim is not None:
        # The CIM may return up to the full context it was given, on each of its bounded attempts.
        cost = prices.worst_case_micros(
            cim, input_tokens=input_tokens, max_output_tokens=input_tokens, attempts=cim_attempts
        )
        add(cim, cost, cim_attempts)
        total += cost
    if baseline is not None:
        cost = prices.worst_case_micros(
            baseline,
            input_tokens=input_tokens,
            max_output_tokens=baseline_max_output_tokens or max_output_tokens,
            attempts=1,
        )
        add(baseline, cost, 1)
        total += cost
    lines = tuple(
        ReservationLine(
            model_id=model_id,
            amount_micros=amount,
            gpu_pool=prices.pool_of(model_id),
            gpu_micros=gpu[model_id],
        )
        for model_id, amount in amounts.items()
    )
    # Billed tool calls (e.g. web search) count in the shared scopes, whichever model serves the run.
    tool_micros = sum(prices.tool_cost_micros(tool_id, units) for tool_id, units in (tools or {}).items())
    return Envelope(
        amount_micros=total + tool_micros,
        lines=lines,
        upstream_micros=upstream_micros,
        tool_micros=tool_micros,
    )


def upstream_costs(
    prices: PriceBook,
    models: Sequence[ModelSpec],
    *,
    upstream_attempts: int,
    input_tokens: int,
    max_output_tokens: int,
) -> dict[str, int]:
    return {
        m.id: prices.worst_case_micros(
            m, input_tokens=input_tokens, max_output_tokens=max_output_tokens, attempts=upstream_attempts
        )
        for m in models
    }


def narrow_for_lower_cost(
    pool: Sequence[ModelSpec],
    attempted: Sequence[ModelSpec],
    denial: QuotaDeniedError,
    costs: Mapping[str, int],
    prices: PriceBook,
) -> list[ModelSpec]:
    """Candidates left after a `lower_cost_route` denial; empty when no cheaper eligible route exists.

    Only ever removes models from `pool`, which is the policy-eligible set for this execution path, so a
    lower-cost route can never cross a confidentiality boundary or leave the allowlist (LLD §12).
    A model or GPU-pool budget excludes its models; any other budget excludes every candidate at least
    as expensive as the costliest one just attempted.
    """
    if denial.scope_type == "model" and denial.scope_id:
        excluded = {denial.scope_id}
    elif denial.scope_type == "gpu_pool" and denial.scope_id:
        excluded = set(prices.models_in(denial.scope_id, (m.id for m in pool)))
    else:
        ceiling = max((costs[m.id] for m in attempted), default=0)
        excluded = {m.id for m in pool if costs[m.id] >= ceiling}
    remaining = [m for m in pool if m.id not in excluded]
    return remaining if len(remaining) < len(pool) else []
