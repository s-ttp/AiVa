"""Build the OPA data bundle (`data.aiva_bundle`) from the catalog and policy documents (ADR-0005)."""

from typing import Any

from aiva_gateway.catalog import Catalog
from aiva_gateway.catalog.models import PolicyDocument
from aiva_gateway.core.canonical import sha256_hex


def policy_document_data(policy: PolicyDocument) -> dict[str, Any]:
    return {
        "policy_id": policy.policy_id,
        "title": policy.title,
        "priority": policy.priority,
        "match": policy.match.model_dump(mode="json", exclude_none=True),
        "enforce": policy.enforce.model_dump(mode="json", exclude_none=True),
    }


def build_bundle(
    catalog: Catalog, policies: list[PolicyDocument], *, version: str, allow_simulated_ingress: bool
) -> dict[str, Any]:
    """Return the bundle with `meta.bundle_hash` = SHA-256 of everything else in the bundle."""
    body: dict[str, Any] = {
        "networks": {n.id: n.cidr for n in catalog.tenant.trusted_networks},
        "applications": {
            app.id: {
                "enabled": app.enabled,
                "department_id": app.department_id,
                "trusted_network_ids": app.trusted_network_ids,
                "internet_callers": app.internet_callers,
                "allowed_tasks": app.allowed_tasks,
                "allowed_models": app.allowed_models,
                "allowed_tools": app.allowed_tools,
                "classification_floor": app.classification_floor.value,
            }
            for app in catalog.tenant.applications
        },
        "models": {
            m.id: {"role": m.role, "external": m.external, "sovereign": m.sovereign, "enabled": True}
            for m in catalog.models.models
        },
        "tools": {
            t.id: {"access": t.access, "egress": t.egress, "enabled": t.enabled} for t in catalog.tools
        },
        "policies": [
            policy_document_data(p) for p in sorted(policies, key=lambda p: (-p.priority, p.policy_id))
        ],
        # The only models an answer-quality comparison may call (ADR-0023, AIPEL-QUAL-002).
        "quality": {
            "reference_model_id": catalog.models.frontier_baseline,
            "grader_model_id": catalog.models.grader_model,
        },
    }
    meta = {
        "tenant_id": catalog.tenant.tenant.id,
        "version": version,
        "allow_simulated_ingress": allow_simulated_ingress,
    }
    bundle_hash = sha256_hex({"meta": meta, **body})
    return {"meta": {**meta, "bundle_hash": bundle_hash}, **body}
