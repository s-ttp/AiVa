import shutil
from pathlib import Path

import pytest
import yaml

from aiva_gateway.catalog import load_catalog
from aiva_gateway.config import REPO_ROOT


def test_repository_config_is_valid() -> None:
    catalog = load_catalog(Path(REPO_ROOT, "config"))
    # The ten locked scenarios (LLD §15), then the live demo page's six use cases.
    assert {s.number for s in catalog.scenarios if s.live_demo_use_case is None} == set(range(1, 11))
    assert {s.number for s in catalog.scenarios if s.live_demo_use_case} == set(range(11, 17))
    assert next(s for s in catalog.scenarios if s.number == 7).deferred
    assert catalog.policies[0].policy_id == "GCT-CONF-001"  # highest priority first


def test_unknown_model_reference_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "config"
    shutil.copytree(Path(REPO_ROOT, "config"), config)
    policy = config / "policies" / "GCT-CONF-001.yaml"
    policy.write_text(policy.read_text().replace("[gct-qwen-finetuned]", "[no-such-model]"))
    with pytest.raises(ValueError, match="unknown model no-such-model"):
        load_catalog(config)


@pytest.mark.parametrize("price", ['"4.0000005"', '"-1.00"'])
def test_rate_card_prices_must_be_whole_micro_usd(tmp_path: Path, price: str) -> None:
    config = tmp_path / "config"
    shutil.copytree(Path(REPO_ROOT, "config"), config)
    cards = config / "rate_cards.yaml"
    cards.write_text(cards.read_text().replace('input_per_mtok: "4.00"', f"input_per_mtok: {price}", 1))
    with pytest.raises(ValueError, match="whole micro-USD"):
        load_catalog(config)


def test_cim_trained_tasks_must_be_taxonomy_task_categories(tmp_path: Path) -> None:
    config = tmp_path / "config"
    shutil.copytree(Path(REPO_ROOT, "config"), config)
    models = config / "models.yaml"
    trained = "    cim_trained_tasks: [qa, rag, agentic, summarization, analysis]\n"
    assert trained in models.read_text()
    models.write_text(models.read_text().replace(trained, "    cim_trained_tasks: [qa, chit-chat]\n"))
    with pytest.raises(ValueError, match="model enterprise-cim: unknown trained task chit-chat"):
        load_catalog(config)


def test_the_grader_must_be_a_registered_grader_role_model(tmp_path: Path) -> None:
    config = tmp_path / "config"
    shutil.copytree(Path(REPO_ROOT, "config"), config)
    models = config / "models.yaml"
    data = yaml.safe_load(models.read_text(encoding="utf-8"))
    data["grader_model"] = data["frontier_baseline"]
    models.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    with pytest.raises(
        ValueError, match="grader_model claude-flagship must be a registered grader-role model"
    ):
        load_catalog(config)
    data["grader_model"] = "no-such-model"
    models.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="grader_model no-such-model"):
        load_catalog(config)


def _edit(config: Path, name: str, key: str, entry_id: str, **fields: object) -> None:
    """Set fields on one entry of a copied config file, in the parsed data (a later duplicate key would
    otherwise win over an inserted line)."""
    path = config / name
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = data[key] if key in data else data["tenant"][key]
    next(e for e in entries if e["id"] == entry_id).update(fields)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def test_a_scenario_quality_tier_defaults_to_standard_and_must_be_reachable(tmp_path: Path) -> None:
    catalog = load_catalog(Path(REPO_ROOT, "config"))
    assert {s.quality_tier for s in catalog.scenarios} <= {"standard", "high", "critical"}
    config = tmp_path / "config"
    shutil.copytree(Path(REPO_ROOT, "config"), config)
    _edit(config, "scenarios.yaml", "scenarios", "coding-assistant", quality_tier="urgent")
    with pytest.raises(ValueError, match="quality_tier"):
        load_catalog(config)
    # Critical needs a rank-3 model the application allows; without one RLCD would quietly take the best
    # model it has, so the loader refuses the tier instead.
    _edit(config, "scenarios.yaml", "scenarios", "coding-assistant", quality_tier="critical")
    coding = load_catalog(config).scenario("coding-assistant")
    assert coding is not None and coding.quality_tier == "critical"
    allowed = next(a for a in catalog.tenant.applications if a.id == coding.application_id).allowed_models
    assert "claude-flagship" in allowed
    without = [m for m in allowed if m != "claude-flagship"]
    _edit(config, "tenant.yaml", "applications", coding.application_id, allowed_models=without)
    with pytest.raises(
        ValueError, match="scenario coding-assistant: quality tier critical needs a model of rank >= 3"
    ):
        load_catalog(config)


