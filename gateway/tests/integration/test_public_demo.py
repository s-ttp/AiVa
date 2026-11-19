"""Public demo (ADR-0019): the platform-proxy credential channel, public sessions with per-session
pseudonyms, what the `public_visitor` role may and may not do, and the session rate limits."""

import json
import random
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import select

from aiva_gateway.auth.authenticate import PUBLIC_VISITOR_ID
from aiva_gateway.auth.tokens import mint_user_token, new_session_id
from aiva_gateway.config import PUBLIC_SESSIONS_PER_SOURCE, REPO_ROOT
from aiva_gateway.db.models import AuditLogRow
from aiva_gateway.services import Services
from tests.conftest import CONSOLE_IP, chat_body, console, with_overrides

SECRET = "proxy-identity-secret-for-tests-0123456789"
HF_TOKEN_CANARY = "hf_CANARYplatformTokenNeverRead0123"
APP = "gct-support-chatbot"


def proxied(services: Services, **update: Any) -> Services:
    """A deployed gateway behind the platform proxy (the Space 2 image)."""
    settings = services.settings.model_copy(
        update={
            "ingress": "platform-proxy",
            "proxy_secret": SecretStr(SECRET),
            "environment": "demo",
            **update,
        }
    )
    return with_overrides(services, settings=settings)


def visitor_ip() -> str:
    """A fresh public address outside every tenant network, so rate-limit windows never collide."""
    return f"34.{random.randint(2, 254)}.{random.randint(0, 254)}.{random.randint(1, 254)}"


def proof(ip: str | None = None) -> dict[str, str]:
    return {"X-AiVa-Proxy-Secret": SECRET, "X-AiVa-Client-IP": ip or visitor_ip()}


def through_proxy(token: str | None = None, ip: str | None = None) -> dict[str, str]:
    """Headers as the proxy sends them: the platform's token in Authorization, the AiVa one moved."""
    headers = {"Authorization": f"Bearer {HF_TOKEN_CANARY}", **proof(ip)}
    if token:
        headers["X-AiVa-Authorization"] = f"Bearer {token}"
    return headers


@pytest.fixture(autouse=True)
async def clear_limits(services: Services) -> None:
    for pattern in ("aiva:public-session:*", "aiva:demo-sign-in:*"):
        async for key in services.redis.scan_iter(match=pattern):
            await services.redis.delete(key)


async def start_session(
    make_client, svc: Services | None = None, headers: dict[str, str] | None = None
) -> str:  # type: ignore[no-untyped-def]
    async with make_client(svc=svc, headers=headers) as client:
        response = await client.post("/v1/auth/public/session")
    assert response.status_code == 201, response.text
    token: str = response.json()["access_token"]
    return token


# --- Credential channel -----------------------------------------------------------------------------


async def test_behind_the_proxy_authorization_is_never_read(services, make_client, user_token) -> None:  # type: ignore[no-untyped-def]
    svc = proxied(services)
    tester = user_token("u-tester")
    # A valid AiVa token in Authorization is ignored there: that header is the platform's.
    async with make_client(svc=svc, headers={"Authorization": f"Bearer {tester}", **proof()}) as client:
        ignored = await client.get("/v1/auth/me")
    async with make_client(svc=svc, headers=through_proxy(tester)) as client:
        me = await client.get("/v1/auth/me")
    assert ignored.status_code == 401
    assert me.status_code == 200 and me.json()["user_id"] == "u-tester"


async def test_direct_ingress_prefers_x_aiva_authorization(make_client, user_token) -> None:  # type: ignore[no-untyped-def]
    headers = {
        "Authorization": "Bearer not-a-credential",
        "X-AiVa-Authorization": f"Bearer {user_token('u-auditor')}",
    }
    async with make_client(headers=headers) as client:
        me = await client.get("/v1/auth/me")
    async with make_client(user_token("u-auditor")) as client:
        plain = await client.get("/v1/auth/me")
    assert me.json()["user_id"] == plain.json()["user_id"] == "u-auditor"


async def test_a_proxied_request_without_a_credential_is_still_refused(services, make_client) -> None:  # type: ignore[no-untyped-def]
    async with make_client(svc=proxied(services), headers=through_proxy()) as client:
        response = await client.get("/v1/runs/run_none")
    assert response.status_code == 401


