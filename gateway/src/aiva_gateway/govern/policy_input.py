"""Build the OPA input document (ADR-0005). Shared by the pipeline and the policy simulator.

The input carries policy facts only: identity, source, request hints, classification levels and DLP
finding types. Prompt and evidence text never leave the gateway in a policy input.
"""

from collections.abc import Mapping
from typing import Any

from aiva_gateway.contracts.policy import (
    PolicyClassification,
    PolicyDlpFacts,
    PolicyEgress,
    PolicyIdentity,
    PolicyInput,
    PolicyQuality,
    PolicyRequestFacts,
    PolicySource,
    PolicyTool,
)
from aiva_gateway.core.canonical import sha256_hex
from aiva_gateway.core.enums import PolicyStage
from aiva_gateway.pipeline.context import RunContext
from aiva_gateway.pipeline.interfaces import DlpResult


def tool_arguments_sha256(arguments: Mapping[str, Any]) -> str:
    """Hash of a tool call's arguments (canonical JSON); a tool approval is bound to it (ADR-0012)."""
    return sha256_hex(dict(arguments))


def dlp_facts(prompt: DlpResult, evidence: DlpResult | None) -> PolicyDlpFacts:
    """Prompt injection is enforceable; evidence injection is only a restricting fact (LLD §17)."""
    return PolicyDlpFacts(
        finding_types=sorted(set(prompt.finding_types) | set(evidence.finding_types if evidence else [])),
        injection_detected=prompt.injection_detected,
        injection_indicators=list(prompt.injection_indicators),
        evidence_injection_detected=bool(evidence and evidence.injection_detected),
    )


def build_policy_input(
    ctx: RunContext,
    stage: PolicyStage,
    *,
    egress: PolicyEgress | None = None,
    tool: PolicyTool | None = None,
    quality: PolicyQuality | None = None,
) -> PolicyInput:
    if ctx.classification is None or ctx.dlp is None:
        raise RuntimeError("classify the run before evaluating policy")
    identity = ctx.identity
    request = ctx.request
    return PolicyInput(
        stage=stage,
        environment=ctx.environment,
        tenant_id=ctx.tenant_id,
        identity=PolicyIdentity(
            kind=identity.principal_kind,
            application_id=identity.application_id,
            identity_mode=identity.identity_mode,
            roles=list(identity.roles),
            department_id=ctx.department_id,
        ),
        source=PolicySource(ip=identity.source_ip, mode=identity.source_mode),
        request=PolicyRequestFacts(
            task=request.task or (ctx.scenario.task_category if ctx.scenario else None),
            scenario_id=request.scenario_id,
            tags=request.routing.tags,
            quality_tier=request.quality_tier,
            routing_mode=request.routing.mode,
            preferred_model=request.routing.preferred_model,
            max_output_tokens=request.max_output_tokens,
        ),
        classification=PolicyClassification.model_validate(ctx.classification.model_dump()),
        dlp=dlp_facts(ctx.dlp, ctx.evidence_dlp),
        egress=egress,
        tool=tool,
        quality=quality,
        approvals=[ctx.approval] if ctx.approval else [],
    )
