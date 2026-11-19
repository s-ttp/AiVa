"""Policy lifecycle: draft, simulate, publish, rollback; separation of duties; audit (ADR-0012)."""

from sqlalchemy import select

from aiva_gateway.db.models import AuditLogRow
from aiva_gateway.govern.policy_engine import OpaPolicyEngine
from aiva_gateway.govern.publisher import latest_published
from tests.conftest import ADMIN, SECOND_ADMIN, chat_body, seeded_policies, with_overrides

BLOCK_ROAMING = {
    "policy_id": "TEST-BLOCK-TAG",
    "title": "Block a test tag",
    "priority": 100,
    "match": {"tags_any": ["blocked-by-draft"]},
    "enforce": {"action": "deny", "reason": "Blocked by the test draft."},
}


def _sample(
    application_id: str, ip: str, content: str = "Roaming rates?", **extra: object
) -> dict[str, object]:
    return {
        "application_id": application_id,
        "source_ip": ip,
        "messages": [{"role": "user", "content": content}],
        **extra,
    }


def _case(name: str, expected: str, sample: dict[str, object]) -> dict[str, object]:
    return {"name": name, "sample": sample, "expected_action": expected}


async def _audit_actions(db, version: int) -> list[str]:
    rows = await db.execute(
        select(AuditLogRow.action)
        .where(AuditLogRow.target_type == "policy_version", AuditLogRow.target_id == str(version))
        .order_by(AuditLogRow.id)
    )
    return list(rows.scalars())


async def test_policy_simulator_demonstrates_identity_cidr_and_confidentiality(
    services, make_client, user_token, db
) -> None:
    """Exit gate: application identity, trusted CIDR and confidentiality constraints in the simulator."""
    support, fraud = "gct-support-chatbot", "gct-fraud-investigator"
    cases = [
        _case("trusted app, trusted CIDR", "allow", _sample(support, "10.20.1.15")),
        _case("trusted app, untrusted CIDR", "deny", _sample(support, "203.0.113.9")),
        _case("CIDR of another application", "deny", _sample(support, "10.30.10.20")),
        _case("unregistered application", "deny", _sample("rogue-app", "10.20.1.15")),
        _case(
            "declared confidential",
            "allow",
            _sample(support, "10.20.1.15", data_classification="confidential"),
        ),
        _case(
            "DLP-detected subscriber number",
            "allow",
            _sample(support, "10.20.1.15", "Customer +971 50 123 4567 asks", data_classification="public"),
        ),
        _case("application floor", "allow", _sample(fraud, "10.30.10.20")),
        _case(
            "simulated identity needs a tester",
            "deny",
            _sample(support, "192.0.2.10", identity_mode="simulated", roles=["finance_analyst"]),
        ),
    ]
    async with make_client(user_token(ADMIN)) as client:
        response = await client.post("/v1/admin/policies/test", json={"cases": cases})
    body = response.json()
    assert response.status_code == 200, body
    assert (body["status"], body["passed"], body["failed"]) == ("published", len(cases), 0)
    results = {r["name"]: r for r in body["results"]}

    allowed = results["trusted app, trusted CIDR"]
    assert allowed["decision"]["matched_network_id"] == "net-care-campus"
    assert {"gemini-flash-lite", "claude-flagship"} <= set(allowed["decision"]["effective"]["allowed_models"])
    assert allowed["input"]["identity"]["application_id"] == "gct-support-chatbot"

    for name in ("trusted app, untrusted CIDR", "CIDR of another application"):
        assert [r["rule_id"] for r in results[name]["decision"]["reasons"]] == ["AIPEL-NET-001"]
    assert "AIPEL-ID-001" in [
        r["rule_id"] for r in results["unregistered application"]["decision"]["reasons"]
    ]
    assert "AIPEL-ID-003" in [
        r["rule_id"] for r in results["simulated identity needs a tester"]["decision"]["reasons"]
    ]

    for name in ("declared confidential", "DLP-detected subscriber number", "application floor"):
        effective = results[name]["decision"]["effective"]
        assert effective["execution_path"] == "sovereign-direct", name
        assert effective["allowed_models"] == ["gct-qwen-finetuned"], name
        assert (effective["cim"], effective["external_providers"]) == ("bypass", "deny"), name
    dlp_case = results["DLP-detected subscriber number"]["input"]
    assert dlp_case["classification"]["declared"] == "public"
    assert dlp_case["classification"]["dlp"] == dlp_case["classification"]["effective"] == "confidential"
    assert "Customer" not in str(dlp_case)  # the evaluated input carries facts, not prompt text
    assert results["application floor"]["input"]["classification"]["floor"] == "confidential"
    assert "policy.simulate" in await _audit_actions(db, body["version"])


