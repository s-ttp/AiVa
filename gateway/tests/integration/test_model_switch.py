"""Model switches per application (ADR-0027 §2): a switch is a policy draft that only narrows, published
through the existing lifecycle; switching back on is high-impact; the read models show the state."""

from typing import Any

from sqlalchemy import select

from aiva_gateway.db.models import AuditLogRow
from tests.conftest import ADMIN, SECOND_ADMIN

APP = "gct-support-chatbot"
SWITCH = f"GCT-SWITCH-{APP}"
GRANTS = ["gct-qwen-finetuned", "gemini-flash-lite", "deepseek-v4-1-flash", "claude-flagship"]


async def _switch(make_client, token: str, model_id: str, enabled: bool, **extra: Any):  # type: ignore[no-untyped-def]
    body = {"application_id": APP, "model_id": model_id, "enabled": enabled, **extra}
    async with make_client(token) as client:
        return await client.post("/v1/admin/policies/model-switch", json=body)


async def _publish(make_client, token: str, version: int):  # type: ignore[no-untyped-def]
    async with make_client(token) as client:
        return await client.post(
            "/v1/admin/policies/publish", json={"version": version, "approval_note": "model switch"}
        )


async def _state(make_client, token: str) -> tuple[dict[str, Any], dict[str, Any]]:  # type: ignore[no-untyped-def]
    async with make_client(token) as client:
        apps = (await client.get("/v1/admin/applications")).json()
        models = (await client.get("/v1/admin/models")).json()
    return apps, models


async def test_switching_off_drafts_a_narrowing_policy(services, make_client, user_token, db) -> None:  # type: ignore[no-untyped-def]
    response = await _switch(make_client, user_token(ADMIN), "gemini-flash-lite", False, notes="cost review")
    body = response.json()
    assert response.status_code == 201, body
    draft = body["draft"]
    assert draft["status"] == "draft" and draft["authored_by_viewer"] is True
    assert (
        "gemini-flash-lite off for gct-support-chatbot" in draft["notes"] and "cost review" in draft["notes"]
    )
    switch = next(p for p in draft["policies"] if p["policy_id"] == SWITCH)
    assert switch["match"]["application_any"] == [APP]
    assert switch["enforce"]["allowed_models"] == [m for m in GRANTS if m != "gemini-flash-lite"]
    assert [(c["target_id"], c["change"], c["high_impact"]) for c in draft["changes_from_published"]] == [
        (SWITCH, "added", False)
    ]
    assert body["switched_off_models"] == ["gemini-flash-lite"] and body["high_impact"] is False
    checks = {c["classification"]: c for c in body["checks"]}
    assert list(checks) == ["internal", "confidential", "restricted", "sovereign-only"]  # floor and above
    assert "gemini-flash-lite" in checks["internal"]["allowed_models_before"]
    assert "gemini-flash-lite" not in checks["internal"]["allowed_models_after"]
    assert checks["confidential"]["allowed_models_after"] == ["gct-qwen-finetuned"]
    assert not any(c["blocked"] for c in checks.values())
    assert body["sovereign_blocked"] is False and body["warnings"] == []
    # A draft changes nothing until it is published.
    apps, _ = await _state(make_client, user_token(ADMIN))
    assert {a["id"]: a for a in apps["applications"]}[APP]["switched_off_models"] == []
    audit = (
        await db.execute(
            select(AuditLogRow).where(
                AuditLogRow.action == "policy.model_switch",
                AuditLogRow.target_id == str(draft["version"]),
            )
        )
    ).scalar_one()
    assert audit.actor_id == draft["created_by"]  # the administrator's pseudonym
    assert audit.details["model_id"] == "gemini-flash-lite" and audit.details["enabled"] is False


