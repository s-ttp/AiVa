"""FinOps anomaly rules (LLD §21.9; ADR-0015), configured in `config/insights.yaml`.

Rules read the hourly aggregates after each refresh: per-application token spikes, expensive-model share
changes, repeated retries and high output-token ratios, and unusual per-user consumption. Each finding is
written to the FinOps alert store (`budget_alert`) with a deterministic ID per rule, scope and UTC hour, so a
re-run never duplicates an alert. Alerts inform; they never replace the preflight reservation or an
enforced cap. Messages name pseudonyms only, never identities. An alert's provenance is the least
trustworthy label of the usage behind it (a simulated spike is labelled simulated).
"""

import hashlib
import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aiva_gateway.core.enums import Provenance
from aiva_gateway.core.money import usd_to_micros
from aiva_gateway.db.models import BudgetAlertRow, ReportAggregateHourlyRow
from aiva_gateway.finops.alerts import AlertDraft, record_alerts, usd
from aiva_gateway.finops.scopes import Bucket
from aiva_gateway.reporting.lines import ATTEMPT, RUN, least_trusted_label

TOKEN_SPIKE = "token_spike"  # noqa: S105 - an alert kind, not a secret
EXPENSIVE_MODEL_SHARE = "expensive_model_share"
REPEATED_RETRIES = "repeated_retries"
HIGH_OUTPUT_RATIO = "high_output_ratio"
UNUSUAL_USER_CONSUMPTION = "unusual_user_consumption"
KINDS = (TOKEN_SPIKE, EXPENSIVE_MODEL_SHARE, REPEATED_RETRIES, HIGH_OUTPUT_RATIO, UNUSUAL_USER_CONSUMPTION)
HOUR = timedelta(hours=1)


class _Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = True


class TokenSpikeRule(_Rule):
    baseline_hours: int = Field(default=24, ge=1, le=24 * 14)
    multiplier: float = Field(default=3.0, gt=1)
    min_tokens: int = Field(default=20_000, ge=0)


class ExpensiveModelShareRule(_Rule):
    expensive_models: list[str] = Field(default_factory=lambda: ["claude-flagship"])
    baseline_hours: int = Field(default=24, ge=1, le=24 * 14)
    min_increase: float = Field(default=0.25, gt=0, le=1)
    min_spend_usd: Decimal = Decimal("0.05")


class RepeatedRetriesRule(_Rule):
    min_retries: int = Field(default=3, ge=1)
    min_retry_rate: float = Field(default=0.2, ge=0, le=1)


class HighOutputRatioRule(_Rule):
    max_ratio: float = Field(default=4.0, gt=0)
    min_requests: int = Field(default=5, ge=1)
    min_output_tokens: int = Field(default=5_000, ge=0)


class UnusualUserRule(_Rule):
    multiplier: float = Field(default=5.0, gt=1)
    min_tokens: int = Field(default=20_000, ge=0)
    min_peers: int = Field(default=3, ge=1)


class AnomalyRules(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    token_spike: TokenSpikeRule = TokenSpikeRule()
    expensive_model_share: ExpensiveModelShareRule = ExpensiveModelShareRule()
    repeated_retries: RepeatedRetriesRule = RepeatedRetriesRule()
    high_output_ratio: HighOutputRatioRule = HighOutputRatioRule()
    unusual_user_consumption: UnusualUserRule = UnusualUserRule()

    @property
    def lookback(self) -> timedelta:
        return HOUR * max(self.token_spike.baseline_hours, self.expensive_model_share.baseline_hours)


class InsightsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str
    anomaly_rules: AnomalyRules


def load_insights_config(config_dir: Path) -> InsightsConfig:
    data = yaml.safe_load((config_dir / "insights.yaml").read_text(encoding="utf-8"))
    return InsightsConfig.model_validate(data)


# --- Per-scope hourly usage ---------------------------------------------------------------------------


@dataclass
class Usage:
    """Usage of one scope (an application or a user) in one hour."""

    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    attempts: int = 0
    retries: int = 0
    api_micros: int = 0
    expensive_micros: int = 0
    labels: set[str] = field(default_factory=set)

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, row: Any, expensive: frozenset[str]) -> None:
        if row.line_kind == RUN:
            self.requests += int(row.requests or 0)
        if row.line_kind == ATTEMPT:
            self.input_tokens += int(row.input_tokens or 0)
            self.output_tokens += int(row.output_tokens or 0)
            self.attempts += int(row.attempts or 0)
            self.retries += int(row.retries or 0)
            self.labels.add(row.provenance)
        if row.line_kind != RUN:
            self.api_micros += int(row.api_micros or 0)
            if row.model_id in expensive:
                self.expensive_micros += int(row.api_micros or 0)
        if row.data_designation == "synthetic":
            self.labels.add(Provenance.SYNTHETIC.value)