def test_a_scenario_output_budget_defaults_to_the_chat_default_and_must_be_a_valid_request(
    tmp_path: Path,
) -> None:
    from aiva_gateway.contracts.chat import ChatRequest

    catalog = load_catalog(Path(REPO_ROOT, "config"))
    default = ChatRequest.model_fields["max_output_tokens"].default
    assert default == 1024
    assert all(s.max_output_tokens >= 1 for s in catalog.scenarios)
    config = tmp_path / "config"
    shutil.copytree(Path(REPO_ROOT, "config"), config)
    _edit(config, "scenarios.yaml", "scenarios", "coding-assistant", max_output_tokens=6144)
    assert load_catalog(config).scenario("coding-assistant").max_output_tokens == 6144  # type: ignore[union-attr]
    _edit(config, "scenarios.yaml", "scenarios", "coding-assistant", max_output_tokens=40000)
    with pytest.raises(ValueError, match="max_output_tokens"):
        load_catalog(config)


def test_a_live_demo_use_case_is_optional_unique_and_never_deferred(tmp_path: Path) -> None:
    catalog = load_catalog(Path(REPO_ROOT, "config"))
    deferred = next(s for s in catalog.scenarios if s.deferred)
    assert deferred.live_demo_use_case is None
    config = tmp_path / "config"
    shutil.copytree(Path(REPO_ROOT, "config"), config)
    # Free every use case the shipped live scenarios hold, so the checks below start from none taken. A live
    # use case carries two alternate questions, so a scenario given one gets two as well.
    for scenario in catalog.scenarios:
        if scenario.live_demo_use_case:
            _edit(
                config,
                "scenarios.yaml",
                "scenarios",
                scenario.id,
                live_demo_use_case=None,
                alternate_prompts=[],
            )
    alternates = ["A first synthetic alternate?", "A second synthetic alternate?"]
    _edit(
        config,
        "scenarios.yaml",
        "scenarios",
        "coding-assistant",
        live_demo_use_case="frontier-optimization",
        alternate_prompts=alternates,
    )
    coding = load_catalog(config).scenario("coding-assistant")
    assert coding is not None and coding.live_demo_use_case == "frontier-optimization"
    _edit(
        config,
        "scenarios.yaml",
        "scenarios",
        "customer-support",
        live_demo_use_case="frontier-optimization",
        alternate_prompts=alternates,
    )
    with pytest.raises(
        ValueError, match="live demo use case frontier-optimization is given to more than one"
    ):
        load_catalog(config)
    _edit(config, "scenarios.yaml", "scenarios", "customer-support", live_demo_use_case="market-research")
    assert load_catalog(config).scenario("customer-support").live_demo_use_case == "market-research"  # type: ignore[union-attr]
    _edit(config, "scenarios.yaml", "scenarios", "customer-support", live_demo_use_case="lunch-order")
    with pytest.raises(ValueError, match="live_demo_use_case"):
        load_catalog(config)
    _edit(
        config,
        "scenarios.yaml",
        "scenarios",
        "customer-support",
        live_demo_use_case=None,
        alternate_prompts=[],
    )
    _edit(config, "scenarios.yaml", "scenarios", deferred.id, live_demo_use_case="inference-api")
    with pytest.raises(
        ValueError, match=f"scenario {deferred.id}: a deferred scenario cannot be a live demo"
    ):
        load_catalog(config)