# --- Public sessions --------------------------------------------------------------------------------


async def test_a_deployed_gateway_issues_sessions_only_through_the_proxy(
    services, make_client, user_token
) -> None:  # type: ignore[no-untyped-def]
    svc = proxied(services)
    async with make_client(svc=svc) as client:
        direct = await client.post("/v1/auth/public/session")
    async with make_client(svc=svc, headers={"X-AiVa-Proxy-Secret": SECRET[:-1] + "!"}) as client:
        forged = await client.post("/v1/auth/public/session")
    async with make_client(svc=svc, headers=through_proxy()) as client:
        issued = await client.post("/v1/auth/public/session")
        proxied_options = (await client.get("/v1/auth/sign-in-options")).json()
    # A deployed gateway reached directly (no platform proxy) issues none either.
    deployed = with_overrides(services, settings=services.settings.model_copy(update={"environment": "demo"}))
    async with make_client(svc=deployed) as client:
        unproxied = await client.post("/v1/auth/public/session")
        unproxied_options = (await client.get("/v1/auth/sign-in-options")).json()
    assert direct.status_code == forged.status_code == 401
    assert direct.json()["error"]["code"] == forged.json()["error"]["code"] == "PROXY_PROOF_REQUIRED"
    assert unproxied.status_code == 403 and unproxied.json()["error"]["code"] == "PUBLIC_SESSION_UNAVAILABLE"
    assert issued.status_code == 201 and issued.json()["token_type"] == "Bearer"
    assert (unproxied_options["public"], proxied_options["public"]) == (False, True)
    expires = datetime.fromisoformat(issued.json()["expires_at"])
    assert 7000 < (expires - datetime.now(UTC)).total_seconds() <= 7200


async def test_a_visitor_session_has_the_public_visitor_role(make_client) -> None:  # type: ignore[no-untyped-def]
    token = await start_session(make_client)
    async with make_client(token) as client:
        me = (await client.get("/v1/auth/me")).json()
    assert me["user_id"] == PUBLIC_VISITOR_ID and me["roles"] == ["public_visitor"]
    assert me["department_id"] == "public-demo"
    granted = set(me["permissions"])
    assert {"chat:test", "chat:live", "runs:read", "reports:read", "policies:read", "catalog:read"} <= granted
    assert not granted & {
        "approvals:decide",
        "approvals:read",
        "policies:write",
        "policies:publish",
        "budgets:read",
        "budgets:write",
        "reports:export",
        "reports:identity",
        "evaluations:run",
    }


async def test_visitors_run_console_tests_and_read_only_their_own_runs(
    make_client, app_client, tester_client
) -> None:  # type: ignore[no-untyped-def]
    first, second = await start_session(make_client), await start_session(make_client)
    async with make_client(first) as client:
        ran = await client.post("/v1/ai/chat", json=chat_body("Explain roaming charges.", **console(APP)))
    assert ran.status_code == 200, ran.text
    run_id = ran.json()["run_id"]
    async with tester_client() as client:
        tester_run = (
            await client.post("/v1/ai/chat", json=chat_body("Explain roaming.", **console(APP)))
        ).json()
    async with app_client(APP) as client:
        app_run = (await client.post("/v1/ai/chat", json=chat_body("Explain roaming."))).json()

    async with make_client(first) as client:
        own = await client.get(f"/v1/runs/{run_id}")
        trace = await client.get(f"/v1/runs/{run_id}/trace")
        application = await client.get(f"/v1/runs/{app_run['run_id']}")
        testers = await client.get(f"/v1/runs/{tester_run['run_id']}")
    async with make_client(second) as client:
        others = await client.get(f"/v1/runs/{run_id}")
    assert own.status_code == trace.status_code == application.status_code == 200
    assert own.json()["identity_mode"] == "simulated"
    assert testers.status_code == others.status_code == 404


