"""Govern administrative reads: applications, trusted networks, model allowlist, tools (LLD §22.5)."""

from dataclasses import replace

import pytest

from aiva_gateway.core.money import usd_to_micros
from tests.conftest import ADMIN, with_overrides


async def _get(make_client, user_token, path: str, user: str = ADMIN):
    async with make_client(user_token(user)) as client:
        return await client.get(path)


async def test_applications_show_enforced_grants_without_secrets(
    make_client, user_token, credentials
) -> None:
    response = await _get(make_client, user_token, "/v1/admin/applications")
    body = response.json()
    assert response.status_code == 200, body
    apps = {a["id"]: a for a in body["applications"]}
    assert body["policy_version"] and len(body["bundle_hash"]) == 64
    agent = apps["gct-incident-agent"]
    assert "notification.send" in agent["allowed_tools"]
    assert {n["id"] for n in agent["trusted_networks"]} == {"net-noc", "net-test-console"}
    assert apps["gct-fraud-investigator"]["classification_floor"] == "confidential"
    # ADR-0027 §3: the applications that take key calls from the internet.
    assert {a for a, grants in apps.items() if grants["internet_callers"]} == {
        "gct-api-sandbox",
        "gct-coding-assistant",
        "gct-knowledge-portal",
    }
    support = apps["gct-support-chatbot"]
    metadata = {
        "key_id",
        "label",
        "created_at",
        "revoked_at",
        "created_by",
        "revoked_by",
        "expires_at",
        "last_used_at",
        "active",
    }
    assert support["credentials"] and all(set(c) == metadata for c in support["credentials"])
    secret = credentials["gct-support-chatbot"].split(".", 1)[1]
    assert secret not in response.text

    one = await _get(make_client, user_token, "/v1/admin/applications/gct-support-chatbot")
    assert one.status_code == 200 and one.json()["id"] == "gct-support-chatbot"
    assert (await _get(make_client, user_token, "/v1/admin/applications/nope")).status_code == 404


async def test_trusted_networks_and_ingress(make_client, user_token) -> None:
    body = (await _get(make_client, user_token, "/v1/admin/trusted-networks")).json()
    networks = {n["id"]: n for n in body["networks"]}
    assert networks["net-fraud-soc"]["cidr"] == "10.30.10.0/24"
    assert networks["net-fraud-soc"]["application_ids"] == ["gct-fraud-investigator"]
    assert len(networks["net-test-console"]["application_ids"]) == 10
    assert body["ingress"] == {
        "trusted_proxies": [],
        "forwarded_for_trusted": False,
        "simulated_ingress_allowed": True,
    }


async def test_model_allowlist(make_client, user_token) -> None:
    body = (await _get(make_client, user_token, "/v1/admin/models")).json()
    models = {m["id"]: m for m in body["models"]}
    assert models["gct-qwen-finetuned"]["sovereign"] and not models["gct-qwen-finetuned"]["external"]
    assert models["claude-flagship"]["external"] and len(models["claude-flagship"]["application_ids"]) == 10
    assert models["enterprise-cim"]["role"] == "cim"
    conf = next(p for p in body["policy_allowlists"] if p["policy_id"] == "GCT-CONF-001")
    assert conf["allowed_models"] == ["gct-qwen-finetuned"] and conf["execution_path"] == "sovereign-direct"
    assert any("AIPEL-INV-002" in i for i in body["invariants"])


async def test_models_carry_catalog_fields_and_integer_prices(make_client, user_token, services) -> None:
    models = {m["id"]: m for m in (await _get(make_client, user_token, "/v1/admin/models")).json()["models"]}
    claude = models["claude-flagship"]
    assert claude["catalog_configured"] and claude["provider"] == "anthropic"
    assert isinstance(claude["context_window"], int) and claude["context_window"] > 0
    # Prices and the version come from config/rate_cards.yaml, so a rate-card change needs no edit here.
    card = services.catalog.rate_card("rc-claude-opus-5-5")
    assert claude["rate_card"] == {
        "id": "rc-claude-opus-5-5",
        "version": services.catalog.rate_cards.version,
        "currency": "USD",
        "input_micros_per_mtok": usd_to_micros(card.input_per_mtok),
        "output_micros_per_mtok": usd_to_micros(card.output_per_mtok),
        "cached_input_micros_per_mtok": usd_to_micros(card.cached_input_per_mtok),
        "gpu_micros_per_hour": None,
        "effective_from": card.effective_from.isoformat(),
        "source": card.source,
    }
    qwen = models["gct-qwen-finetuned"]["rate_card"]
    qwen_card = services.catalog.rate_card("rc-gct-qwen-local")
    assert qwen["input_micros_per_mtok"] == 0  # GPU-served: $0 token price by POC convention (ADR-0006)
    assert isinstance(qwen["gpu_micros_per_hour"], int)  # integer micro-USD, never a float
    assert qwen["gpu_micros_per_hour"] == usd_to_micros(qwen_card.gpu_per_hour)
    assert "price_provenance" not in qwen  # prices carry citations, not provenance labels


async def test_a_bundle_model_missing_from_the_catalog_reads_as_not_configured(
    services, make_client, user_token
) -> None:
    catalog = services.catalog
    registry = catalog.models.model_copy(
        update={"models": [m for m in catalog.models.models if m.id != "deepseek-v4-1-flash"]}
    )
    reduced = with_overrides(services, catalog=replace(catalog, models=registry))
    async with make_client(user_token(ADMIN), svc=reduced) as client:
        body = (await client.get("/v1/admin/models")).json()
    missing = next(m for m in body["models"] if m["id"] == "deepseek-v4-1-flash")
    assert missing["catalog_configured"] is False
    assert {
        k: missing[k] for k in ("provider", "provider_model_id", "revision", "context_window", "rate_card")
    } == {
        "provider": None,
        "provider_model_id": None,
        "revision": None,
        "context_window": None,
        "rate_card": None,
    }
    assert missing["display_name"] == "deepseek-v4-1-flash"  # still listed: the bundle enforces it


async def test_tools(make_client, user_token) -> None:
    body = (await _get(make_client, user_token, "/v1/admin/tools")).json()
    tools = {t["id"]: t for t in body["tools"]}
    assert (tools["notification.send"]["access"], tools["notification.send"]["egress"]) == (
        "write",
        "external",
    )
    assert tools["notification.send"]["application_ids"] == ["gct-incident-agent"]
    assert tools["subscriber.suspend"]["application_ids"] == []


@pytest.mark.parametrize(
    "path", ["/v1/admin/applications", "/v1/admin/trusted-networks", "/v1/admin/models", "/v1/admin/tools"]
)
async def test_admin_reads_require_policy_read(make_client, user_token, app_client, path: str) -> None:
    assert (await _get(make_client, user_token, path, "u-auditor")).status_code == 200
    assert (await _get(make_client, user_token, path, "u-tester")).status_code == 403
    async with app_client("gct-support-chatbot") as client:
        assert (await client.get(path)).status_code == 403
    async with make_client() as client:
        assert (await client.get(path)).status_code == 401