@pytest.mark.parametrize(
    ("model_id", "grounding", "message"),
    [
        ("enterprise-cim", {"knowledge": {"qualified": False}}, "grounding applies to answering"),
        ("claude-flagship", {"records": {"temperature": 0}}, "Anthropic model's temperature cannot be set"),
        ("deepseek-v4-1-flash", {"knowledge": {"temperature": 2.5}}, "less than or equal to 2"),
        ("deepseek-v4-1-flash", {"code": {"temperature": 0}}, "knowledge"),
    ],
)
def test_grounded_answer_settings_are_validated(
    tmp_path: Path, model_id: str, grounding: dict[str, object], message: str
) -> None:
    # ADR-0009, 2026-10-22 grounded-answers amendment.
    config = tmp_path / "config"
    shutil.copytree(Path(REPO_ROOT, "config"), config)
    _edit(config, "models.yaml", "models", model_id, grounding=grounding)
    with pytest.raises(ValueError, match=message):
        load_catalog(config)


def test_live_demo_alternate_prompts_are_two_distinct_valid_requests(tmp_path: Path) -> None:
    """Each live demo use case asks its sample or one of two alternates (owner decision, 2026-10-25). An
    alternate is validated as the request the page sends, like the sample."""
    catalog = load_catalog(Path(REPO_ROOT, "config"))
    live = [s for s in catalog.scenarios if s.live_demo_use_case]
    assert live and all(len(s.alternate_prompts) == 2 for s in live)
    assert not any(s.alternate_prompts for s in catalog.scenarios if not s.live_demo_use_case)
    sample = next(s for s in live if s.id == "live-security-enforcement")

    def refused(match: str, scenario_id: str = sample.id, **fields: object) -> None:
        config = tmp_path / f"config-{len(list(tmp_path.iterdir()))}"
        shutil.copytree(Path(REPO_ROOT, "config"), config)
        _edit(config, "scenarios.yaml", "scenarios", scenario_id, **fields)
        with pytest.raises(ValueError, match=match):
            load_catalog(config)

    refused("alternate prompts are for live demo use cases only", "enterprise-rag", alternate_prompts=["Hi?"])
    refused("exactly 2 alternate prompts", alternate_prompts=sample.alternate_prompts[:1])
    refused("exactly 2 alternate prompts", alternate_prompts=[*sample.alternate_prompts, "A third?"])
    refused("an alternate prompt is blank", alternate_prompts=[sample.alternate_prompts[0], "   "])
    # Equal to the sample once case and spacing are ignored, or to the other alternate.
    refused(
        "must differ", alternate_prompts=[f"  {sample.sample_prompt.upper()} ", sample.alternate_prompts[1]]
    )
    refused("must differ", alternate_prompts=[sample.alternate_prompts[0]] * 2)
    # Longer than a chat message may be: the same bound as the sample's.
    refused("invalid sample request", alternate_prompts=[sample.alternate_prompts[0], "x" * 200_001])


def test_retrieval_skip_tasks_must_be_taxonomy_task_categories(tmp_path: Path) -> None:
    config = tmp_path / "config"
    shutil.copytree(Path(REPO_ROOT, "config"), config)
    profiles = config / "context_profiles.yaml"
    assert "retrieval_skip_tasks: [coding]\n" in profiles.read_text()
    profiles.write_text(
        profiles.read_text().replace("retrieval_skip_tasks: [coding]\n", "retrieval_skip_tasks: [chess]\n")
    )
    with pytest.raises(ValueError, match="unknown retrieval_skip_tasks task chess"):
        load_catalog(config)