async def test_visitors_cannot_see_each_others_workflows(make_client) -> None:  # type: ignore[no-untyped-def]
    first, second = await start_session(make_client), await start_session(make_client)
    body = {"incident_id": "INC-7781", **console("gct-incident-agent")}
    async with make_client(first) as client:
        started = await client.post("/v1/agents/workflows", json=body)
        workflow_id = started.json()["workflow_id"]
        mine = [w["workflow_id"] for w in (await client.get("/v1/agents/workflows")).json()["items"]]
    async with make_client(second) as client:
        theirs = [w["workflow_id"] for w in (await client.get("/v1/agents/workflows")).json()["items"]]
        hidden = await client.get(f"/v1/agents/workflows/{workflow_id}")
    assert started.status_code in (200, 202), started.text
    assert workflow_id in mine and workflow_id not in theirs
    assert hidden.status_code == 404


async def test_reports_show_a_visitor_no_user_dimension(make_client, user_token) -> None:  # type: ignore[no-untyped-def]
    token = await start_session(make_client)
    today = datetime.now(UTC).date().isoformat()
    window = {"from": today, "to": today}
    async with make_client(token) as client:
        await client.post("/v1/ai/chat", json=chat_body("Explain roaming charges.", **console(APP)))
        overview = await client.get("/v1/reports/overview", params=window)
        by_user = await client.get("/v1/reports/consumption", params={**window, "group_by": "user"})
        by_app = await client.get("/v1/reports/consumption", params={**window, "group_by": "application"})
        filtered = await client.get("/v1/reports/runs", params={**window, "user": "usr_0000000000000000"})
        runs = await client.get("/v1/reports/runs", params=window)
        options = await client.get("/v1/reports/filters", params=window)
        export = await client.post(
            "/v1/reports/exports", json={"report": "consumption", "group_by": "application", "window": window}
        )
    assert overview.status_code == by_app.status_code == runs.status_code == options.status_code == 200
    assert by_user.status_code == filtered.status_code == export.status_code == 403
    assert runs.json()["runs"] and {r["principal"] for r in runs.json()["runs"]} == {"hidden"}
    assert options.json()["options"]["user"] == []
    # Every report response that carries the scope says so, so the UI need not infer it from the role.
    scopes = [overview.json()["metadata"]["scope"], runs.json()["metadata"]["scope"], options.json()["scope"]]
    assert [s["users_visible"] for s in scopes] == [False, False, False]
    async with make_client(user_token("u-auditor")) as client:
        auditor = (await client.get("/v1/reports/filters", params=window)).json()["scope"]
    assert auditor["users_visible"] is True


async def test_visitor_tokens_come_only_from_a_public_session(services, make_client) -> None:  # type: ignore[no-untyped-def]
    settings = services.settings
    no_session = mint_user_token(settings, user_id=PUBLIC_VISITOR_ID, tenant_id="gct")
    tester_with_session = mint_user_token(
        settings, user_id="u-tester", tenant_id="gct", session_id=new_session_id()
    )
    for token in (no_session, tester_with_session):
        async with make_client(token) as client:
            assert (await client.get("/v1/auth/me")).status_code == 401
    dev = with_overrides(services, settings=settings.model_copy(update={"dev_sign_in": True}))
    async with make_client(svc=dev) as client:
        listed = {u["user_id"] for u in (await client.get("/v1/auth/dev/users")).json()}
        minted = await client.post("/v1/auth/dev/token", json={"user_id": PUBLIC_VISITOR_ID})
    assert PUBLIC_VISITOR_ID not in listed and minted.status_code == 404


async def test_starting_a_session_is_audited_by_pseudonym_only(services, make_client, db) -> None:  # type: ignore[no-untyped-def]
    address = visitor_ip()
    token = await start_session(make_client, proxied(services), through_proxy(ip=address))
    async with make_client(token) as client:
        me = (await client.get("/v1/auth/me")).json()
    row = (
        await db.scalars(
            select(AuditLogRow)
            .where(AuditLogRow.action == "auth.public_session")
            .order_by(AuditLogRow.id.desc())
        )
    ).first()
    assert row is not None and row.actor_id.startswith("usr_") and row.target_id == row.actor_id
    assert row.details["source"].startswith("cli_")
    assert address not in json.dumps({"actor": row.actor_id, "details": row.details})
    assert me["user_id"] == PUBLIC_VISITOR_ID


# --- Rate limits ------------------------------------------------------------------------------------


