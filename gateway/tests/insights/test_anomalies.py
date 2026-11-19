"""LLD §21.9 anomaly rules: pure rule logic, then alerts written through the FinOps alert store."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from aiva_gateway.config import REPO_ROOT
from aiva_gateway.core.enums import Provenance
from aiva_gateway.db.models import BudgetAlertRow
from aiva_gateway.reporting.aggregate import Aggregator
from aiva_gateway.reporting.anomalies import (
    AnomalyDetector,
    AnomalyRules,
    ExpensiveModelShareRule,
    HighOutputRatioRule,
    RepeatedRetriesRule,
    TokenSpikeRule,
    UnusualUserRule,
    Usage,
    evaluate_hour,
    expensive_share,
    high_output_ratio,
    load_insights_config,
    repeated_retries,
    token_spike,
    unusual_users,
)

HOUR = datetime(2026, 10, 14, 13, tzinfo=UTC)


def usage(tokens: int = 0, *, output: int = 0, api: int = 0, expensive: int = 0, **extra: int) -> Usage:
    return Usage(
        input_tokens=tokens,
        output_tokens=output,
        api_micros=api,
        expensive_micros=expensive,
        labels={"simulated"},
        **extra,
    )


def test_configured_rules_load() -> None:
    config = load_insights_config(REPO_ROOT / "config")
    assert config.anomaly_rules.token_spike.multiplier == 3.0
    assert config.anomaly_rules.expensive_model_share.expensive_models == ["claude-flagship"]


def test_token_spike_against_the_trailing_average() -> None:
    rule = TokenSpikeRule(baseline_hours=4, multiplier=3.0, min_tokens=1000)
    history = [usage(1000), usage(1000)]  # two busy hours, two idle: average 500 per hour
    spike = token_spike("app-a", HOUR, usage(1600), history, rule)
    assert spike is not None and spike.scope_id == "app-a" and spike.provenance is Provenance.SIMULATED
    assert "3.2 times" in spike.message
    assert token_spike("app-a", HOUR, usage(1400), history, rule) is None  # 2.8x
    assert token_spike("app-a", HOUR, usage(900), [usage(10)], rule) is None  # below min_tokens
    assert token_spike("app-a", HOUR, usage(5000), [], rule) is None  # no baseline to compare with


def test_expensive_model_share_change() -> None:
    rule = ExpensiveModelShareRule(baseline_hours=24, min_increase=0.25, min_spend_usd="0.01")
    history = [usage(api=100_000, expensive=10_000)]  # 10%
    found = expensive_share("app-a", HOUR, usage(api=100_000, expensive=60_000), history, rule)
    assert found is not None and "rose to 60%" in found.message and found.amount_micros == 60_000
    assert expensive_share("app-a", HOUR, usage(api=100_000, expensive=30_000), history, rule) is None
    assert expensive_share("app-a", HOUR, usage(api=5_000, expensive=5_000), history, rule) is None  # < $0.01


def test_retries_and_output_ratio() -> None:
    retries = RepeatedRetriesRule(min_retries=3, min_retry_rate=0.2)
    assert repeated_retries("app-a", HOUR, usage(retries=3, attempts=10), retries) is not None
    assert repeated_retries("app-a", HOUR, usage(retries=3, attempts=20), retries) is None
    assert repeated_retries("app-a", HOUR, usage(retries=2, attempts=2), retries) is None
    ratio = HighOutputRatioRule(max_ratio=4.0, min_requests=2, min_output_tokens=100)
    assert high_output_ratio("app-a", HOUR, usage(100, output=500, requests=2), ratio) is not None
    assert high_output_ratio("app-a", HOUR, usage(200, output=500, requests=2), ratio) is None
    assert high_output_ratio("app-a", HOUR, usage(10, output=500, requests=1), ratio) is None


def test_unusual_user_against_peer_median() -> None:
    rule = UnusualUserRule(multiplier=5.0, min_tokens=1000, min_peers=3)
    users = {"usr_a": usage(6000), "usr_b": usage(1000), "usr_c": usage(1200), "usr_d": usage(900)}
    found = unusual_users("app-a", HOUR, users, rule)
    assert [f.scope_id for f in found] == ["usr_a"]
    assert found[0].scope_type == "user" and "usr_a" in found[0].message
    assert (
        unusual_users("app-a", HOUR, {"usr_a": usage(6000), "usr_b": usage(10)}, rule) == []
    )  # too few peers


def test_the_same_user_in_two_applications_gets_two_alerts() -> None:
    rule = UnusualUserRule(multiplier=5.0, min_tokens=1000, min_peers=3)
    users = {"usr_a": usage(6000), "usr_b": usage(1000), "usr_c": usage(1200), "usr_d": usage(900)}
    [in_a] = unusual_users("app-a", HOUR, users, rule)
    [in_b] = unusual_users("app-b", HOUR, users, rule)
    assert (in_a.scope_id, in_b.scope_id) == ("usr_a", "usr_a")
    assert in_a.alert_id("gct") != in_b.alert_id("gct")
    assert (in_a.draft("gct").row().application_id, in_b.draft("gct").row().application_id) == (
        "app-a",
        "app-b",
    )


def test_findings_have_one_alert_id_per_rule_scope_and_hour() -> None:
    rules = AnomalyRules(repeated_retries=RepeatedRetriesRule(min_retries=1, min_retry_rate=0.0))
    apps = {(HOUR, "app-a"): usage(retries=1, attempts=2), (HOUR - timedelta(hours=1), "app-a"): usage()}
    first = evaluate_hour(HOUR, apps, {}, rules)
    again = evaluate_hour(HOUR, apps, {}, rules)
    assert [f.alert_id("gct") for f in first] == [f.alert_id("gct") for f in again]
    assert len({f.alert_id("gct") for f in first}) == len(first) == 1
    assert len(first[0].alert_id("gct")) <= 40 and first[0].alert_id("gct") != first[0].alert_id("other")
    later = evaluate_hour(
        HOUR + timedelta(hours=1), {(HOUR + timedelta(hours=1), "app-a"): apps[(HOUR, "app-a")]}, {}, rules
    )
    assert later[0].alert_id("gct") != first[0].alert_id("gct")


async def test_detector_writes_idempotent_alerts_visible_by_scope(
    traffic, services, make_client, user_token
) -> None:
    rules = AnomalyRules(repeated_retries=RepeatedRetriesRule(min_retries=1, min_retry_rate=0.01))
    detector = AnomalyDetector(services.sessionmaker, rules)
    hours = [traffic.start, traffic.start + timedelta(hours=1)]  # the traffic may straddle an hour
    raised = await detector.evaluate("gct", hours)
    assert raised >= 1

    async def alert_ids() -> set[str]:
        async with services.sessionmaker() as session:
            return set(
                await session.scalars(
                    select(BudgetAlertRow.alert_id).where(BudgetAlertRow.tenant_id == "gct")
                )
            )

    before = await alert_ids()
    assert await detector.evaluate("gct", hours) == 0  # same rule, scope and hour: no duplicate
    # Re-running the aggregation for the same window neither duplicates nor loses an alert.
    aggregator = Aggregator(services.sessionmaker, services.catalog, services.prices, anomalies=detector)
    result = await aggregator.refresh(
        "gct", triggered_by="test", start=hours[0], end=hours[-1] + timedelta(hours=1)
    )
    assert result.status == "completed" and result.alerts_raised == 0
    assert await alert_ids() == before
    async with services.sessionmaker() as session:
        alerts = list(
            await session.scalars(
                select(BudgetAlertRow).where(
                    BudgetAlertRow.kind == "repeated_retries",
                    BudgetAlertRow.scope_id == "gct-coding-assistant",
                )
            )
        )
    assert alerts and alerts[0].provenance in ("simulated", "synthetic")
    assert alerts[0].period == "hour" and "gct-coding-assistant" in alerts[0].message
    async with make_client(user_token("u-finance")) as client:
        finance = (await client.get("/v1/admin/budgets/alerts")).json()["alerts"]
    async with make_client(user_token("u-care-manager")) as client:
        manager = (await client.get("/v1/admin/budgets/alerts")).json()["alerts"]
    assert any(a["kind"] == "repeated_retries" and a["scope_id"] == "gct-coding-assistant" for a in finance)
    assert not any(a["scope_id"] == "gct-coding-assistant" for a in manager)