async def test_raw_input_cases_are_supported(make_client, user_token) -> None:
    case = {
        "stage": "request",
        "environment": "test",
        "tenant_id": "gct",
        "identity": {
            "kind": "application",
            "application_id": "gct-noc-assistant",
            "identity_mode": "authenticated",
        },
        "source": {"ip": "10.50.1.1", "mode": "observed"},
        "request": {"task": "analysis"},
        "classification": {"declared": "internal", "effective": "internal"},
        "dlp": {"finding_types": ["SECRET"]},
    }
    async with make_client(user_token(ADMIN)) as client:
        body = (
            await client.post("/v1/admin/policies/test", json={"cases": [{"name": "secret", "input": case}]})
        ).json()
    assert body["results"][0]["decision"]["action"] == "deny"
    assert "GCT-SEC-002" in body["results"][0]["decision"]["matched_rule_ids"]


async def test_draft_is_validated(make_client, user_token) -> None:
    bad = {**BLOCK_ROAMING, "enforce": {"allowed_models": ["no-such-model"]}}
    async with make_client(user_token(ADMIN)) as client:
        response = await client.post("/v1/admin/policies", json={"policies": [bad, bad]})
    body = response.json()
    assert response.status_code == 422
    assert any("unknown model no-such-model" in e for e in body["error"]["details"]["errors"])
    assert any("duplicate policy_id" in e for e in body["error"]["details"]["errors"])


async def test_draft_simulate_publish_and_rollback(services, make_client, user_token, app_client, db) -> None:
    async with services.sessionmaker() as session:
        start = await latest_published(session, "gct")
    assert start is not None
    tagged = chat_body("Roaming?", routing={"tags": ["blocked-by-draft"]})
    try:
        async with make_client(user_token(ADMIN)) as client:
            draft = await client.post(
                "/v1/admin/policies",
                json={"policies": [*seeded_policies(services), BLOCK_ROAMING], "notes": "block a test tag"},
            )
            assert draft.status_code == 201, draft.json()
            detail = draft.json()
            version = detail["version"]
            assert detail["status"] == "draft" and detail["published_version"] == start.version
            assert [
                (c["target_id"], c["change"], c["high_impact"]) for c in detail["changes_from_published"]
            ] == [("TEST-BLOCK-TAG", "added", False)]
            simulated = await client.post(
                "/v1/admin/policies/test",
                json={
                    "version": version,
                    "cases": [
                        {
                            "name": "tagged",
                            "sample": _sample("gct-support-chatbot", "10.20.1.15", tags=["blocked-by-draft"]),
                            "expected_action": "deny",
                        }
                    ],
                },
            )
            assert simulated.json()["passed"] == 1
            assert simulated.json()["bundle_hash"] == detail["bundle_hash"]

        # A draft is not enforced.
        async with app_client("gct-support-chatbot") as client:
            assert (await client.post("/v1/ai/chat", json=tagged)).json()["status"] == "completed"

        # Tightening only: the author may publish it.
        async with make_client(user_token(ADMIN)) as client:
            published = await client.post(
                "/v1/admin/policies/publish", json={"version": version, "approval_note": "block test tag"}
            )
            again = await client.post(
                "/v1/admin/policies/publish", json={"version": version, "approval_note": "again"}
            )
        body = published.json()
        assert published.status_code == 200, body
        assert (body["kind"], body["previous_version"], body["details"]["high_impact"]) == (
            "publish",
            start.version,
            False,
        )
        assert body["bundle_hash"] == detail["bundle_hash"]  # exactly the simulated bundle
        assert again.status_code == 409

        async with app_client("gct-support-chatbot") as client:
            blocked = (await client.post("/v1/ai/chat", json=tagged)).json()
        assert blocked["status"] == "blocked" and blocked["error"]["rule_ids"] == ["TEST-BLOCK-TAG"]
        assert blocked["policy"]["policy_version"] == str(version)

        # Rollback re-pins the earlier version and its original hash.
        async with make_client(user_token(SECOND_ADMIN)) as client:
            rollback = await client.post(
                "/v1/admin/policies/publish", json={"version": start.version, "approval_note": "roll back"}
            )
        assert rollback.status_code == 200, rollback.json()
        assert (rollback.json()["kind"], rollback.json()["bundle_hash"]) == ("rollback", start.bundle_hash)
        async with app_client("gct-support-chatbot") as client:
            restored = (await client.post("/v1/ai/chat", json=tagged)).json()
        assert restored["status"] == "completed"
        assert (restored["policy"]["policy_version"], restored["policy"]["bundle_hash"]) == (
            str(start.version),
            start.bundle_hash,
        )
        assert await _audit_actions(db, version) == ["policy.draft", "policy.simulate", "policy.publish"]
        assert "policy.rollback" in await _audit_actions(db, start.version)
    finally:
        async with services.sessionmaker() as session:
            current = await latest_published(session, "gct")
        if current is not None and current.version != start.version:
            async with make_client(user_token(SECOND_ADMIN)) as client:
                await client.post(
                    "/v1/admin/policies/publish", json={"version": start.version, "approval_note": "restore"}
                )


