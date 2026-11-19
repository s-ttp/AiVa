"""Public demo runs in Insights (ADR-0019). A visitor's live run is metered as measured and a replayed or
simulated one never is; both aggregate with everything else, and the trace views never label synthetic
traffic better than `synthetic`. Report access without the user dimension is covered by
integration/test_public_demo.py; here every report also refuses a user filter, and a trace hides its
principal."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.insights.conftest import refresh_window

# Fixtures from the public-demo suites: session rate limits cleared per test, and a visitor's run identity.
from tests.integration.test_public_demo import clear_limits, start_session  # noqa: F401
from tests.integration.test_public_demo_budget import live, run, sovereign, visitor  # noqa: F401

APP = "gct-support-chatbot"  # the application the budget tests' visitor runs through
HOUR = timedelta(hours=1)


def _today() -> dict[str, str]:
    today = datetime.now(UTC).date().isoformat()
    return {"from": today, "to": today}


async def _app_usage(client: Any) -> dict[str, Any]:
    params = {**_today(), "group_by": "application", "application_id": APP}
    [row] = (await client.get("/v1/reports/consumption", params=params)).json()["rows"]
    usage: dict[str, Any] = row["usage"]
    return usage


@pytest.mark.usefixtures("visitor")
async def test_a_visitors_live_and_replayed_runs_aggregate_with_their_own_provenance(
    app_client, services, make_client, user_token, traffic
) -> None:
    hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    window = (hour - HOUR, hour + 2 * HOUR)  # both runs, even across an hour boundary
    await refresh_window(services, *window)
    async with make_client(user_token("u-finance")) as client:
        before = await _app_usage(client)

    live_run = await run(app_client, live(services), sovereign(live=True))
    replayed = await run(app_client, live(services), sovereign())  # a visitor's default: replay
    assert live_run["status"] == replayed["status"] == "completed"
    assert live_run["usage"]["provenance"] == "measured"
    assert replayed["usage"]["provenance"] in ("replayed", "simulated")
    await refresh_window(services, *window)

    async with make_client(user_token("u-finance")) as client:
        after = await _app_usage(client)
        live_trace = (await client.get(f"/v1/reports/runs/{live_run['trace_id']}")).json()
        replay_trace = (await client.get(f"/v1/reports/runs/{replayed['trace_id']}")).json()

    def delta(kind: str, part: str) -> int:
        return int(after["tokens"][kind][part]) - int(before["tokens"][kind][part])

    assert after["request_count"] - before["request_count"] == 2
    for kind, field in (
        ("input_tokens", "upstream_submitted_input_tokens"),
        ("output_tokens", "upstream_output_tokens"),
    ):
        assert delta(kind, "measured") == live_run["usage"][field]
        assert delta(kind, "replayed") + delta(kind, "simulated") == replayed["usage"][field]
    assert delta("input_tokens", "measured") > 0
    # Attempts keep the evidence of their own call; the run's label is capped for synthetic traffic.
    assert [a["provenance"] for a in live_trace["attempts"]] == ["measured"]
    assert {a["provenance"] for a in replay_trace["attempts"]} <= {"replayed", "simulated"}
    assert live_trace["run"]["data_designation"] == "synthetic"
    assert live_trace["run"]["provenance"] == "synthetic"
    assert replay_trace["run"]["provenance"] in ("replayed", "simulated")


@pytest.mark.parametrize(
    ("path", "group_by"),
    [
        ("/v1/reports/overview", None),
        ("/v1/reports/consumption", "application"),
        ("/v1/reports/classification", "task_category"),
        ("/v1/reports/optimization", "model"),
        ("/v1/reports/governance", "application"),
        ("/v1/reports/runs", None),
        ("/v1/reports/quality", None),
    ],
)
async def test_every_report_refuses_a_visitor_a_user_filter(
    make_client, path: str, group_by: str | None
) -> None:
    token = await start_session(make_client)
    params = {**_today(), "user": "usr_0000000000000000", **({"group_by": group_by} if group_by else {})}
    async with make_client(token) as client:
        refused = await client.get(path, params=params)
    assert refused.status_code == 403, refused.text


async def test_a_visitor_sees_a_trace_without_its_principal(make_client, traffic) -> None:
    token = await start_session(make_client)
    async with make_client(token) as client:
        runs = (await client.get("/v1/reports/runs", params={**_today(), "limit": 5})).json()["runs"]
        trace = (await client.get(f"/v1/reports/runs/{runs[0]['trace_id']}")).json()
    assert trace["run"]["principal"] == "hidden"
    assert "usr_" not in str(trace)
