"""Live calibration of the answer-quality comparison (ADR-0023, 2026-10-21 amendment): real reference and
grader calls on one synthetic question, to check the rubric's anchors hold.

- the reference answer graded against itself scores at least 95;
- its first third (cut mid-answer) scores below 60;
- the same pair graded twice scores within 5 points.

Spends a few cents (one reference call, three grading calls), so it runs only when asked:
`AIVA_QUALITY_CALIBRATION=1` with `AIVA_ANTHROPIC_API_KEY` and `AIVA_OPENAI_API_KEY`. The keys are read when
this module is imported, before the test session hides deployment secrets from the gateway's Settings.
"""

import os

import pytest
from pydantic import SecretStr

from aiva_gateway.assurance.quality_check import (
    GRADER_FORMAT,
    GRADER_MAX_OUTPUT_TOKENS,
    grader_messages,
    parse_grade,
    reference_output_cap,
    reference_request_messages,
)
from aiva_gateway.catalog import load_catalog
from aiva_gateway.catalog.models import ModelSpec
from aiva_gateway.config import REPO_ROOT, Settings
from aiva_gateway.contracts.cim import CimEvidence
from aiva_gateway.contracts.quality import QualityGrade
from aiva_gateway.inference.endpoints import EndpointResolver
from aiva_gateway.inference.factory import build_adapter
from aiva_gateway.pipeline.interfaces import InferenceRequest

ANTHROPIC_KEY = os.environ.get("AIVA_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
OPENAI_KEY = os.environ.get("AIVA_OPENAI_API_KEY")
ENABLED = os.environ.get("AIVA_QUALITY_CALIBRATION") == "1" and bool(ANTHROPIC_KEY and OPENAI_KEY)

pytestmark = pytest.mark.skipif(
    not ENABLED,
    reason="live calibration spends; set AIVA_QUALITY_CALIBRATION=1 with AIVA_ANTHROPIC_API_KEY and "
    "AIVA_OPENAI_API_KEY",
)

CATALOG = load_catalog(REPO_ROOT / "config")
QUESTION = (
    "Compare the synthetic Traveller 10 and Traveller 25 roaming bundles: price, validity, data "
    "allowance and where each one works."
)
EVIDENCE = [
    CimEvidence(
        id="KB-ROAM-001#2",
        text="[KB-ROAM-001 §2] Traveller roaming bundles (synthetic) - Traveller 10\n"
        "The synthetic GCT Traveller 10 bundle costs 99 AED, is valid for 30 days and includes 10 GB of data "
        "in the six GCC countries.",
    ),
    CimEvidence(
        id="KB-ROAM-001#3",
        text="[KB-ROAM-001 §3] Traveller roaming bundles (synthetic) - Traveller 25\n"
        "The synthetic GCT Traveller 25 bundle costs 199 AED, is valid for 30 days and includes 25 GB of "
        "data in the GCC countries, the United Kingdom and the European Union.",
    ),
]


def _settings() -> Settings:
    assert ANTHROPIC_KEY and OPENAI_KEY
    return Settings(inference_mode="live").model_copy(
        update={"anthropic_api_key": SecretStr(ANTHROPIC_KEY), "openai_api_key": SecretStr(OPENAI_KEY)}
    )


def _model(model_id: str | None) -> ModelSpec:
    model = CATALOG.model(model_id or "")
    assert model is not None
    return model


async def _complete(request: InferenceRequest) -> str:
    resolver = EndpointResolver(overrides={}, namespace=None, token=None)
    result = await build_adapter(request.model, _settings(), resolver).complete(request)
    assert result.status == "ok", result.error_code
    return result.content


async def _grade(reference: str, answer: str) -> QualityGrade:
    grader = _model(CATALOG.models.grader_model)
    reply = await _complete(
        InferenceRequest(
            run_id="quality-calibration",
            model=grader,
            messages=grader_messages(QUESTION, EVIDENCE, reference, answer),
            max_output_tokens=GRADER_MAX_OUTPUT_TOKENS,
            response_format=GRADER_FORMAT,
        )
    )
    grade = parse_grade(reply, grader)
    assert grade is not None, reply
    return grade


async def test_the_grader_holds_its_anchors() -> None:
    reference = await _complete(
        InferenceRequest(
            run_id="quality-calibration",
            model=_model(CATALOG.models.frontier_baseline),
            messages=reference_request_messages([{"role": "user", "content": QUESTION}], EVIDENCE),
            max_output_tokens=reference_output_cap(1024),
            effort=CATALOG.models.baseline.effort,
        )
    )
    itself = await _grade(reference, reference)
    assert itself.score >= 95 and itself.verdict == "match", itself
    first_third = reference[: len(reference) // 3]
    cut = await _grade(reference, first_third)
    assert cut.score < 60, cut
    again = await _grade(reference, first_third)
    assert abs(again.score - cut.score) <= 5, (cut.score, again.score)