async def test_sessions_are_limited_per_source(services, make_client) -> None:  # type: ignore[no-untyped-def]
    svc, address = proxied(services), visitor_ip()
    statuses = []
    async with make_client(svc=svc, headers=through_proxy(ip=address)) as client:
        for _ in range(PUBLIC_SESSIONS_PER_SOURCE + 1):
            response = await client.post("/v1/auth/public/session")
            statuses.append(response.status_code)
    async with make_client(svc=svc, headers=through_proxy(ip=visitor_ip())) as client:
        elsewhere = await client.post("/v1/auth/public/session")
    assert statuses == [201] * PUBLIC_SESSIONS_PER_SOURCE + [429]
    assert response.json()["error"]["code"] == "PUBLIC_SESSION_LIMITED"
    assert 0 < int(response.headers["Retry-After"]) <= 3600
    assert elsewhere.status_code == 201


async def test_sessions_are_limited_tenant_wide(services, make_client) -> None:  # type: ignore[no-untyped-def]
    # The test environment may lower (or raise) the limits; three sources, a tenant-wide limit of two.
    svc = with_overrides(
        services, settings=services.settings.model_copy(update={"public_sessions_per_tenant": 2})
    )
    statuses = []
    for peer in ("10.81.0.1", "10.81.0.2", "10.81.0.3"):
        async with make_client(svc=svc, peer=peer) as client:
            statuses.append((await client.post("/v1/auth/public/session")).status_code)
    assert statuses == [201, 201, 429]


async def _sessions_from_one_source(make_client, svc: Services, count: int, **client: Any) -> list[int]:  # type: ignore[no-untyped-def]
    async with make_client(svc=svc, **client) as session_client:
        return [(await session_client.post("/v1/auth/public/session")).status_code for _ in range(count)]


async def test_a_local_or_test_gateway_may_raise_the_limits(services, make_client) -> None:  # type: ignore[no-untyped-def]
    # An e2e suite or a replay gate needs more sessions per source than the public demo allows.
    raised = PUBLIC_SESSIONS_PER_SOURCE + 3
    svc = with_overrides(
        services, settings=services.settings.model_copy(update={"public_sessions_per_source": raised})
    )
    statuses = await _sessions_from_one_source(make_client, svc, raised + 1, peer="10.82.0.1")
    assert statuses == [201] * raised + [429]


async def test_a_deployed_gateway_keeps_the_limits_whatever_its_settings_say(services, make_client) -> None:  # type: ignore[no-untyped-def]
    # `model_copy` skips the start-up validator; the limiter still uses the public demo's limits.
    svc = proxied(services, public_sessions_per_source=50, public_sessions_per_tenant=1000)
    headers = through_proxy(ip=visitor_ip())
    statuses = await _sessions_from_one_source(
        make_client, svc, PUBLIC_SESSIONS_PER_SOURCE + 1, headers=headers
    )
    assert statuses == [201] * PUBLIC_SESSIONS_PER_SOURCE + [429]


async def test_without_redis_no_session_is_issued(services, make_client) -> None:  # type: ignore[no-untyped-def]
    down = Redis.from_url("redis://127.0.0.1:9/0", socket_connect_timeout=0.2)
    try:
        async with make_client(svc=with_overrides(services, redis=down)) as client:
            response = await client.post("/v1/auth/public/session")
    finally:
        await down.aclose()
    assert response.status_code == 503


# --- Route matrix ------------------------------------------------------------------------------------

