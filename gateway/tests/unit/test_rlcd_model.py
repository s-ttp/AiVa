"""RLCD routing on recorded model outputs (v2, ML's parity rows): thresholds, tighten-only sensitivity,
labelled fallback."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from aiva_gateway.catalog import load_catalog
from aiva_gateway.config import REPO_ROOT
from aiva_gateway.core.enums import Classification
from aiva_gateway.optimize.rlcd import (
    HeuristicRouter,
    ModelRouter,
    render,
    tightened_sensitivity,
)
from aiva_gateway.optimize.rlcd_model import (
    RLCD_LABELS,
    RLCD_REPO,
    RLCD_REVISION,
    FixtureRlcd,
    build_rlcd,
    model_version,
)
from aiva_gateway.pipeline.interfaces import RlcdPrediction, RouterRequest

# ML & Data's v2 parity rows (ml/fixtures/rlcd-parity-v2.jsonl, sha256 9e89eeae…): `expected` is the pinned
# model's output; `v1` is v1's beside it, for reference only.
PARITY = Path(REPO_ROOT, "gateway", "tests", "fixtures", "rlcd", "rlcd-parity-v2.jsonl")
ROWS = {row["id"]: row for row in map(json.loads, PARITY.read_text().splitlines()) if row}
VERSION = model_version(RLCD_REPO, RLCD_REVISION)
CATALOG = load_catalog(REPO_ROOT / "config")
_ROLE = re.compile(r"(?:^|\n)\[(system|user|assistant|tool)\] ")


def messages(row_id: str) -> list[dict[str, str]]:
    """The messages whose rendering is the parity row's text."""
    parts = _ROLE.split(ROWS[row_id]["text"])
    return [{"role": parts[i], "content": parts[i + 1]} for i in range(1, len(parts), 2)]


def recorded(row_id: str) -> RlcdPrediction:
    expected = ROWS[row_id]["expected"]
    return RlcdPrediction(VERSION, RLCD_LABELS, {h: tuple(v["probs"]) for h, v in expected.items()}, 12.0)


def prediction(**heads: tuple[float, ...]) -> RlcdPrediction:
    base = {h: tuple(1.0 if i == 0 else 0.0 for i in range(len(labels))) for h, labels in RLCD_LABELS.items()}
    return RlcdPrediction(VERSION, RLCD_LABELS, {**base, **heads}, 5.0)


def request(pred: RlcdPrediction | None, **extra: Any) -> RouterRequest:
    fields: dict[str, Any] = {
        "task": None,
        "scenario_task": None,
        "quality_tier": "standard",
        "original_context_tokens": 120,
        "evidence_count": 0,
        "preferred_model": None,
        "routing_mode": "hybrid",
        "prediction": pred,
    }
    return RouterRequest(**{**fields, **extra})


def test_render_matches_the_training_format() -> None:
    for row_id in ROWS:
        assert render(messages(row_id)) == ROWS[row_id]["text"]


def test_without_a_prediction_the_heuristic_serves_and_says_so() -> None:
    assessment = ModelRouter().assess(request(None))
    heuristic = HeuristicRouter().assess(request(None))
    assert assessment.rlcd_version == "heuristic-v0" and assessment.confidence is None
    assert assessment.task_category == heuristic.task_category
    assert assessment.reasons[-1] == "RLCD model unavailable: heuristic assessment"
    assert not assessment.confidences and assessment.predicted_sensitivity is None


def test_recorded_predictions_route_with_the_model_labels_and_version() -> None:
    for row_id, row in ROWS.items():
        assessment = ModelRouter().assess(request(recorded(row_id)))
        assert assessment.rlcd_version == VERSION
        expected = row["expected"]
        if "task_category" not in assessment.unclassified:
            assert assessment.task_category == expected["task_category"]["label"], row_id
        if "complexity" not in assessment.unclassified:
            assert assessment.complexity == expected["complexity"]["label"], row_id
        assert set(assessment.confidences) == set(RLCD_LABELS)


