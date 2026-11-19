"""Load and cross-validate `config/`. Invalid configuration fails fast at startup."""

import ipaddress
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel

from aiva_gateway.catalog.models import (
    MIN_QUALITY_RANK,
    Application,
    ContextProfile,
    ContextProfiles,
    ModelRegistry,
    ModelSpec,
    PolicyDocument,
    RateCard,
    RateCards,
    Scenario,
    ScenarioCatalog,
    Taxonomy,
    TenantConfig,
    ToolRateCard,
    ToolRegistry,
    ToolSpec,
)


def _load[M: BaseModel](path: Path, model: type[M]) -> M:
    with path.open(encoding="utf-8") as handle:
        return model.model_validate(yaml.safe_load(handle))


@dataclass(frozen=True)
class Catalog:
    tenant: TenantConfig
    models: ModelRegistry
    rate_cards: RateCards
    policies: list[PolicyDocument]
    scenarios: list[Scenario]
    taxonomy: Taxonomy
    context_profiles: list[ContextProfile]
    tools: list[ToolSpec]
    # Task categories whose CIM-assisted runs do not search the knowledge base or the web.
    retrieval_skip_tasks: frozenset[str] = frozenset()

    def application(self, application_id: str) -> Application | None:
        return next((app for app in self.tenant.applications if app.id == application_id), None)

    def model(self, model_id: str) -> ModelSpec | None:
        return next((m for m in self.models.models if m.id == model_id), None)

    def tool(self, tool_id: str) -> ToolSpec | None:
        return next((t for t in self.tools if t.id == tool_id), None)

    def rate_card(self, rate_card_id: str) -> RateCard | None:
        return next((rc for rc in self.rate_cards.rate_cards if rc.id == rate_card_id), None)

    def tool_rate_card(self, tool_id: str) -> ToolRateCard | None:
        return next((rc for rc in self.rate_cards.tool_rate_cards if rc.tool_id == tool_id), None)

    def scenario(self, scenario_id: str) -> Scenario | None:
        return next((s for s in self.scenarios if s.id == scenario_id), None)

    def context_profile(self, profile_id: str) -> ContextProfile | None:
        return next((p for p in self.context_profiles if p.id == profile_id), None)

    def department_of(self, application_id: str) -> str | None:
        app = self.application(application_id)
        return app.department_id if app else None

    def cost_center_of(self, department_id: str | None) -> str | None:
        dept = next((d for d in self.tenant.departments if d.id == department_id), None)
        return dept.cost_center_id if dept else None

    def business_function_of(self, department_id: str | None) -> str | None:
        dept = next((d for d in self.tenant.departments if d.id == department_id), None)
        return dept.business_function if dept else None

    def application_cidrs(self, application_id: str) -> list[tuple[str, str]]:
        """(trusted network id, CIDR) pairs the application may be called from."""
        app = self.application(application_id)
        if app is None:
            return []
        networks = {n.id: n for n in self.tenant.trusted_networks}
        return [(nid, networks[nid].cidr) for nid in app.trusted_network_ids]


def load_catalog(config_dir: Path) -> Catalog:
    profiles = _load(config_dir / "context_profiles.yaml", ContextProfiles)
    catalog = Catalog(
        tenant=_load(config_dir / "tenant.yaml", TenantConfig),
        models=_load(config_dir / "models.yaml", ModelRegistry),
        rate_cards=_load(config_dir / "rate_cards.yaml", RateCards),
        policies=sorted(
            (_load(path, PolicyDocument) for path in sorted((config_dir / "policies").glob("*.yaml"))),
            key=lambda p: (-p.priority, p.policy_id),
        ),
        scenarios=_load(config_dir / "scenarios.yaml", ScenarioCatalog).scenarios,
        taxonomy=_load(config_dir / "taxonomy.yaml", Taxonomy),
        context_profiles=profiles.profiles,
        tools=_load(config_dir / "tools.yaml", ToolRegistry).tools,
        retrieval_skip_tasks=frozenset(profiles.retrieval_skip_tasks),
    )
    _validate_references(catalog)
    return catalog


# Each live demo use case asks one of three questions, its sample or one of two alternates (owner decision,
# 2026-10-25).
LIVE_DEMO_ALTERNATES = 2


def _alternate_prompt_errors(scenario: Scenario) -> list[str]:
    """Alternates belong to live demo use cases only, two each, every one non-blank and distinct from the
    sample and from each other (their validity as requests is `_sample_errors`'s)."""
    alternates, name = scenario.alternate_prompts, f"scenario {scenario.id}"
    if not scenario.live_demo_use_case:
        return [f"{name}: alternate prompts are for live demo use cases only"] if alternates else []
    errors: list[str] = []
    if len(alternates) != LIVE_DEMO_ALTERNATES:
        errors.append(f"{name}: a live demo use case has exactly {LIVE_DEMO_ALTERNATES} alternate prompts")
    normalized = [" ".join(p.split()).casefold() for p in [scenario.sample_prompt, *alternates]]
    if any(not p for p in normalized[1:]):
        errors.append(f"{name}: an alternate prompt is blank")
    elif len(set(normalized)) != len(normalized):
        errors.append(f"{name}: alternate prompts must differ from the sample and from each other")
    return errors