# Every non-GET operation and what a visitor gets. Console runs and the sign-in routes are open to it;
# everything else is refused (403). A new operation fails `test_every_operation_is_classified` until it is
# added here.
_TODAY = datetime.now(UTC).date().isoformat()
OPEN = {
    ("POST", "/v1/ai/chat"): chat_body("Explain roaming charges.", **console(APP)),
    # The OpenAI-compatible route (ADR-0026) runs the same console run: a user token names its application
    # and the Test Console's simulated source.
    ("POST", "/v1/chat/completions"): chat_body(
        "Explain roaming charges.",
        metadata={"aiva_application": APP, "aiva_simulated_source_ip": CONSOLE_IP},
    ),
    ("POST", "/v1/policy/evaluate"): chat_body("Explain roaming charges.", **console(APP)),
    ("POST", "/v1/agents/workflows"): {"incident_id": "INC-7781", **console("gct-incident-agent")},
    ("POST", "/v1/agents/workflows/{workflow_id}/resume"): {
        "approval_id": "apr_none",
        **console("gct-incident-agent"),
    },
    ("POST", "/v1/artifacts"): {
        "document_type": "docx",
        "title": "Synthetic board note",
        "instructions": "Draft a one-paragraph note.",
        "evidence": [{"id": "SRC-1", "text": "Synthetic capex is 12.5 million."}],
        **console("gct-doc-copilot"),
    },
    ("POST", "/v1/auth/public/session"): None,
    ("POST", "/v1/auth/demo/token"): {"user_id": "u-tester", "passcode": "not-the-passcode"},
    ("POST", "/v1/auth/dev/token"): {"user_id": "u-tester"},
}
REFUSED = {
    ("POST", "/v1/admin/approvals/{approval_id}/approve"): {"note": "visitor"},
    ("POST", "/v1/admin/approvals/{approval_id}/deny"): {"note": "visitor"},
    ("POST", "/v1/admin/budgets"): {
        "scope_type": "application",
        "scope_id": APP,
        "period": "monthly",
        "cap_micros": 1_000_000,
    },
    ("POST", "/v1/admin/budgets/reconcile"): None,
    ("POST", "/v1/admin/policies"): {"policies": []},
    ("POST", "/v1/admin/policies/model-switch"): {
        "application_id": APP,
        "model_id": "gemini-flash-lite",
        "enabled": False,
    },
    ("POST", "/v1/admin/policies/publish"): {"version": 1, "approval_note": "visitor"},
    ("POST", "/v1/admin/policies/test"): {
        "cases": [
            {
                "name": "visitor",
                "expected_action": "allow",
                "sample": {
                    "application_id": APP,
                    "source_ip": "10.20.1.15",
                    "messages": [{"role": "user", "content": "Roaming rates?"}],
                },
            }
        ]
    },
    ("POST", "/v1/evaluations/run"): {
        "benchmark": "confidentiality",
        "dataset_revision": "r1",
        "max_cost_micros": 0,
    },
    ("POST", "/v1/reports/exports"): {
        "report": "consumption",
        "group_by": "application",
        "window": {"from": _TODAY, "to": _TODAY},
    },
    ("POST", "/v1/reports/identities/resolve"): {
        "pseudonyms": ["usr_0000000000000000"],
        "reason": "Investigating a spike",
    },
    # A presenter's answer-quality comparison (ADR-0023): never a visitor (`demo:compare`).
    ("POST", "/v1/runs/{run_id}/quality-check"): None,
    # Application keys (ADR-0027): administrators only (`credentials:manage`).
    ("POST", "/v1/admin/applications/{application_id}/credentials"): {"label": "visitor"},
    ("POST", "/v1/admin/applications/{application_id}/credentials/{key_id}/revoke"): None,
}


def _operations() -> set[tuple[str, str]]:
    spec = json.loads((REPO_ROOT / "contracts" / "openapi.json").read_text())
    return {
        (method.upper(), path) for path, ops in spec["paths"].items() for method in ops if method != "get"
    }


def test_every_operation_is_classified() -> None:
    assert _operations() == set(OPEN) | set(REFUSED)


def _url(path: str) -> str:
    return (
        path.replace("{approval_id}", "apr_none")
        .replace("{workflow_id}", "wf_none")
        .replace("{run_id}", "run_none")
        .replace("{application_id}", APP)
        .replace("{key_id}", "0000000000000000")
    )


async def test_a_visitor_is_refused_every_write_except_console_runs(make_client) -> None:  # type: ignore[no-untyped-def]
    token = await start_session(make_client)
    outcomes = {}
    async with make_client(token) as client:
        for (method, path), body in {**OPEN, **REFUSED}.items():
            response = await client.request(method, _url(path), json=body)
            payload = response.json() if response.content else None
            error = payload.get("error") if isinstance(payload, dict) else None
            outcomes[path] = (response.status_code, (error or {}).get("code"))
    refused = {path for _, path in REFUSED}
    for path, (status, code) in outcomes.items():
        if path in refused:
            assert status == 403, (path, status, code)
        else:
            assert status not in (401, 403, 422), (path, status, code)
