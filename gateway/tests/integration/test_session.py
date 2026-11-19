"""Caller profile and the dev-only demo sign-in (`/v1/auth/*`)."""

from datetime import UTC, datetime, timedelta

from aiva_gateway.services import Services
from tests.conftest import chat_body, console, with_overrides


def _dev_enabled(services: Services) -> Services:
    return with_overrides(services, settings=services.settings.model_copy(update={"dev_sign_in": True}))


async def test_me_returns_roles_and_permissions_for_users(make_client, user_token) -> None:
    async with make_client(user_token("u-tester")) as client:
        tester = (await client.get("/v1/auth/me")).json()
    async with make_client(user_token("u-auditor")) as client:
        auditor = (await client.get("/v1/auth/me")).json()
    assert tester["kind"] == "user" and tester["user_id"] == "u-tester"
    assert tester["display_name"] == "AiVa Tester (demo)"
    assert tester["roles"] == ["tester"]
    assert "chat:test" in tester["permissions"] and "policies:read" not in tester["permissions"]
    assert auditor["roles"] == ["security_auditor"]
    assert {"policies:read", "runs:read"} <= set(auditor["permissions"])
    assert "chat:test" not in auditor["permissions"]


async def test_me_for_applications_has_no_user_permissions(app_client) -> None:
    async with app_client("gct-support-chatbot") as client:
        body = (await client.get("/v1/auth/me")).json()
    assert body["kind"] == "application"
    assert body["application_id"] == "gct-support-chatbot"
    assert body["permissions"] == [] and body["user_id"] is None


async def test_me_requires_credentials(make_client) -> None:
    async with make_client() as client:
        response = await client.get("/v1/auth/me")
    assert response.status_code == 401


async def test_dev_sign_in_is_disabled_by_default(make_client) -> None:
    async with make_client() as client:
        users = await client.get("/v1/auth/dev/users")
        token = await client.post("/v1/auth/dev/token", json={"user_id": "u-tester"})
    for response in (users, token):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "DEV_SIGN_IN_DISABLED"


async def test_dev_sign_in_lists_demo_users_and_mints_a_working_token(make_client, services) -> None:
    enabled = _dev_enabled(services)
    async with make_client(svc=enabled) as client:
        users = (await client.get("/v1/auth/dev/users")).json()
        minted = await client.post("/v1/auth/dev/token", json={"user_id": "u-tester"})
    assert {u["user_id"] for u in users} >= {"u-tester", "u-auditor", "u-platform-admin"}
    assert next(u for u in users if u["user_id"] == "u-tester")["roles"] == ["tester"]
    body = minted.json()
    assert minted.status_code == 200, body
    assert body["token_type"] == "Bearer"
    expires_at = datetime.fromisoformat(body["expires_at"])
    assert timedelta(minutes=50) < expires_at - datetime.now(UTC) <= timedelta(hours=1)

    async with make_client(body["access_token"], svc=enabled) as client:
        assert (await client.get("/v1/auth/me")).json()["user_id"] == "u-tester"
        run = await client.post("/v1/ai/chat", json=chat_body("Roaming?", **console("gct-support-chatbot")))
    assert run.status_code == 200, run.json()
    assert run.json()["identity"]["identity_mode"] == "simulated"


async def test_dev_sign_in_stays_refused_in_production(make_client, services) -> None:
    # Settings validation refuses this combination at start-up; the endpoint re-checks it regardless.
    production = with_overrides(
        services, settings=services.settings.model_copy(update={"dev_sign_in": True, "environment": "prod"})
    )
    async with make_client(svc=production) as client:
        response = await client.post("/v1/auth/dev/token", json={"user_id": "u-platform-admin"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DEV_SIGN_IN_DISABLED"


async def test_dev_sign_in_rejects_unknown_users(make_client, services) -> None:
    async with make_client(svc=_dev_enabled(services)) as client:
        response = await client.post("/v1/auth/dev/token", json={"user_id": "u-nobody"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
