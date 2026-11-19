"""Sum aggregate lines into report figures (ADR-0006, ADR-0015). Pure functions; integer micro-USD.

Lines arrive grouped by line kind, stage, provenance and a few run dimensions, so every figure can be split
by where it came from. Actual (provider-measured), estimated, simulated and replayed quantities stay apart;
avoided usage is modelled and reported on its own; a figure built from synthetic traffic is never labelled
better than `synthetic`.
"""

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, get_args

from aiva_gateway.contracts.reports import (
    Amount,
    AvoidedUsage,
    BaselineComparison,
    CostKpi,
    CostVariance,
    FlowRoute,
    FlowRouteCount,
    FlowStop,
    FlowStopCount,
    GovernanceKpi,
    OutcomeCounts,
    PackingSummary,
    ProvenanceSplit,
    Ratio,
    ReportUsage,
    SavingsStep,
    SavingsWaterfall,
    Spend,
    TokensKpi,
    TokenUsage,
    TrafficFlow,
)
from aiva_gateway.core.enums import ChargeType, Classification, ExecutionPath, Outcome, Provenance
from aiva_gateway.reporting.dimensions import NONE
from aiva_gateway.reporting.lines import (
    ADJUSTMENT,
    ATTEMPT,
    CHARGE,
    FLOAT_MEASURES,
    MEASUREMENT_STAGES,
    MEASURES,
    QUALITY_STAGES,
    RUN,
)
from aiva_gateway.reporting.lines import least_trusted_label as least_trusted

_ESTIMATED = Provenance.ESTIMATED.value
_SYNTHETIC = Provenance.SYNTHETIC.value
_STAGES = ("upstream", "cim", *MEASUREMENT_STAGES)


def ratio(numerator: int, denominator: int) -> Ratio:
    return Ratio(
        numerator=numerator,
        denominator=denominator,
        value=numerator / denominator if denominator else None,
    )


_STOPS: tuple[str, ...] = get_args(FlowStop)
_ROUTES: tuple[str, ...] = get_args(FlowRoute)
_PATH_ROUTES = {
    ExecutionPath.CACHE_RETURN.value: "cache",
    ExecutionPath.SOVEREIGN_DIRECT.value: "sovereign",
    ExecutionPath.LOCAL_FAST.value: "local_fast",
}
_UNPACKED = ("full", NONE)  # context strategies that sent no CIM package


def flow_bucket(outcome: str, execution_path: str, context_strategy: str) -> str:
    """The traffic-flow bucket of a run: why it took no route, or the route it took (TrafficFlow)."""
    if execution_path in _PATH_ROUTES:
        return _PATH_ROUTES[execution_path]
    if execution_path == ExecutionPath.CIM_ASSISTED.value:
        return "full_context" if context_strategy in _UNPACKED else "cim_packaged"
    stopped = (Outcome.POLICY_DENIED.value, Outcome.APPROVAL_REQUIRED.value, Outcome.QUOTA_DENIED.value)
    return outcome if outcome in stopped else "not_routed"


def _packed(row: Mapping[str, Any]) -> bool:
    """Whether a line belongs to a run that sent the CIM's package."""
    keys = ("outcome", "execution_path", "context_strategy")
    return all(key in row for key in keys) and flow_bucket(*(str(row[key]) for key in keys)) == "cim_packaged"


def _confidential(sensitivity: str) -> bool:
    try:
        return Classification(sensitivity).requires_sovereign_path
    except ValueError:
        return False