async def test_high_impact_change_needs_a_second_administrator(
    services, make_client, user_token, policy_versions
) -> None:
    relaxed = [p for p in seeded_policies(services) if p["policy_id"] != "GCT-SEC-002"]
    async with make_client(user_token(ADMIN)) as client:
        draft = (await client.post("/v1/admin/policies", json={"policies": relaxed})).json()
        own = await client.post(
            "/v1/admin/policies/publish",
            json={"version": draft["version"], "approval_note": "remove secrets"},
        )
    assert [(c["target_id"], c["change"], c["high_impact"]) for c in draft["changes_from_published"]] == [
        ("GCT-SEC-002", "removed", True)
    ]
    assert own.status_code == 403 and own.json()["error"]["code"] == "SEPARATION_OF_DUTIES"
    async with make_client(user_token(SECOND_ADMIN)) as client:
        other = await client.post(
            "/v1/admin/policies/publish", json={"version": draft["version"], "approval_note": "reviewed"}
        )
    assert other.status_code == 200 and other.json()["details"]["high_impact"] is True
    # The policy_versions fixture rolls back to the starting version afterwards.


async def test_publish_fails_closed_when_opa_is_down(services, make_client, user_token) -> None:
    async with services.sessionmaker() as session:
        start = await latest_published(session, "gct")
    assert start is not None
    async with make_client(user_token(ADMIN)) as client:
        draft = (
            await client.post(
                "/v1/admin/policies", json={"policies": [*seeded_policies(services), BLOCK_ROAMING]}
            )
        ).json()
    broken = with_overrides(services, opa=OpaPolicyEngine("http://127.0.0.1:9", 0.5))
    async with make_client(user_token(ADMIN), svc=broken) as client:
        published = await client.post(
            "/v1/admin/policies/publish", json={"version": draft["version"], "approval_note": "should fail"}
        )
        simulated = await client.post(
            "/v1/admin/policies/test",
            json={
                "version": draft["version"],
                "cases": [{"name": "x", "sample": _sample("gct-support-chatbot", "10.20.1.15")}],
            },
        )
    assert published.status_code == 503 and published.json()["error"]["code"] == "POLICY_ENGINE_UNAVAILABLE"
    assert simulated.status_code == 503
    async with services.sessionmaker() as session:
        current = await latest_published(session, "gct")
    assert current is not None and current.version == start.version  # nothing was published


async def test_lifecycle_permissions(make_client, user_token) -> None:
    async with make_client(user_token("u-auditor")) as client:
        listed = await client.get("/v1/admin/policies")
        detail = await client.get(f"/v1/admin/policies/{listed.json()[0]['version']}")
        draft = await client.post("/v1/admin/policies", json={"policies": []})
        publish = await client.post(
            "/v1/admin/policies/publish", json={"version": 1, "approval_note": "nope"}
        )
    assert listed.status_code == 200 and detail.status_code == 200
    assert detail.json()["policies"]
    assert draft.status_code == 403 and publish.status_code == 403
    async with make_client(user_token(ADMIN)) as client:
        missing = await client.get("/v1/admin/policies/99999")
    assert missing.status_code == 404


async def test_detail_says_whether_the_viewer_authored_the_draft(services, make_client, user_token) -> None:
    async with make_client(user_token(ADMIN)) as client:
        created = (
            await client.post("/v1/admin/policies", json={"policies": seeded_policies(services)})
        ).json()
        mine = (await client.get(f"/v1/admin/policies/{created['version']}")).json()
    async with make_client(user_token(SECOND_ADMIN)) as client:
        theirs = (await client.get(f"/v1/admin/policies/{created['version']}")).json()
    assert created["authored_by_viewer"] is True and mine["authored_by_viewer"] is True
    assert theirs["authored_by_viewer"] is False