async def test_a_published_switch_narrows_and_switching_on_needs_a_second_administrator(
    services, make_client, user_token, policy_versions
) -> None:  # type: ignore[no-untyped-def]
    off = (await _switch(make_client, user_token(ADMIN), "gemini-flash-lite", False)).json()
    # Switching off only narrows: its author may publish it.
    assert (await _publish(make_client, user_token(ADMIN), off["draft"]["version"])).status_code == 200
    apps, models = await _state(make_client, user_token(ADMIN))
    assert {a["id"]: a for a in apps["applications"]}[APP]["switched_off_models"] == ["gemini-flash-lite"]
    gemini = {m["id"]: m for m in models["models"]}["gemini-flash-lite"]
    assert gemini["switched_off_application_ids"] == [APP] and APP in gemini["application_ids"]
    async with make_client(user_token("u-tester")) as client:
        evaluated = await client.post(
            "/v1/policy/evaluate",
            json={
                "messages": [{"role": "user", "content": "Explain roaming charges."}],
                "test_profile": {"application_id": APP, "simulated_source_ip": "192.0.2.10"},
            },
        )
    decision = evaluated.json()["decision"]
    assert "gemini-flash-lite" not in decision["effective"]["allowed_models"]
    assert SWITCH in decision["matched_rule_ids"]

    # A second switch builds on the published one.
    second = (await _switch(make_client, user_token(ADMIN), "deepseek-v4-1-flash", False)).json()
    assert second["switched_off_models"] == ["deepseek-v4-1-flash", "gemini-flash-lite"]
    assert second["base_version"] == off["draft"]["version"]

    on = (await _switch(make_client, user_token(ADMIN), "gemini-flash-lite", True)).json()
    assert on["switched_off_models"] == [] and on["high_impact"] is True
    assert SWITCH not in {p["policy_id"] for p in on["draft"]["policies"]}
    assert [(c["target_id"], c["change"]) for c in on["draft"]["changes_from_published"]] == [
        (SWITCH, "removed")
    ]
    own = await _publish(make_client, user_token(ADMIN), on["draft"]["version"])
    assert own.status_code == 403 and own.json()["error"]["code"] == "SEPARATION_OF_DUTIES"
    other = await _publish(make_client, user_token(SECOND_ADMIN), on["draft"]["version"])
    assert other.status_code == 200
    apps, _ = await _state(make_client, user_token(ADMIN))
    assert {a["id"]: a for a in apps["applications"]}[APP]["switched_off_models"] == []
    # The policy_versions fixture restores the starting version afterwards.


async def test_switching_another_model_back_on_keeps_the_rest_off(
    services, make_client, user_token, policy_versions
) -> None:  # type: ignore[no-untyped-def]
    for model in ("gemini-flash-lite", "deepseek-v4-1-flash"):
        draft = (await _switch(make_client, user_token(ADMIN), model, False)).json()["draft"]
        assert (await _publish(make_client, user_token(ADMIN), draft["version"])).status_code == 200
    on = (await _switch(make_client, user_token(ADMIN), "deepseek-v4-1-flash", True)).json()
    switch = next(p for p in on["draft"]["policies"] if p["policy_id"] == SWITCH)
    assert switch["enforce"]["allowed_models"] == [m for m in GRANTS if m != "gemini-flash-lite"]
    assert [
        (c["target_id"], c["change"], c["high_impact"]) for c in on["draft"]["changes_from_published"]
    ] == [(SWITCH, "modified", True)]


async def test_only_granted_models_and_real_changes(make_client, user_token) -> None:  # type: ignore[no-untyped-def]
    token = user_token(ADMIN)
    async with make_client(token) as client:
        not_granted = await client.post(
            "/v1/admin/policies/model-switch",
            json={
                "application_id": "gct-market-intelligence",
                "model_id": "gct-qwen-finetuned",
                "enabled": False,
            },
        )
        unknown_app = await client.post(
            "/v1/admin/policies/model-switch",
            json={"application_id": "nope", "model_id": "gemini-flash-lite", "enabled": False},
        )
    already_on = await _switch(make_client, token, "gemini-flash-lite", True)
    unknown_model = await _switch(make_client, token, "enterprise-cim", False)
    assert not_granted.status_code == 422 and not_granted.json()["error"]["code"] == "MODEL_NOT_GRANTED"
    assert unknown_model.status_code == 422 and unknown_model.json()["error"]["code"] == "MODEL_NOT_GRANTED"
    assert unknown_app.status_code == 404
    assert already_on.status_code == 409 and already_on.json()["error"]["code"] == "MODEL_SWITCH_UNCHANGED"


async def test_only_administrators_switch_and_authorization_comes_first(
    make_client, user_token, credentials
) -> None:  # type: ignore[no-untyped-def]
    invalid = {"application_id": APP}  # missing fields: a 422 only for a caller who may switch
    for user in ("u-auditor", "u-tester"):
        async with make_client(user_token(user)) as client:
            response = await client.post("/v1/admin/policies/model-switch", json=invalid)
        assert response.status_code == 403, user
    async with make_client(credentials[APP]) as client:
        assert (await client.post("/v1/admin/policies/model-switch", json=invalid)).status_code == 403
    async with make_client() as client:
        assert (await client.post("/v1/admin/policies/model-switch", json=invalid)).status_code == 401
    async with make_client(user_token(ADMIN)) as client:
        assert (await client.post("/v1/admin/policies/model-switch", json=invalid)).status_code == 422


async def test_the_read_models_say_whether_the_viewer_may_switch(make_client, user_token) -> None:  # type: ignore[no-untyped-def]
    admin_apps, admin_models = await _state(make_client, user_token(ADMIN))
    auditor_apps, auditor_models = await _state(make_client, user_token("u-auditor"))
    assert admin_apps["viewer_can_switch"] is admin_models["viewer_can_switch"] is True
    assert auditor_apps["viewer_can_switch"] is auditor_models["viewer_can_switch"] is False
