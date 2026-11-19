"""Approval Queue: approval_required runs, re-evaluation on approve, deny, resume, binding (ADR-0012)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from aiva_gateway.db.models import ApprovalRequestRow, AuditLogRow
from aiva_gateway.govern.policy_engine import OpaPolicyEngine
from aiva_gateway.inference.registry import AdapterRegistry
from aiva_gateway.inference.simulated import SimulatedInferenceAdapter
from tests.conftest import chat_body, console, with_overrides

APPROVAL_POLICY = {
    "policy_id": "TEST-APPROVE-TAG",
    "title": "Approval for a test tag",
    "priority": 100,
    "match": {"tags_any": ["needs-approval"]},
    "enforce": {"action": "approval_required", "reason": "A reviewer must approve tagged requests."},
}
DENY_POLICY = {
    "policy_id": "TEST-DENY-TAG",
    "title": "Deny the test tag",
    "priority": 900,
    "match": {"tags_any": ["needs-approval"]},
    "enforce": {"action": "deny"},
}
OWNER, OTHER_OWNER_DEPT = "u-support-owner", "u-noc-manager"


def _request(content: str = "Explain roaming charges.") -> dict[str, object]:
    return chat_body(content, routing={"tags": ["needs-approval"]})


async def _ask(app_client, payload: dict[str, object]) -> dict[str, object]:
    async with app_client("gct-support-chatbot") as client:
        response = await client.post("/v1/ai/chat", json=payload)
    return {"status_code": response.status_code, **response.json()}


async def _decide(make_client, user_token, approval_id: str, verb: str, user: str = OWNER):
    async with make_client(user_token(user)) as client:
        return await client.post(
            f"/v1/admin/approvals/{approval_id}/{verb}", json={"note": f"{verb} in test"}
        )


async def test_approve_then_resume_once(policy_versions, app_client, make_client, user_token, db) -> None:
    await policy_versions([APPROVAL_POLICY])
    first = await _ask(app_client, _request())
    assert first["status"] == "approval_required" and first["error"]["code"] == "APPROVAL_REQUIRED"
    approval = first["approval"]
    approval_id = approval["approval_id"]
    assert approval["status"] == "pending" and approval["rule_ids"] == ["TEST-APPROVE-TAG"]

    async with app_client("gct-support-chatbot") as client:
        polled = (await client.get(f"/v1/approvals/{approval_id}")).json()
        early = await client.post("/v1/ai/chat", json={**_request(), "approval_id": approval_id})
    assert polled["status"] == "pending" and polled["run_id"] == first["run_id"]
    assert polled["matched_network_id"] == "net-care-campus"
    assert early.status_code == 409 and early.json()["error"]["code"] == "APPROVAL_NOT_USABLE"

    decided = await _decide(make_client, user_token, approval_id, "approve")
    body = decided.json()
    assert decided.status_code == 200, body
    assert body["outcome"] == "approved" and body["re_evaluation"]["action"] == "approval_required"
    assert body["approval"]["approved_grants"] == [{"rule_id": "TEST-APPROVE-TAG", "tool_id": None}]
    assert body["approval"]["decided_by"].startswith("usr_")  # pseudonym, never the user ID

    mismatch = await _ask(app_client, {**_request("A different question."), "approval_id": approval_id})
    assert mismatch["status_code"] == 409 and mismatch["error"]["code"] == "APPROVAL_REQUEST_MISMATCH"

    resumed = await _ask(app_client, {**_request(), "approval_id": approval_id})
    assert resumed["status"] == "completed", resumed
    assert resumed["approval"]["satisfied_by"] == approval_id
    assert resumed["policy"]["action"] == "allow"

    replay = await _ask(app_client, {**_request(), "approval_id": approval_id})
    assert replay["status_code"] == 409 and replay["error"]["details"]["status"] == "consumed"
    row = await db.get(ApprovalRequestRow, approval_id)
    await db.refresh(row)
    assert row is not None and row.consumed_by_run_id == resumed["run_id"]
    actions = (
        await db.execute(select(AuditLogRow.action).where(AuditLogRow.target_id == approval_id))
    ).scalars()
    assert set(actions) == {"approval.request", "approval.approved", "approval.consume"}


async def test_approve_re_evaluates_against_current_policy(
    policy_versions, app_client, make_client, user_token
) -> None:
    await policy_versions([APPROVAL_POLICY])
    approval_id = (await _ask(app_client, _request()))["approval"]["approval_id"]
    newer = await policy_versions([APPROVAL_POLICY, DENY_POLICY])
    decided = (await _decide(make_client, user_token, approval_id, "approve")).json()
    assert decided["outcome"] == "closed_by_policy"
    assert decided["approval"]["status"] == "denied"
    assert decided["approval"]["policy_version_at_decision"] == str(newer)
    assert "TEST-DENY-TAG" in [r["rule_id"] for r in decided["re_evaluation"]["reasons"]]
    resumed = await _ask(app_client, {**_request(), "approval_id": approval_id})
    assert resumed["status_code"] == 409


async def test_deny_closes_the_request(policy_versions, app_client, make_client, user_token) -> None:
    await policy_versions([APPROVAL_POLICY])
    approval_id = (await _ask(app_client, _request()))["approval"]["approval_id"]
    denied = (await _decide(make_client, user_token, approval_id, "deny")).json()
    assert (denied["outcome"], denied["approval"]["status"]) == ("denied", "denied")
    again = await _decide(make_client, user_token, approval_id, "approve")
    assert again.status_code == 409 and again.json()["error"]["code"] == "APPROVAL_ALREADY_DECIDED"


async def test_expired_approvals_cannot_be_used(
    policy_versions, app_client, make_client, user_token, db
) -> None:
    await policy_versions([APPROVAL_POLICY])
    approval_id = (await _ask(app_client, _request()))["approval"]["approval_id"]
    await _decide(make_client, user_token, approval_id, "approve")
    await db.execute(
        update(ApprovalRequestRow)
        .where(ApprovalRequestRow.id == approval_id)
        .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await db.commit()
    resumed = await _ask(app_client, {**_request(), "approval_id": approval_id})
    assert resumed["status_code"] == 409 and resumed["error"]["details"]["status"] == "expired"


async def test_retryable_failure_returns_the_approval(
    services, policy_versions, app_client, make_client, user_token
) -> None:
    await policy_versions([APPROVAL_POLICY])
    approval_id = (await _ask(app_client, _request()))["approval"]["approval_id"]
    await _decide(make_client, user_token, approval_id, "approve")
    down = SimulatedInferenceAdapter(unavailable_models={m.id for m in services.catalog.models.models})
    broken = with_overrides(services, adapters=AdapterRegistry(services.catalog, adapters={}, default=down))
    async with app_client("gct-support-chatbot", svc=broken) as client:
        failed = (await client.post("/v1/ai/chat", json={**_request(), "approval_id": approval_id})).json()
    assert failed["status"] == "failed" and failed["error"]["retryable"]
    resumed = await _ask(app_client, {**_request(), "approval_id": approval_id})
    assert resumed["status"] == "completed"


async def test_approval_queue_access(
    policy_versions, app_client, make_client, user_token, tester_client
) -> None:
    await policy_versions([APPROVAL_POLICY])
    approval_id = (await _ask(app_client, _request()))["approval"]["approval_id"]

    async def listed(user: str) -> int | list[str]:
        async with make_client(user_token(user)) as client:
            response = await client.get("/v1/admin/approvals")
        if response.status_code != 200:
            return response.status_code
        return [a["approval_id"] for a in response.json()["approvals"]]

    assert approval_id in await listed("u-tenant-admin")
    assert approval_id in await listed("u-auditor")
    assert approval_id in await listed("u-care-manager")  # customer-care department
    assert approval_id not in await listed(OTHER_OWNER_DEPT)  # other department
    assert approval_id not in await listed("u-finance")  # finance sees budget approvals only
    assert await listed("u-tester") == 403
    async with app_client("gct-coding-assistant") as client:
        assert (await client.get(f"/v1/approvals/{approval_id}")).status_code == 404
    async with tester_client() as client:
        assert (await client.get(f"/v1/approvals/{approval_id}")).status_code == 404  # not their request
    assert (await _decide(make_client, user_token, approval_id, "approve", "u-auditor")).status_code == 403
    assert (
        await _decide(make_client, user_token, approval_id, "approve", "u-care-manager")
    ).status_code == 403


async def test_tester_cannot_resume_another_principals_approval(
    policy_versions, app_client, tester_client
) -> None:
    await policy_versions([APPROVAL_POLICY])
    approval_id = (await _ask(app_client, _request()))["approval"]["approval_id"]
    payload = {**_request(), **console("gct-support-chatbot"), "approval_id": approval_id}
    async with tester_client() as client:
        response = await client.post("/v1/ai/chat", json=payload)
    assert response.status_code == 404 and response.json()["error"]["code"] == "APPROVAL_NOT_FOUND"


async def test_approve_fails_closed_when_policy_engine_is_down(
    services, policy_versions, app_client, make_client, user_token, db
) -> None:
    await policy_versions([APPROVAL_POLICY])
    approval_id = (await _ask(app_client, _request()))["approval"]["approval_id"]
    broken = with_overrides(services, policy_engine=OpaPolicyEngine("http://127.0.0.1:9", 0.5))
    async with make_client(user_token(OWNER), svc=broken) as client:
        response = await client.post(f"/v1/admin/approvals/{approval_id}/approve", json={"note": "approve"})
    assert response.status_code == 503 and response.json()["error"]["code"] == "POLICY_ENGINE_UNAVAILABLE"
    row = await db.get(ApprovalRequestRow, approval_id)
    await db.refresh(row)
    assert row is not None and row.status == "pending" and row.decided_by is None


async def test_approval_records_hold_no_prompt_text(policy_versions, app_client, db) -> None:
    await policy_versions([APPROVAL_POLICY])
    approval_id = (await _ask(app_client, _request("CANARY-approval-31 roaming")))["approval"]["approval_id"]
    row = await db.get(ApprovalRequestRow, approval_id)
    assert row is not None and "CANARY-approval-31" not in str(row.policy_input) + str(row.reasons)


async def test_budget_exhaustion_becomes_a_budget_approval(
    services, app_client, make_client, user_token, db
) -> None:
    """Budget `on_exhaustion: approval_required` lands in the queue; resuming hands the approval to FinOps."""
    from aiva_gateway.core.ids import ulid
    from aiva_gateway.finops.quota import RedisQuotaService

    workflow = f"wf-{ulid()[-12:].lower()}"
    budget = {
        "scope_type": "workflow",
        "scope_id": f"gct-support-chatbot/{workflow}",
        "period": "workflow",
        "cap_micros": 0,
        "on_exhaustion": "approval_required",
    }
    async with make_client(user_token("u-finance")) as client:
        created = await client.post("/v1/admin/budgets", json=budget)
    assert created.status_code == 201, created.text
    request = chat_body(
        "What are the Europe roaming rates?",
        baseline_comparison=True,
        max_output_tokens=16,
        workflow_id=workflow,
    )
    first = await _ask(app_client, request)
    assert first["status"] == "approval_required" and first["error"]["code"] == "QUOTA_EXCEEDED"
    approval_id = first["approval"]["approval_id"]
    assert first["approval"]["rule_ids"] == [created.json()["id"]]

    row = await db.get(ApprovalRequestRow, approval_id)
    assert row is not None and row.kind == "budget" and (row.approved_amount_micros or 0) > 0
    assert (await _decide(make_client, user_token, approval_id, "approve")).status_code == 403  # app owner
    decided = (await _decide(make_client, user_token, approval_id, "approve", "u-finance")).json()
    assert decided["outcome"] == "approved"
    assert decided["approval"]["kind"] == "budget"
    assert decided["approval"]["approved_amount_micros"] == row.approved_amount_micros
    assert {"rule_id": created.json()["id"], "tool_id": None} in decided["approval"]["approved_grants"]

    class Capturing(RedisQuotaService):
        seen: list[str | None] = []

        async def reserve(self, request):  # type: ignore[no-untyped-def]
            Capturing.seen.append(request.approval_id)
            return await super().reserve(request)

    quota = Capturing(services.redis, services.sessionmaker, services.prices)
    async with app_client("gct-support-chatbot", svc=with_overrides(services, quota=quota)) as client:
        resumed = (await client.post("/v1/ai/chat", json={**request, "approval_id": approval_id})).json()
    assert Capturing.seen == [approval_id]
    # FinOps admits the approved overspend for this one request (ADR-0014).
    assert resumed["status"] == "completed", resumed.get("error")


async def test_viewer_flags_follow_the_decide_rules(
    services, policy_versions, make_client, user_token, tester_client, db
) -> None:
    """A tester who also owns the application cannot approve their own request; the flags say so."""
    from aiva_gateway.auth.tokens import pseudonymise
    from aiva_gateway.db.models import PrincipalRow

    dual = "u-test-dual-role"
    await db.merge(
        PrincipalRow(
            id=dual,
            tenant_id="gct",
            pseudonym=pseudonymise(services.settings.pseudonym_key.get_secret_value(), dual),
            display_name="Tester and owner (test)",
            roles=["tester", "application_owner"],
            owned_application_ids=["gct-support-chatbot"],
        )
    )
    await db.commit()
    await policy_versions([APPROVAL_POLICY])
    payload = {**_request(), **console("gct-support-chatbot")}
    async with make_client(user_token(dual)) as client:
        first = (await client.post("/v1/ai/chat", json=payload)).json()
        approval_id = first["approval"]["approval_id"]
        own = (await client.get(f"/v1/approvals/{approval_id}")).json()
        refused = await client.post(f"/v1/admin/approvals/{approval_id}/approve", json={"note": "self"})
    assert (own["requested_by_viewer"], own["viewer_can_decide"]) == (True, False)
    assert refused.status_code == 403

    async def flags(user: str) -> tuple[bool, bool]:
        async with make_client(user_token(user)) as client:
            body = (await client.get(f"/v1/approvals/{approval_id}")).json()
        return body["requested_by_viewer"], body["viewer_can_decide"]

    assert await flags(OWNER) == (False, True)
    assert await flags("u-tenant-admin") == (False, True)
    assert await flags("u-auditor") == (False, False)
    async with make_client(user_token("u-tenant-admin")) as client:
        queue = (await client.get("/v1/admin/approvals")).json()["approvals"]
    assert next(a for a in queue if a["approval_id"] == approval_id)["viewer_can_decide"] is True
    decided = (await _decide(make_client, user_token, approval_id, "approve")).json()
    assert decided["approval"]["viewer_can_decide"] is False  # no longer pending