@dataclass(frozen=True)
class Finding:
    kind: str
    scope_type: str
    scope_id: str
    hour: datetime
    message: str
    provenance: Provenance
    spent_micros: int
    amount_micros: int | None = None
    application_id: str | None = None  # a user's anomaly within one application (row-level scope)

    def alert_id(self, tenant_id: str) -> str:
        """One alert per tenant, rule, scope, application and hour: `alr_` + 32 hex characters (36 <= 40).
        A user's anomaly is per application, so the same user in two applications gets two alerts.
        Scope-only findings keep their earlier IDs (no application in the material)."""
        material = f"{tenant_id}|{self.kind}|{self.scope_type}|{self.scope_id}|{self.hour.isoformat()}"
        if self.application_id is not None:
            material += f"|{self.application_id}"
        return "alr_" + hashlib.sha256(material.encode()).hexdigest()[:32]

    def draft(self, tenant_id: str) -> AlertDraft:
        return AlertDraft(
            tenant_id=tenant_id,
            kind=self.kind,
            message=self.message,
            provenance=self.provenance,
            bucket=Bucket(self.scope_type, self.scope_id, "hour", self.hour.strftime("%Y-%m-%dT%H")),
            spent_micros=self.spent_micros,
            amount_micros=self.amount_micros,
            alert_id=self.alert_id(tenant_id),
            application_id=self.application_id,
        )


def _label(*usages: Usage) -> Provenance:
    labels: set[str] = set().union(*(u.labels for u in usages))
    return least_trusted_label(labels) or Provenance.MEASURED


def _hour(hour: datetime) -> str:
    return hour.strftime("%Y-%m-%d %H:00 UTC")


def _n(value: int) -> str:
    return f"{value:,}"


def token_spike(
    app: str, hour: datetime, current: Usage, history: Sequence[Usage], rule: TokenSpikeRule
) -> Finding | None:
    """Tokens this hour at least `multiplier` times the trailing hourly average (idle hours count 0)."""
    if not rule.enabled or current.tokens < rule.min_tokens:
        return None
    average = sum(u.tokens for u in history) / rule.baseline_hours
    if average <= 0 or current.tokens < rule.multiplier * average:
        return None
    return Finding(
        kind=TOKEN_SPIKE,
        scope_type="application",
        scope_id=app,
        hour=hour,
        message=f"Application {app} used {_n(current.tokens)} tokens in the hour from {_hour(hour)}, "
        f"{current.tokens / average:.1f} times its trailing {rule.baseline_hours}-hour average "
        f"of {_n(round(average))}.",
        provenance=_label(current),
        spent_micros=current.api_micros,
    )


def expensive_share(
    app: str, hour: datetime, current: Usage, history: Sequence[Usage], rule: ExpensiveModelShareRule
) -> Finding | None:
    """The API spend share of expensive models rose by at least `min_increase` over the trailing share."""
    if (
        not rule.enabled
        or current.api_micros <= 0
        or current.expensive_micros < usd_to_micros(rule.min_spend_usd)
    ):
        return None
    share = current.expensive_micros / current.api_micros
    past_total = sum(u.api_micros for u in history)
    past = sum(u.expensive_micros for u in history) / past_total if past_total else 0.0
    if share - past < rule.min_increase:
        return None
    models = ", ".join(rule.expensive_models)
    return Finding(
        kind=EXPENSIVE_MODEL_SHARE,
        scope_type="application",
        scope_id=app,
        hour=hour,
        message=f"Spend share on expensive models ({models}) for application {app} rose to {share:.0%} "
        f"in the hour from {_hour(hour)}, from {past:.0%} over the previous {rule.baseline_hours} hours "
        f"({usd(current.expensive_micros)} of {usd(current.api_micros)}).",
        provenance=_label(current),
        spent_micros=current.api_micros,
        amount_micros=current.expensive_micros,
    )


def repeated_retries(app: str, hour: datetime, current: Usage, rule: RepeatedRetriesRule) -> Finding | None:
    if not rule.enabled or current.retries < rule.min_retries or not current.attempts:
        return None
    rate = current.retries / current.attempts
    if rate < rule.min_retry_rate:
        return None
    return Finding(
        kind=REPEATED_RETRIES,
        scope_type="application",
        scope_id=app,
        hour=hour,
        message=f"Application {app} retried {current.retries} of {current.attempts} provider calls "
        f"({rate:.0%}) in the hour from {_hour(hour)}.",
        provenance=_label(current),
        spent_micros=current.api_micros,
    )


def high_output_ratio(app: str, hour: datetime, current: Usage, rule: HighOutputRatioRule) -> Finding | None:
    if (
        not rule.enabled
        or current.requests < rule.min_requests
        or current.output_tokens < rule.min_output_tokens
        or not current.input_tokens
    ):
        return None
    ratio = current.output_tokens / current.input_tokens
    if ratio < rule.max_ratio:
        return None
    return Finding(
        kind=HIGH_OUTPUT_RATIO,
        scope_type="application",
        scope_id=app,
        hour=hour,
        message=f"Application {app} produced {ratio:.1f} output tokens per input token in the hour from "
        f"{_hour(hour)} (threshold {rule.max_ratio:g}; {_n(current.output_tokens)} output tokens).",
        provenance=_label(current),
        spent_micros=current.api_micros,
    )


