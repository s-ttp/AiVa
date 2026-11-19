"""Authentication, authorisation, run access scoping and the other routes."""

import pytest

from tests.conftest import chat_body, console


async def test_health(make_client) -> None:
    async with make_client() as client:
        assert (await client.get("/health/live")).json() == {"status": "ok"}
        ready = await client.get("/health/ready")
    assert ready.status_code == 200, ready.json()
    assert ready.json()["checks"]["policy_bundle"] != "missing"


async def test_missing_and_invalid_credentials(make_client) -> None:
    async with make_client() as client:
        missing = await client.post("/v1/ai/chat", json=chat_body("hi"))
    async with make_client("aiva_deadbeef.not-a-secret") as client:
        invalid = await client.post("/v1/ai/chat", json=chat_body("hi"))
    async with make_client("not.a.jwt") as client:
        garbage = await client.post("/v1/ai/chat", json=chat_body("hi"))
    for response in (missing, invalid, garbage):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


async def test_validation_errors_use_envelope(app_client) -> None:
    async with app_client("gct-support-chatbot") as client:
        response = await client.post("/v1/ai/chat", json={"messages": []})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


async def test_application_cannot_use_test_profile(app_client) -> None:
    async with app_client("gct-support-chatbot") as client:
        response = await client.post("/v1/ai/chat", json=chat_body("hi", **console("gct-support-chatbot")))
    assert response.status_code == 403


async def test_non_tester_users_cannot_run_chat(make_client, user_token) -> None:
    async with make_client(user_token("u-finance")) as client:
        response = await client.post("/v1/ai/chat", json=chat_body("hi", **console("gct-support-chatbot")))
    assert response.status_code == 403


async def test_run_access_is_scoped(app_client, tester_client, make_client, user_token) -> None:
    async with tester_client() as client:
        run_id = (
            await client.post("/v1/ai/chat", json=chat_body("Roaming?", **console("gct-support-chatbot")))
        ).json()["run_id"]
        own = await client.get(f"/v1/runs/{run_id}")
        trace = await client.get(f"/v1/runs/{run_id}/trace")
    assert own.status_code == 200 and trace.status_code == 200
    assert len(trace.json()["spans"]) >= 13
    async with make_client(user_token("u-auditor")) as client:
        assert (await client.get(f"/v1/runs/{run_id}")).status_code == 200
    async with make_client(user_token("u-care-manager")) as client:
        assert (await client.get(f"/v1/runs/{run_id}")).status_code == 200  # customer-care department
    async with make_client(user_token("u-noc-manager")) as client:
        assert (await client.get(f"/v1/runs/{run_id}")).status_code == 404  # other department
    async with app_client("gct-coding-assistant") as client:
        assert (await client.get(f"/v1/runs/{run_id}")).status_code == 404  # other application
    async with make_client(user_token("u-finance")) as client:
        assert (await client.get(f"/v1/runs/{run_id}")).status_code == 403  # no runs:read


async def test_policy_evaluate_dry_run(app_client, db) -> None:
    async with app_client("gct-fraud-investigator") as client:
        response = await client.post(
            "/v1/policy/evaluate", json={"messages": [{"role": "user", "content": "x"}]}
        )
    body = response.json()
    assert response.status_code == 200, body
    assert body["classification"]["effective"] == "confidential"  # application floor
    assert body["decision"]["effective"]["execution_path"] == "sovereign-direct"


async def test_list_policies_requires_role(make_client, user_token) -> None:
    async with make_client(user_token("u-auditor")) as client:
        versions = await client.get("/v1/admin/policies")
    async with make_client(user_token("u-tester")) as client:
        denied = await client.get("/v1/admin/policies")
    assert versions.status_code == 200 and versions.json()[0]["status"] == "published"
    assert denied.status_code == 403


@pytest.mark.parametrize(
    ("method", "path", "user", "body"),
    [
        (
            "post",
            "/v1/evaluations/run",
            "u-tester",
            {"benchmark": "confidentiality", "dataset_revision": "r1", "max_cost_micros": 0},
        ),
    ],
)
async def test_unbuilt_endpoints_authorise_then_return_501(
    make_client, user_token, method, path, user, body
) -> None:
    async with make_client() as client:
        assert (await client.request(method.upper(), path, json=body)).status_code == 401
    async with make_client(user_token(user)) as client:
        response = await client.request(method.upper(), path, json=body)
    assert response.status_code == 501
    assert response.json()["error"]["code"] == "NOT_IMPLEMENTED"


async def test_scenario_catalog(app_client) -> None:
    async with app_client("gct-support-chatbot") as client:
        scenarios = (await client.get("/v1/catalog/scenarios")).json()
    # The ten locked scenarios, then the live demo page's use cases (scenarios 11-16).
    assert [s["number"] for s in scenarios if s["live_demo_use_case"] is None] == list(range(1, 11))
    assert [s["number"] for s in scenarios if s["live_demo_use_case"]] == list(range(11, 17))
