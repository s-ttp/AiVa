"""Recorded tool calls: a tool approval executes the exact call it was recorded for (ADR-0012 amendment)."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from aiva_gateway.auth.principal import RunIdentity
from aiva_gateway.contracts.chat import ChatRequest
from aiva_gateway.contracts.policy import PolicyApproval
from aiva_gateway.core.enums import IdentityMode, PolicyAction, SourceMode
from aiva_gateway.core.errors import ConflictError, DependencyUnavailableError, NotFoundError
from aiva_gateway.db.models import ApprovalRequestRow, AuditLogRow, PolicyEventRow
from aiva_gateway.govern.approvals import authorize_recorded_tool_call, release_approval
from aiva_gateway.govern.policy_engine import OpaPolicyEngine
from aiva_gateway.govern.policy_input import tool_arguments_sha256
from aiva_gateway.pipeline.orchestrator import Orchestrator
from aiva_gateway.pipeline.recorder import persist_run
from aiva_gateway.services import Services
from tests.conftest import ADMIN, PEERS, with_overrides

AGENT = "gct-incident-agent"
SEND = "notification.send"
ARGS = {"draft_id": "NTF-7781-01"}
IDENTITY = RunIdentity(
    application_id=AGENT,
    identity_mode=IdentityMode.AUTHENTICATED,
    source_ip=PEERS[AGENT],
    source_mode=SourceMode.OBSERVED,
    principal_kind="application",
    principal_pseudonym=None,
    roles=(),
)
DENY_SEND = {
    "policy_id": "TEST-DENY-SEND",
    "title": "Deny customer notices",
    "priority": 950,
    "match": {"tool_any": [SEND]},
    "enforce": {"action": "deny", "reason": "Customer notices are paused."},
}


async def _record(services: Services, arguments: dict[str, str] | None = ARGS) -> str:
    """Run a governed step, record `notification.send` (held by GCT-TOOL-005) and return its approval."""
    request = ChatRequest.model_validate(
        {
            "messages": [{"role": "user", "content": "Triage synthetic incident INC-7781."}],
            "scenario_id": "agentic-workflow",
        }
    )
    orchestrator = Orchestrator(services)
    ctx = orchestrator.new_context(request, IDENTITY, "gct")
    spans = await orchestrator.run(ctx)
    decision = await orchestrator.authorize_tool_call(ctx, SEND, arguments=arguments)
    assert decision.action == PolicyAction.APPROVAL_REQUIRED
    async with services.sessionmaker() as session, session.begin():
        await persist_run(session, services.catalog, ctx, spans)
    approval_id = ctx.policy_events[-1].approval_id
    assert approval_id is not None
    return approval_id


async def _approve(make_client, user_token, approval_id: str) -> None:  # type: ignore[no-untyped-def]
    async with make_client(user_token(ADMIN)) as client:
        response = await client.post(f"/v1/admin/approvals/{approval_id}/approve", json={"note": "send it"})
    assert response.status_code == 200 and response.json()["outcome"] == "approved", response.json()


async def _authorize(
    services: Services,
    approval_id: str,
    *,
    identity: RunIdentity = IDENTITY,
    tool_id: str = SEND,
    arguments: dict[str, str] = ARGS,
):  # type: ignore[no-untyped-def]
    async with services.sessionmaker() as session, session.begin():
        return await authorize_recorded_tool_call(
            session,
            services.policy_engine,
            services.catalog,
            approval_id=approval_id,
            tenant_id="gct",
            identity=identity,
            tool_id=tool_id,
            arguments=arguments,
        )


async def _row(services: Services, approval_id: str) -> ApprovalRequestRow:
    async with services.sessionmaker() as session:
        row = await session.get(ApprovalRequestRow, approval_id)
        assert row is not None
        return row


async def _tool_events(services: Services, run_id: str) -> list[tuple[str | None, str]]:
    async with services.sessionmaker() as session:
        rows = await session.scalars(
            select(PolicyEventRow)
            .where(PolicyEventRow.run_id == run_id, PolicyEventRow.stage == "tool")
            .order_by(PolicyEventRow.sequence)
        )
        return [(r.tool_id, r.action) for r in rows]


async def _audit_actions(services: Services, approval_id: str) -> list[str]:
    async with services.sessionmaker() as session:
        rows = await session.scalars(
            select(AuditLogRow).where(AuditLogRow.target_id == approval_id).order_by(AuditLogRow.id)
        )
        return [r.action for r in rows]


async def test_a_tool_approval_is_bound_to_the_recorded_arguments(services, make_client, user_token) -> None:  # type: ignore[no-untyped-def]
    approval_id = await _record(services)
    assert (await _row(services, approval_id)).arguments_sha256 == tool_arguments_sha256(ARGS)
    async with make_client(user_token(ADMIN)) as client:
        shown = (await client.get(f"/v1/approvals/{approval_id}")).json()
    assert (shown["tool_id"], shown["arguments_sha256"]) == (SEND, tool_arguments_sha256(ARGS))


async def test_an_approved_recorded_call_is_allowed_once(services, make_client, user_token) -> None:  # type: ignore[no-untyped-def]
    approval_id = await _record(services)
    await _approve(make_client, user_token, approval_id)

    result = await _authorize(services, approval_id)
    assert (result.decision.action, result.consumed) == (PolicyAction.ALLOW, True)
    row = await _row(services, approval_id)
    assert (row.status, row.consumed_by_run_id) == ("consumed", row.run_id) == ("consumed", result.run_id)
    # The decision is the run's latest tool-stage policy event, after the recorded approval_required.
    assert (await _tool_events(services, row.run_id))[-2:] == [(SEND, "approval_required"), (SEND, "allow")]
    assert (await _audit_actions(services, approval_id))[-1] == "approval.consume"

    with pytest.raises(ConflictError) as again:
        await _authorize(services, approval_id)
    assert again.value.code == "APPROVAL_NOT_USABLE" and again.value.status_code == 409


@pytest.mark.parametrize(
    ("tool_id", "arguments"),
    [(SEND, {"draft_id": "NTF-7781-02"}), ("notification.draft", ARGS), (SEND, {**ARGS, "channel": "sms"})],
)
async def test_another_call_cannot_use_the_approval(
    services, make_client, user_token, tool_id: str, arguments: dict[str, str]
) -> None:  # type: ignore[no-untyped-def]
    approval_id = await _record(services)
    await _approve(make_client, user_token, approval_id)
    with pytest.raises(ConflictError) as refused:
        await _authorize(services, approval_id, tool_id=tool_id, arguments=arguments)
    assert refused.value.code == "APPROVAL_REQUEST_MISMATCH" and refused.value.status_code == 409
    assert (await _row(services, approval_id)).status == "approved"


async def test_an_approval_recorded_without_arguments_cannot_execute_a_call(
    services, make_client, user_token
) -> None:  # type: ignore[no-untyped-def]
    approval_id = await _record(services, arguments=None)
    await _approve(make_client, user_token, approval_id)
    with pytest.raises(ConflictError) as refused:
        await _authorize(services, approval_id)
    assert refused.value.code == "APPROVAL_REQUEST_MISMATCH"


async def test_pending_and_expired_approvals_are_not_usable(services, make_client, user_token) -> None:  # type: ignore[no-untyped-def]
    approval_id = await _record(services)
    with pytest.raises(ConflictError) as pending:
        await _authorize(services, approval_id)
    assert pending.value.code == "APPROVAL_NOT_USABLE" and pending.value.details == {"status": "pending"}

    await _approve(make_client, user_token, approval_id)
    async with services.sessionmaker() as session, session.begin():
        await session.execute(
            update(ApprovalRequestRow)
            .where(ApprovalRequestRow.id == approval_id)
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
    with pytest.raises(ConflictError) as expired:
        await _authorize(services, approval_id)
    assert expired.value.details == {"status": "expired"}


async def test_another_principal_cannot_find_the_approval(services, make_client, user_token) -> None:  # type: ignore[no-untyped-def]
    approval_id = await _record(services)
    await _approve(make_client, user_token, approval_id)
    other = replace(IDENTITY, application_id="gct-noc-assistant", source_ip=PEERS["gct-noc-assistant"])
    with pytest.raises(NotFoundError):
        await _authorize(services, approval_id, identity=other)
    assert (await _row(services, approval_id)).status == "approved"


async def test_an_untrusted_source_is_refused_and_nothing_is_consumed(
    services, make_client, user_token
) -> None:  # type: ignore[no-untyped-def]
    approval_id = await _record(services)
    await _approve(make_client, user_token, approval_id)
    result = await _authorize(services, approval_id, identity=replace(IDENTITY, source_ip="203.0.113.9"))
    assert (result.decision.action, result.consumed) == (PolicyAction.DENY, False)
    assert (await _row(services, approval_id)).status == "approved"
    assert (await _audit_actions(services, approval_id))[-1] == "approval.recheck_refused"


async def test_a_stricter_policy_published_after_approval_refuses_the_call(
    services, policy_versions, make_client, user_token
) -> None:  # type: ignore[no-untyped-def]
    approval_id = await _record(services)
    await _approve(make_client, user_token, approval_id)
    version = await policy_versions([DENY_SEND])
    result = await _authorize(services, approval_id)
    assert result.decision.action == PolicyAction.DENY and not result.consumed
    assert result.decision.policy_version == str(version)
    assert "TEST-DENY-SEND" in {r.rule_id for r in result.decision.reasons}
    assert (await _row(services, approval_id)).status == "approved"


async def test_policy_engine_down_fails_closed_and_consumes_nothing(
    services, make_client, user_token
) -> None:  # type: ignore[no-untyped-def]
    approval_id = await _record(services)
    await _approve(make_client, user_token, approval_id)
    run_id = (await _row(services, approval_id)).run_id
    events_before = await _tool_events(services, run_id)
    broken = with_overrides(services, policy_engine=OpaPolicyEngine("http://127.0.0.1:9", 0.5))
    with pytest.raises(DependencyUnavailableError) as raised:
        await _authorize(broken, approval_id)
    assert raised.value.status_code == 503
    assert (await _row(services, approval_id)).status == "approved"
    assert await _tool_events(services, run_id) == events_before


async def test_only_one_concurrent_authorization_consumes_the_approval(
    services, make_client, user_token
) -> None:  # type: ignore[no-untyped-def]
    approval_id = await _record(services)
    await _approve(make_client, user_token, approval_id)
    results = await asyncio.gather(
        _authorize(services, approval_id), _authorize(services, approval_id), return_exceptions=True
    )
    allowed = [r for r in results if not isinstance(r, BaseException) and r.consumed]
    refused = [r for r in results if isinstance(r, ConflictError)]
    assert len(allowed) == 1 and len(refused) == 1
    assert refused[0].code == "APPROVAL_NOT_USABLE"
    assert (await _audit_actions(services, approval_id)).count("approval.consume") == 1


async def test_releasing_a_claimed_approval_is_audited(services, make_client, user_token) -> None:  # type: ignore[no-untyped-def]
    approval_id = await _record(services)
    await _approve(make_client, user_token, approval_id)
    async with services.sessionmaker() as session, session.begin():
        await session.execute(
            update(ApprovalRequestRow)
            .where(ApprovalRequestRow.id == approval_id)
            .values(status="consumed", consumed_by_run_id="run_claimer", consumed_at=datetime.now(UTC))
        )
    async with services.sessionmaker() as session, session.begin():
        await release_approval(session, PolicyApproval(approval_id=approval_id), run_id="run_other")
    assert (await _row(services, approval_id)).status == "consumed"  # only the claiming run releases
    async with services.sessionmaker() as session, session.begin():
        await release_approval(session, PolicyApproval(approval_id=approval_id), run_id="run_claimer")
    assert (await _row(services, approval_id)).status == "approved"
    assert (await _audit_actions(services, approval_id)).count("approval.release") == 1