@dataclass
class Totals:
    """Running sums of aggregate lines, with the splits every report needs."""

    measures: dict[str, float] = field(default_factory=lambda: dict.fromkeys(MEASURES, 0))
    tokens_in: Counter[str] = field(default_factory=Counter)
    tokens_out: Counter[str] = field(default_factory=Counter)
    api: Counter[str] = field(default_factory=Counter)
    stage_in: Counter[str] = field(default_factory=Counter)
    stage_out: Counter[str] = field(default_factory=Counter)
    stage_retry_in: Counter[str] = field(default_factory=Counter)
    stage_retry_out: Counter[str] = field(default_factory=Counter)
    stage_api: Counter[str] = field(default_factory=Counter)
    stage_retry_api: Counter[str] = field(default_factory=Counter)
    stage_gpu: Counter[str] = field(default_factory=Counter)
    stage_labels: dict[str, set[str]] = field(default_factory=dict)
    adjustment_api: int = 0
    charge_api: int = 0
    tool_call_api: int = 0
    tool_labels: set[str] = field(default_factory=set)
    outcomes: Counter[str] = field(default_factory=Counter)
    paths: Counter[str] = field(default_factory=Counter)
    cim_states: Counter[str] = field(default_factory=Counter)
    statuses: Counter[str] = field(default_factory=Counter)
    deny_rules: Counter[str] = field(default_factory=Counter)
    flow: Counter[str] = field(default_factory=Counter)
    packed_in: Counter[str] = field(default_factory=Counter)
    confidential_requests: int = 0
    confidential_sovereign: int = 0
    served_by: dict[str, Counter[str]] = field(default_factory=dict)
    run_labels: set[str] = field(default_factory=set)
    usage_labels: set[str] = field(default_factory=set)
    gpu_labels: set[str] = field(default_factory=set)
    avoided_labels: set[str] = field(default_factory=set)
    designations: set[str] = field(default_factory=set)

    def add(self, row: Mapping[str, Any]) -> None:
        values = {name: row[name] or 0 for name in MEASURES}
        for name, value in values.items():
            self.measures[name] += float(value) if name in FLOAT_MEASURES else int(value)
        kind, stage, label = row["line_kind"], row["stage"], row["provenance"]
        designation = row.get("data_designation")
        if designation:
            self.designations.add(str(designation))
        api, gpu = int(values["api_micros"]), int(values["gpu_estimated_micros"])
        if gpu:
            self.gpu_labels.add(label)
        if kind == RUN:
            requests = int(values["request_count"])
            self.run_labels.add(label)
            for key, counter in (
                ("outcome", self.outcomes),
                ("execution_path", self.paths),
                ("cim_state", self.cim_states),
                ("classification_status", self.statuses),
            ):
                if key in row:
                    counter[str(row[key])] += requests
            if {"outcome", "execution_path", "context_strategy"} <= row.keys():
                bucket = flow_bucket(
                    str(row["outcome"]), str(row["execution_path"]), str(row["context_strategy"])
                )
                self.flow[bucket] += requests
            if row.get("deny_rule_id"):
                self.deny_rules[str(row["deny_rule_id"])] += requests
            if "sensitivity" in row and _confidential(str(row["sensitivity"])):
                self.confidential_requests += requests
                if row.get("execution_path") == ExecutionPath.SOVEREIGN_DIRECT.value:
                    self.confidential_sovereign += requests
            if "model_id" in row and requests:
                suitability = self.served_by.setdefault(str(row["model_id"]), Counter())
                suitability["requests"] += requests
                suitability["checked"] += int(values["quality_checked_count"])
                suitability["passed"] += int(values["quality_passed_count"])
            if values["avoided_micros"] or values["avoided_call_count"]:
                self.avoided_labels.add(label)
            return
        self.usage_labels.add(label)
        self.api[label] += api
        if kind == ATTEMPT and stage == "upstream" and _packed(row):
            self.packed_in[label] += int(values["input_tokens"]) - int(values["retry_input_tokens"])
        if kind == ATTEMPT:
            self.tokens_in[label] += int(values["input_tokens"])
            self.tokens_out[label] += int(values["output_tokens"])
            self.stage_in[stage] += int(values["input_tokens"])
            self.stage_out[stage] += int(values["output_tokens"])
            self.stage_retry_in[stage] += int(values["retry_input_tokens"])
            self.stage_retry_out[stage] += int(values["retry_output_tokens"])
            self.stage_api[stage] += api
            self.stage_retry_api[stage] += int(values["retry_micros"])
            self.stage_gpu[stage] += gpu
            self.stage_labels.setdefault(stage, set()).add(label)
        elif kind == ADJUSTMENT:
            self.adjustment_api += api
        elif kind == CHARGE and stage == ChargeType.TOOL_CALL.value:
            self.tool_call_api += api
            self.tool_labels.add(label)
        elif kind == CHARGE:
            self.charge_api += api

    def extend(self, rows: Iterable[Mapping[str, Any]]) -> "Totals":
        for row in rows:
            self.add(row)
        return self

    # --- Labels ------------------------------------------------------------------------------------------

    def label(self, *labels: Iterable[str], floor: str | None = None) -> Provenance:
        """The least trustworthy of the given labels, capped at `synthetic` for generated traffic."""
        present: set[str] = set().union(*labels)
        if floor:
            present.add(floor)
        if _SYNTHETIC in self.designations:
            present.add(_SYNTHETIC)
        return least_trusted(present) or Provenance.MEASURED

    def m(self, name: str) -> int:
        return int(self.measures[name])

    # --- Figures -----------------------------------------------------------------------------------------

    @property
    def requests(self) -> int:
        return self.m("request_count")

    def split(self, counter: Counter[str]) -> ProvenanceSplit:
        parts = {p.value: int(counter.get(p.value, 0)) for p in Provenance}
        nonzero = {name for name, value in counter.items() if value}
        return ProvenanceSplit(**parts, total=sum(counter.values()), provenance=self.label(nonzero))

    def tokens(self) -> TokenUsage:
        first_in = {s: self.stage_in[s] - self.stage_retry_in[s] for s in _STAGES}
        first_out = {s: self.stage_out[s] - self.stage_retry_out[s] for s in _STAGES}
        retry_in, retry_out = sum(self.stage_retry_in.values()), sum(self.stage_retry_out.values())
        total_in, total_out = sum(self.stage_in.values()), sum(self.stage_out.values())
        return TokenUsage(
            input_tokens=self.split(self.tokens_in),
            output_tokens=self.split(self.tokens_out),
            upstream_input_tokens=first_in["upstream"],
            upstream_output_tokens=first_out["upstream"],
            cim_input_tokens=first_in["cim"],
            cim_output_tokens=first_out["cim"],
            retry_input_tokens=retry_in,
            retry_output_tokens=retry_out,
            baseline_input_tokens=first_in["baseline"],
            baseline_output_tokens=first_out["baseline"],
            quality_comparison_input_tokens=sum(first_in[s] for s in QUALITY_STAGES),
            quality_comparison_output_tokens=sum(first_out[s] for s in QUALITY_STAGES),
            other_input_tokens=total_in - sum(first_in.values()) - retry_in,
            other_output_tokens=total_out - sum(first_out.values()) - retry_out,
            cached_input_tokens=self.m("cached_input_tokens"),
            reasoning_tokens=self.m("reasoning_tokens"),
        )

    def first_call_api(self, stage: str) -> int:
        return self.stage_api[stage] - self.stage_retry_api[stage]

    def spend(self) -> Spend:
        retry = sum(self.stage_retry_api.values())
        named = (
            sum(self.first_call_api(s) for s in _STAGES) + retry + self.adjustment_api + self.tool_call_api
        )
        return Spend(
            api=self.split(self.api),
            upstream_micros=self.first_call_api("upstream"),
            cim_micros=self.first_call_api("cim"),
            retry_micros=retry,
            baseline_comparison_micros=self.first_call_api("baseline"),
            quality_comparison_micros=sum(self.first_call_api(s) for s in QUALITY_STAGES),
            adjustment_micros=self.adjustment_api,
            tool_call_micros=self.tool_call_api,
            other_micros=sum(self.api.values()) - named,
            gpu_estimated=Amount(
                value=self.m("gpu_estimated_micros"), provenance=self.label(self.gpu_labels, floor=_ESTIMATED)
            ),
        )

    def avoided(self) -> AvoidedUsage:
        return AvoidedUsage(
            calls=self.m("avoided_call_count"),
            tokens=self.m("avoided_tokens"),
            micros=self.m("avoided_micros"),
            provenance=self.label(self.avoided_labels, floor=_ESTIMATED),
        )

    def usage(self) -> ReportUsage:
        return ReportUsage(
            request_count=self.requests,
            provenance=self.label(),
            tokens=self.tokens(),
            spend=self.spend(),
            avoided=self.avoided(),
        )

    def outcome_counts(self) -> OutcomeCounts:
        known = OutcomeCounts.model_fields
        return OutcomeCounts(**{name: count for name, count in self.outcomes.items() if name in known})

    def traffic_flow(self) -> TrafficFlow:
        return TrafficFlow(
            requests=self.requests,
            stopped=[FlowStopCount(bucket=b, count=self.flow[b]) for b in _STOPS],
            routed=[FlowRouteCount(bucket=b, count=self.flow[b]) for b in _ROUTES],
            provenance=self.label(),
        )

    def packing(self) -> PackingSummary:
        return PackingSummary(
            packed_runs=self.flow["cim_packaged"],
            passages_retrieved=self.m("passages_retrieved"),
            passages_kept=self.m("passages_kept"),
            model_input_tokens=self.split(self.packed_in),
            provenance=self.label(),
        )

    # --- The Story mode's hero figures (the /live counters, lines.value_measures) ---------------------------

    def cost_kpi(self) -> CostKpi:
        aiva, direct = self.m("priced_api_micros"), self.m("priced_frontier_micros")
        spent = self.label(self.run_labels, self.tool_labels)
        modelled = self.label(self.run_labels, floor=_ESTIMATED)
        return CostKpi(
            priced_requests=self.m("priced_request_count"),
            aiva=Amount(value=aiva, provenance=spent),
            direct_frontier=Amount(value=direct, provenance=modelled),
            saved=ratio(direct - aiva, direct),
            hosting_excluded=self.spend().gpu_estimated,
            provenance=self.label(self.run_labels, self.tool_labels, floor=_ESTIMATED),
        )

    def tokens_kpi(self) -> TokensKpi:
        caching, context = self.m("cache_tokens_saved"), self.m("context_tokens_saved")
        return TokensKpi(
            base_tokens=self.m("optimizable_tokens"),
            caching_tokens=caching,
            context_tokens=context,
            fewer=ratio(caching + context, self.m("optimizable_tokens")),
            caching_provenance=self.label(self.avoided_labels, floor=_ESTIMATED),
            context_provenance=self.label(self.run_labels, floor=_ESTIMATED),
            provenance=self.label(self.avoided_labels, self.run_labels, floor=_ESTIMATED),
        )

    def governance_kpi(self) -> GovernanceKpi:
        return GovernanceKpi(
            requests=self.requests,
            stopped_by_policy=self.outcomes[Outcome.POLICY_DENIED.value]
            + self.outcomes[Outcome.APPROVAL_REQUIRED.value],
            sensitive_requests=self.confidential_requests,
            sensitive_on_sovereign=self.confidential_sovereign,
            provenance=self.label(),
        )

    def quality_comparison_cost(self) -> Amount:
        cost = self.spend().quality_comparison_micros
        return Amount(value=cost, provenance=self._calls_label(QUALITY_STAGES))

    def cost_variance(self) -> CostVariance:
        reserved, committed = self.m("reserved_micros"), self.m("committed_micros")
        return CostVariance(
            reserved_micros=reserved,
            committed_micros=committed,
            variance_micros=reserved - committed,
            provenance=self.label(self.run_labels, floor=_ESTIMATED),
        )

    def savings(self, frontier_model_id: str, pricing_snapshot: str) -> SavingsWaterfall:
        """Baseline -> cache -> context -> routing -> prompt cache -> overheads -> net (reporting.lines).

        The overheads add back every ledger amount the modelled steps leave out (CIM, retries, billed tool
        calls, corrections and other charges) and the local GPU estimate, so the net is the whole cost of
        serving. The opt-in baseline comparison and a presenter's answer-quality comparison (ADR-0023) are a
        measurement cost, not a cost of serving: they come after the net and never reduce the savings, and the
        net plus them is exactly all actual API spend plus the GPU estimate.
        """
        baseline = self.m("frontier_baseline_micros")
        cache, context = self.m("savings_cache_micros"), self.m("savings_context_micros")
        routing, prompt = self.m("savings_routing_micros"), self.m("savings_prompt_cache_micros")
        served = baseline - cache - context - routing - prompt
        cim = self.stage_api["cim"]
        retries = self.stage_api["upstream"] - served
        measurement = sum(self.stage_api[s] for s in MEASUREMENT_STAGES)
        adjustments = (
            self.adjustment_api
            + self.charge_api
            + sum(v for s, v in self.stage_api.items() if s not in ("upstream", "cim", *MEASUREMENT_STAGES))
        )
        tools = self.tool_call_api
        gpu = self.m("gpu_estimated_micros")
        net = served + cim + retries + tools + adjustments + gpu
        modelled = self.label(self.run_labels, floor=_ESTIMATED)
        observed = self.label(self.usage_labels)
        steps = [
            ("frontier_baseline", baseline, modelled),
            ("cache", -cache, modelled),
            ("context", -context, modelled),
            ("routing", -routing, modelled),
            ("prompt_cache", -prompt, modelled),
            ("cim_overhead", cim, self.label(self.stage_labels.get("cim", set()))),
            ("retries", retries, observed),
            ("tool_calls", tools, self.label(self.tool_labels or self.run_labels)),
            ("adjustments", adjustments, observed),
            ("local_gpu", gpu, self.label(self.gpu_labels, floor=_ESTIMATED)),
            ("net", net, self.label(self.usage_labels, self.run_labels, floor=_ESTIMATED)),
        ]
        return SavingsWaterfall(
            frontier_model_id=frontier_model_id,
            pricing_snapshot=pricing_snapshot,
            steps=[
                SavingsStep(step=name, amount_micros=value, provenance=label) for name, value, label in steps
            ],
            net_savings_micros=baseline - net,
            measurement_cost=Amount(value=measurement, provenance=self._measurement_label()),
            provenance=self.label(self.usage_labels, self.run_labels, floor=_ESTIMATED),
        )

    def _baseline_label(self) -> Provenance:
        """The baseline comparison calls' label. With no such call, the zero is a count the gateway made of
        these runs, so it takes their label: never `measured` for simulated or synthetic traffic."""
        return self._calls_label(("baseline",))

    def _measurement_label(self) -> Provenance:
        """The label of every call that measured a run (baseline and answer-quality comparisons)."""
        return self._calls_label(MEASUREMENT_STAGES)

    def _calls_label(self, stages: Iterable[str]) -> Provenance:
        labels: set[str] = set().union(*(self.stage_labels.get(s, set()) for s in stages))
        return self.label(labels or self.run_labels)

    def baseline(self, frontier_model_id: str, frontier_model_name: str) -> BaselineComparison:
        runs, passed = self.m("baseline_runs"), self.m("baseline_passed_runs")
        baseline, actual = self.m("baseline_micros"), self.m("baseline_actual_micros")
        measured = self._baseline_label()
        observed = self.label(self.usage_labels)
        compared = self.label(self.stage_labels.get("baseline", set()), self.usage_labels)
        return BaselineComparison(
            frontier_model_id=frontier_model_id,
            frontier_model_name=frontier_model_name,
            runs=runs,
            baseline=Amount(value=baseline, provenance=measured),
            actual=Amount(value=actual, provenance=observed),
            savings=Amount(value=baseline - actual, provenance=compared),
            quality_passed_runs=passed,
            quality_failed_runs=runs - passed,
            quality_gated_savings=Amount(value=self.m("baseline_passed_savings_micros"), provenance=compared),
        )

    def api_amount(self) -> Amount:
        return Amount(value=sum(self.api.values()), provenance=self.label(self.usage_labels))