def _sample_errors(scenario: Scenario) -> list[str]:
    """Scenario samples must be valid requests, and every critical figure must appear verbatim in the
    evidence item it cites (so the demo's figure checks test real source text)."""
    from pydantic import ValidationError

    from aiva_gateway.contracts.agents import AgentWorkflowRequest
    from aiva_gateway.contracts.artifacts import ArtifactCreateRequest
    from aiva_gateway.contracts.chat import ChatRequest

    errors: list[str] = []
    artifact, workflow = scenario.sample_artifact, scenario.sample_workflow
    try:
        # The default chat request, as the Test Console sends it: prompt, evidence, tier and output budget.
        # A live demo use case's alternate questions are sent the same way, so each must be as valid.
        for prompt in [scenario.sample_prompt, *scenario.alternate_prompts]:
            ChatRequest.model_validate(
                {
                    "messages": [{"role": "user", "content": prompt}],
                    "evidence": [e.model_dump() for e in scenario.sample_evidence or []],
                    "quality_tier": scenario.quality_tier,
                    "max_output_tokens": scenario.max_output_tokens,
                }
            )
        if artifact is not None:
            ArtifactCreateRequest.model_validate(artifact.model_dump())
        if workflow is not None:
            AgentWorkflowRequest.model_validate(workflow.model_dump(exclude_none=True))
    except ValidationError as exc:
        errors.append(f"scenario {scenario.id}: invalid sample request: {exc.errors()[0]['msg']}")
    errors.extend(_alternate_prompt_errors(scenario))
    if scenario.sample_evidence is not None and len({e.id for e in scenario.sample_evidence}) != len(
        scenario.sample_evidence
    ):
        errors.append(f"scenario {scenario.id}: duplicate sample evidence IDs")
    if artifact is not None:
        evidence = {e.id: e.text for e in artifact.evidence}
        if len(evidence) != len(artifact.evidence):
            errors.append(f"scenario {scenario.id}: duplicate sample evidence IDs")
        for figure in artifact.figures:
            source = evidence.get(figure.source_id)
            if source is None:
                errors.append(f"scenario {scenario.id}: figure {figure.id} cites unknown {figure.source_id}")
            elif figure.value not in source:
                errors.append(f"scenario {scenario.id}: figure {figure.id} is not in {figure.source_id}")
    return errors


def _about_errors(model: ModelSpec) -> list[str]:
    """Every model carries its info balloon's facts (ADR-0021): a summary and where it runs; a dedicated
    endpoint also its machine, its server and its scale-to-zero window."""
    errors = []
    if not model.summary:
        errors.append(f"model {model.id}: summary is required")
    if model.hosting is None:
        errors.append(f"model {model.id}: hosting is required")
    elif model.provider == "hf-endpoint":
        hosting = model.hosting
        missing = [
            name
            for name, value in (
                ("instance", hosting.instance),
                ("serving", hosting.serving),
                ("scale_to_zero_minutes", hosting.scale_to_zero_minutes),
            )
            if value is None
        ]
        errors.extend(f"model {model.id}: hosting.{name} is required for an endpoint" for name in missing)
    elif model.hosting.scale_to_zero_minutes is not None:
        errors.append(f"model {model.id}: only a dedicated endpoint scales to zero")
    return errors


