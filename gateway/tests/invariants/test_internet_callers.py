"""ADR-0027 (owner decision 2026-10-26): only the applications the owner named take key calls from the
internet. Every other application keeps AIPEL-NET-001 for a call from outside its trusted networks, a user
never passes on the flag, and every other rule still applies to an internet call."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from aiva_gateway.db.models import ModelAttemptRow
from aiva_gateway.services import Services
from tests.conftest import chat_body, console
from tests.integration.test_public_demo import proxied, through_proxy, visitor_ip

INTERNET_APPS = {"gct-api-sandbox", "gct-coding-assistant", "gct-knowledge-portal"}
EXTERNAL = {"gemini-flash-lite", "deepseek-v4-1-flash", "claude-flagship"}


def test_only_the_owner_named_applications_take_internet_calls(services: Services) -> None:
    assert {a.id for a in services.catalog.tenant.applications if a.internet_callers} == INTERNET_APPS
    # The sandbox's spend is bounded by a hard $5 monthly cap (the owner's option text).
    caps = [
        b
        for b in services.catalog.tenant.budgets
        if b.scope_type == "application" and b.scope_id == "gct-api-sandbox"
    ]
    assert [(b.period, b.cap_usd, b.on_exhaustion) for b in caps] == [("monthly", Decimal("5.00"), "block")]


async def test_other_applications_refuse_key_calls_from_the_internet(
    services, make_client, credentials
) -> None:  # type: ignore[no-untyped-def]
    others = sorted(a.id for a in services.catalog.tenant.applications if a.id not in INTERNET_APPS)
    assert others
    for application_id in others:
        key = credentials[application_id]
        # Behind the platform proxy (null policy source) and directly from a public address.
        async with make_client(svc=proxied(services), headers=through_proxy(key)) as client:
            behind_proxy = (await client.post("/v1/ai/chat", json=chat_body("Roaming?"))).json()
        async with make_client(key, peer=visitor_ip()) as client:
            direct = (await client.post("/v1/ai/chat", json=chat_body("Roaming?"))).json()
        for body in (behind_proxy, direct):
            assert body["status"] == "blocked", application_id
            assert "AIPEL-NET-001" in body["error"]["rule_ids"], application_id


@pytest.mark.parametrize(
    ("application_id", "task"),
    [("gct-api-sandbox", "qa"), ("gct-coding-assistant", "coding"), ("gct-knowledge-portal", "qa")],
)
async def test_named_applications_take_key_calls_through_the_proxy(
    services, make_client, credentials, application_id, task
) -> None:  # type: ignore[no-untyped-def]
    headers = through_proxy(credentials[application_id])
    async with make_client(svc=proxied(services), headers=headers) as client:
        body = (await client.post("/v1/ai/chat", json=chat_body("Explain list slicing", task=task))).json()
    assert body["status"] == "completed", body
    assert body["identity"]["matched_network_id"] == "internet"
    assert body["identity"]["identity_mode"] == "authenticated"


async def test_an_internet_call_keeps_the_sovereign_path(services, make_client, credentials, db) -> None:  # type: ignore[no-untyped-def]
    payload = chat_body(
        "Summarise the synthetic ticket",
        task="summarization",
        data_classification="confidential",
        routing={"mode": "pinned", "tags": ["claude-preferred"], "preferred_model": "claude-flagship"},
    )
    headers = through_proxy(credentials["gct-api-sandbox"])
    async with make_client(svc=proxied(services), headers=headers) as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["identity"]["matched_network_id"] == "internet"
    assert body["execution_path"] == "sovereign-direct"
    assert body["selected_model"] == "gct-qwen-finetuned"
    assert body["cim_invoked"] is False
    rows = await db.execute(select(ModelAttemptRow.model_id).where(ModelAttemptRow.run_id == body["run_id"]))
    assert not set(rows.scalars()) & EXTERNAL


async def test_an_internet_call_keeps_the_other_rules(services, make_client, credentials) -> None:  # type: ignore[no-untyped-def]
    # The coding assistant is not granted summarization, and a secret is refused wherever the call is from.
    headers = through_proxy(credentials["gct-coding-assistant"])
    async with make_client(svc=proxied(services), headers=headers) as client:
        task = (await client.post("/v1/ai/chat", json=chat_body("Summarise", task="summarization"))).json()
        secret = (
            await client.post("/v1/ai/chat", json=chat_body("Use password = hunter2pass", task="coding"))
        ).json()
    assert task["status"] == "blocked" and task["error"]["rule_ids"] == ["AIPEL-TASK-001"]
    assert secret["status"] == "blocked" and "GCT-SEC-002" in secret["error"]["rule_ids"]
    assert "AIPEL-NET-001" not in task["error"]["rule_ids"] + secret["error"]["rule_ids"]


async def test_users_still_need_a_trusted_network(services, make_client, user_token) -> None:  # type: ignore[no-untyped-def]
    # A tester's Test Console run of an internet application, without a simulated source in its networks.
    headers = through_proxy(user_token("u-tester"))
    async with make_client(svc=proxied(services), headers=headers) as client:
        profile = {"test_profile": {"application_id": "gct-api-sandbox"}}
        unsourced = (await client.post("/v1/ai/chat", json=chat_body("Hi", **profile))).json()
        public = (
            await client.post("/v1/ai/chat", json=chat_body("Hi", **console("gct-api-sandbox", visitor_ip())))
        ).json()
        trusted = (
            await client.post("/v1/ai/chat", json=chat_body("Hi", **console("gct-api-sandbox")))
        ).json()
    for body in (unsourced, public):
        assert body["status"] == "blocked" and "AIPEL-NET-001" in body["error"]["rule_ids"]
    assert (
        trusted["status"] == "completed" and trusted["identity"]["matched_network_id"] == "net-test-console"
    )