def test_heads_below_their_threshold_are_unclassified_and_keep_the_heuristic_value() -> None:
    # Row d8e4: complexity "complex" at 0.58, below 0.80.
    assessment = ModelRouter().assess(request(recorded("rl-d8e4d890097f6c84")))
    assert assessment.unclassified == ("complexity",)
    assert assessment.complexity == HeuristicRouter().assess(request(None)).complexity
    assert not assessment.local_fast_eligible  # an unclassified complexity never opens the fast path
    low = prediction(
        task_category=(0.55, 0.45, 0, 0, 0, 0, 0),
        complexity=(0.79, 0.21, 0, 0),
        context_need=(0.59, 0.41, 0),
    )
    assessment = ModelRouter().assess(request(low, evidence_count=2))
    assert assessment.unclassified == ("task_category", "complexity", "context_need")
    heuristic = HeuristicRouter().assess(request(None, evidence_count=2))
    assert (assessment.task_category, assessment.context_sufficient) == (
        heuristic.task_category,
        heuristic.context_sufficient,
    )


@pytest.mark.parametrize("hint", ["task", "scenario_task"])
def test_a_declared_task_wins_and_its_confidence_is_not_reported_as_the_run_confidence(hint: str) -> None:
    # The model says rag at 0.9996; a declared task, or the scenario's, still decides.
    assessment = ModelRouter().assess(request(recorded("rl-e002a6a0fee0c806"), **{hint: "qa"}))
    assert assessment.task_category == "qa" and assessment.confidence is None
    assert assessment.confidences["task_category"] == pytest.approx(0.999587)


@pytest.mark.parametrize(
    ("probs", "level"),
    [
        ((0.30, 0.25, 0.20, 0.15, 0.10), "internal"),  # P(>=confidential) = 0.45 < 0.5
        ((0.10, 0.20, 0.20, 0.30, 0.20), "restricted"),  # P(>=restricted) = 0.50
        ((0.40, 0.20, 0.10, 0.10, 0.20), "internal"),
        ((0.99, 0.01, 0.0, 0.0, 0.0), "public"),
    ],
)
def test_sensitivity_is_the_highest_level_holding_half_the_mass(probs: tuple[float, ...], level: str) -> None:
    assert tightened_sensitivity(prediction(sensitivity=probs)) is Classification(level)


def test_a_parity_miss_predicts_internal_so_only_tighten_only_keeps_it_safe() -> None:
    # Gold is confidential; the model says internal at 0.61 (P(>= confidential) 0.39). The orchestrator never
    # lowers a classification, so DLP (MSISDN) and the declared label still decide (pipeline test).
    assert ROWS["rl-57713fb159c8f9ba"]["gold"]["sensitivity"] == "confidential"
    assert tightened_sensitivity(recorded("rl-57713fb159c8f9ba")) is Classification.INTERNAL


def test_plans_stay_inside_the_eligible_set_and_carry_the_model_version() -> None:
    router = ModelRouter()
    eligible = [m for m in CATALOG.models.models if m.id in {"gemini-flash-lite", "deepseek-v4-1-flash"}]
    req = request(recorded("rl-e002a6a0fee0c806"), preferred_model="claude-flagship")
    plan = router.plan(req, router.assess(req), eligible, {"gemini-flash-lite": 1, "deepseek-v4-1-flash": 2})
    assert plan.model_id in {m.id for m in eligible} and plan.model_id != "claude-flagship"
    assert plan.rlcd_version == VERSION


async def test_fixture_classifier_replays_recorded_outputs_only() -> None:
    rlcd = build_rlcd("fixture", token=None, fixture=PARITY)
    assert isinstance(rlcd, FixtureRlcd)
    found = await rlcd.classify(messages("rl-d8e4d890097f6c84"))
    assert found is not None and found.top("sensitivity")[0] == "confidential"
    assert await rlcd.classify([{"role": "user", "content": "Not a recorded input."}]) is None
    assert (
        await build_rlcd("heuristic", token=None, fixture=None).classify(messages("rl-e002a6a0fee0c806"))
        is None
    )
