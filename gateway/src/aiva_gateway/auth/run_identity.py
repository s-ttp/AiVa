"""Decide who a chat run executes as (ADR-0007), and whether it may run live (ADR-0019)."""

from typing import Literal

from aiva_gateway.auth.ingress import is_valid_ip
from aiva_gateway.auth.principal import Caller, RunIdentity
from aiva_gateway.auth.rbac import Permission, granted, identity_granted
from aiva_gateway.config import Settings
from aiva_gateway.contracts.messages import TestProfile
from aiva_gateway.core.enums import IdentityMode, SourceMode
from aiva_gateway.core.errors import PermissionDeniedError, ValidationFailedError


def authorize_run_caller(caller: Caller) -> None:
    """Whether this caller may start a run (ADR-0007, ADR-0019): an application credential, or a user
    granted `chat:test` (testers, public visitors and administrators). Run routes check it before their body
    is validated (`api.deps.RunCallerDep`), so a user who may not run is refused 403, never told 422 about a
    body they could not send anyway; `resolve_run_identity` applies the same check."""
    if caller.kind != "application" and not granted(caller, Permission.CHAT_TEST):
        raise PermissionDeniedError(
            "Users call AiVa through an application.",
            remediation="Use an application credential, or sign in as a tester to use the AiVa Test Console.",
        )


def resolve_run_identity(
    caller: Caller, profile: TestProfile | None, observed_ip: str | None, settings: Settings
) -> RunIdentity:
    if caller.kind == "application":
        if profile is not None:
            raise PermissionDeniedError(
                "Test profiles are only available to testers in the AiVa Test Console.",
                remediation="Remove test_profile when calling with an application credential.",
            )
        assert caller.application_id is not None
        return RunIdentity(
            application_id=caller.application_id,
            identity_mode=IdentityMode.AUTHENTICATED,
            source_ip=observed_ip,
            source_mode=SourceMode.OBSERVED,
            principal_kind="application",
            principal_pseudonym=None,
            roles=(),
        )
    authorize_run_caller(caller)
    if profile is None:
        raise ValidationFailedError("test_profile is required for Test Console requests.")
    if not settings.allow_simulated_ingress:
        raise PermissionDeniedError("Simulated test identities are disabled in this environment.")
    if profile.simulated_source_ip is not None and not is_valid_ip(profile.simulated_source_ip):
        raise ValidationFailedError("simulated_source_ip is not a valid IP address.")
    simulated_ip = profile.simulated_source_ip
    return RunIdentity(
        application_id=profile.application_id,
        identity_mode=IdentityMode.SIMULATED,
        source_ip=simulated_ip or observed_ip,
        source_mode=SourceMode.SIMULATED if simulated_ip else SourceMode.OBSERVED,
        principal_kind="user",
        principal_pseudonym=caller.pseudonym,
        roles=tuple(sorted(r.value for r in caller.roles)),
        principal_department_id=caller.department_id,
    )


ExecutionMode = Literal["replay", "live"]


def authorize_execution_mode(identity: RunIdentity, requested: ExecutionMode) -> None:
    """Refuse a live run the principal may not start (ADR-0019); replay is always allowed. The orchestrator
    calls it when a request asks for `live`. An application credential keeps the gateway's configured mode.
    A permitted visitor's live run is then limited by FinOps (per-visitor quota, public-demo budget)."""
    if requested != "live" or identity.principal_kind == "application":
        return
    if not identity_granted(identity, Permission.CHAT_LIVE):
        raise PermissionDeniedError(
            "Live runs are not available to your role; the recorded replay is.",
            code="LIVE_NOT_PERMITTED",
            remediation="Run the replay, or ask an administrator for a role that grants 'chat:live'.",
        )