def unusual_users(app: str, hour: datetime, users: dict[str, Usage], rule: UnusualUserRule) -> list[Finding]:
    """Users whose tokens are at least `multiplier` times the median of the application's other users."""
    if not rule.enabled:
        return []
    findings = []
    active = {user: usage for user, usage in users.items() if usage.tokens > 0}
    for user, usage in sorted(active.items()):
        peers = [u.tokens for other, u in active.items() if other != user]
        if usage.tokens < rule.min_tokens or len(peers) < rule.min_peers:
            continue
        median = statistics.median(peers)
        if median <= 0 or usage.tokens < rule.multiplier * median:
            continue
        findings.append(
            Finding(
                kind=UNUSUAL_USER_CONSUMPTION,
                scope_type="user",
                scope_id=user,
                hour=hour,
                message=f"User {user} consumed {_n(usage.tokens)} tokens in application {app} in the hour "
                f"from {_hour(hour)}, {usage.tokens / median:.1f} times the median of {len(peers)} "
                "other users.",
                provenance=_label(usage),
                spent_micros=usage.api_micros,
                application_id=app,
            )
        )
    return findings


def evaluate_hour(
    hour: datetime,
    apps: dict[tuple[datetime, str], Usage],
    users: dict[tuple[datetime, str], dict[str, Usage]],
    rules: AnomalyRules,
) -> list[Finding]:
    findings: list[Finding] = []
    for (bucket, app), current in sorted(apps.items()):
        if bucket != hour:
            continue

        def history(hours: int, app: str = app) -> list[Usage]:
            return [
                apps[(hour - HOUR * i, app)] for i in range(1, hours + 1) if (hour - HOUR * i, app) in apps
            ]

        for finding in (
            token_spike(app, hour, current, history(rules.token_spike.baseline_hours), rules.token_spike),
            expensive_share(
                app,
                hour,
                current,
                history(rules.expensive_model_share.baseline_hours),
                rules.expensive_model_share,
            ),
            repeated_retries(app, hour, current, rules.repeated_retries),
            high_output_ratio(app, hour, current, rules.high_output_ratio),
        ):
            if finding is not None:
                findings.append(finding)
        findings.extend(unusual_users(app, hour, users.get((hour, app), {}), rules.unusual_user_consumption))
    return findings


class AnomalyDetector:
    """Evaluates the rules for refreshed hours and records new alerts (the Aggregator's anomaly hook)."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], rules: AnomalyRules) -> None:
        self.sessionmaker = sessionmaker
        self.rules = rules
        self.expensive = frozenset(rules.expensive_model_share.expensive_models)

    async def __call__(self, tenant_id: str, hours: Sequence[datetime]) -> int:
        return await self.evaluate(tenant_id, hours)

    async def evaluate(self, tenant_id: str, hours: Sequence[datetime]) -> int:
        if not hours:
            return 0
        async with self.sessionmaker() as session:
            apps, users = await self._usage(session, tenant_id, min(hours) - self.rules.lookback, max(hours))
        findings = [f for hour in sorted(set(hours)) for f in evaluate_hour(hour, apps, users, self.rules)]
        return await self.record(tenant_id, findings)

    async def _usage(
        self, session: AsyncSession, tenant_id: str, start: datetime, end: datetime
    ) -> tuple[dict[tuple[datetime, str], Usage], dict[tuple[datetime, str], dict[str, Usage]]]:
        hourly = ReportAggregateHourlyRow
        dims = (
            hourly.bucket_start,
            hourly.application_id,
            hourly.principal,
            hourly.line_kind,
            hourly.model_id,
            hourly.provenance,
            hourly.data_designation,
        )
        rows = await session.execute(
            select(
                *dims,
                func.sum(hourly.input_tokens).label("input_tokens"),
                func.sum(hourly.output_tokens).label("output_tokens"),
                func.sum(hourly.request_count).label("requests"),
                func.sum(hourly.attempt_count).label("attempts"),
                func.sum(hourly.retry_count).label("retries"),
                func.sum(hourly.api_micros).label("api_micros"),
            )
            .where(hourly.tenant_id == tenant_id, hourly.bucket_start >= start, hourly.bucket_start <= end)
            .group_by(*dims)
        )
        apps: dict[tuple[datetime, str], Usage] = defaultdict(Usage)
        users: dict[tuple[datetime, str], dict[str, Usage]] = defaultdict(lambda: defaultdict(Usage))
        for row in rows:
            key = (row.bucket_start, row.application_id)
            apps[key].add(row, self.expensive)
            if str(row.principal).startswith("usr_"):
                users[key][row.principal].add(row, self.expensive)
        return apps, users

    async def record(self, tenant_id: str, findings: Iterable[Finding]) -> int:
        """Write new alerts through the FinOps alert store; an alert already recorded is skipped."""
        drafts = [finding.draft(tenant_id) for finding in findings]
        if not drafts:
            return 0
        ids = [draft.alert_id for draft in drafts if draft.alert_id]
        async with self.sessionmaker() as session:
            existing = set(
                await session.scalars(select(BudgetAlertRow.alert_id).where(BudgetAlertRow.alert_id.in_(ids)))
            )
        await record_alerts(self.sessionmaker, drafts, on_conflict="ignore")
        return len(set(ids) - existing)