def _validate_references(catalog: Catalog) -> None:
    errors = policy_reference_errors(catalog, catalog.policies)
    model_ids = {m.id for m in catalog.models.models}
    network_ids = {n.id for n in catalog.tenant.trusted_networks}
    department_ids = {d.id for d in catalog.tenant.departments}
    cost_center_ids = {c.id for c in catalog.tenant.cost_centers}
    rate_card_ids = {rc.id for rc in catalog.rate_cards.rate_cards}
    application_ids = {a.id for a in catalog.tenant.applications}
    profile_ids = {p.id for p in catalog.context_profiles}
    tool_ids = {t.id for t in catalog.tools}

    for net in catalog.tenant.trusted_networks:
        try:
            ipaddress.ip_network(net.cidr, strict=True)
        except ValueError as exc:
            errors.append(f"trusted network {net.id}: {exc}")
    for dept in catalog.tenant.departments:
        if dept.cost_center_id not in cost_center_ids:
            errors.append(f"department {dept.id}: unknown cost center {dept.cost_center_id}")
    for app in catalog.tenant.applications:
        if app.department_id not in department_ids:
            errors.append(f"application {app.id}: unknown department {app.department_id}")
        errors.extend(
            f"application {app.id}: unknown model {m}" for m in app.allowed_models if m not in model_ids
        )
        errors.extend(
            f"application {app.id}: unknown network {n}"
            for n in app.trusted_network_ids
            if n not in network_ids
        )
        errors.extend(
            f"application {app.id}: unknown tool {t}" for t in app.allowed_tools if t not in tool_ids
        )
    priced_tools = [rc.tool_id for rc in catalog.rate_cards.tool_rate_cards]
    errors.extend(f"tool rate card: unknown tool {t}" for t in priced_tools if t not in tool_ids)
    errors.extend(
        f"tool rate card: {t} is priced more than once"
        for t in sorted(set(priced_tools))
        if priced_tools.count(t) > 1
    )
    for model in catalog.models.models:
        if model.rate_card_id not in rate_card_ids:
            errors.append(f"model {model.id}: unknown rate card {model.rate_card_id}")
        if model.sovereign and model.external:
            errors.append(f"model {model.id}: a sovereign model cannot be external")
        errors.extend(
            f"model {model.id}: unknown trained task {t}"
            for t in model.cim_trained_tasks or ()
            if t not in catalog.taxonomy.dimensions.get("task_category", ())
        )
        errors.extend(_about_errors(model))
    errors.extend(
        f"context_profiles.yaml: unknown retrieval_skip_tasks task {t}"
        for t in sorted(catalog.retrieval_skip_tasks)
        if t not in catalog.taxonomy.dimensions.get("task_category", ())
    )
    if catalog.models.rlcd is None:
        errors.append("models.yaml: rlcd (the classifier's pin and facts) is required")
    for ref in (catalog.models.frontier_baseline, catalog.models.cim_model):
        if ref not in model_ids:
            errors.append(f"models.yaml references unknown model {ref}")
    roles = {m.id: m.role for m in catalog.models.models}
    grader = catalog.models.grader_model
    if grader is not None and roles.get(grader) != "grader":
        errors.append(f"models.yaml: grader_model {grader} must be a registered grader-role model")
    ranks = {m.id: m.quality_rank for m in catalog.models.models}
    allowed = {a.id: a.allowed_models for a in catalog.tenant.applications}
    for scenario in catalog.scenarios:
        if scenario.application_id not in application_ids:
            errors.append(f"scenario {scenario.id}: unknown application {scenario.application_id}")
        elif (
            max((ranks.get(m, 0) for m in allowed[scenario.application_id]), default=0)
            < MIN_QUALITY_RANK[scenario.quality_tier]
        ):
            # RLCD would fall back to the best model allowed, so the tier would not mean what it says.
            errors.append(
                f"scenario {scenario.id}: quality tier {scenario.quality_tier} needs a model of rank "
                f">= {MIN_QUALITY_RANK[scenario.quality_tier]} allowed for {scenario.application_id}"
            )
        if scenario.context_profile and scenario.context_profile not in profile_ids:
            errors.append(f"scenario {scenario.id}: unknown context profile {scenario.context_profile}")
        if scenario.live_demo_use_case and scenario.deferred:
            errors.append(f"scenario {scenario.id}: a deferred scenario cannot be a live demo use case")
        errors.extend(_sample_errors(scenario))
    use_cases = [s.live_demo_use_case for s in catalog.scenarios if s.live_demo_use_case]
    errors.extend(
        f"live demo use case {u} is given to more than one scenario"
        for u in sorted({u for u in use_cases if use_cases.count(u) > 1})
    )
    if errors:
        raise ValueError("Invalid configuration:\n" + "\n".join(errors))


def policy_reference_errors(catalog: Catalog, policies: list[PolicyDocument]) -> list[str]:
    """Cross-reference errors for policy documents (used at startup and for policy drafts)."""
    errors: list[str] = []
    model_ids = {m.id for m in catalog.models.models}
    application_ids = {a.id for a in catalog.tenant.applications}
    tool_ids = {t.id for t in catalog.tools}
    seen: set[str] = set()
    for policy in policies:
        if policy.policy_id in seen:
            errors.append(f"policy {policy.policy_id}: duplicate policy_id")
        seen.add(policy.policy_id)
        errors.extend(
            f"policy {policy.policy_id}: unknown model {m}"
            for m in policy.enforce.allowed_models or []
            if m not in model_ids
        )
        errors.extend(
            f"policy {policy.policy_id}: unknown application {a}"
            for a in policy.match.application_any or []
            if a not in application_ids
        )
        errors.extend(
            f"policy {policy.policy_id}: unknown tool {t}"
            for t in policy.match.tool_any or []
            if t not in tool_ids
        )
        for cidr in policy.match.source_cidr_any or []:
            try:
                ipaddress.ip_network(cidr, strict=True)
            except ValueError as exc:
                errors.append(f"policy {policy.policy_id}: {exc}")
    return errors
