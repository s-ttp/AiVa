"""Budget and metering of the answer-quality comparison (ADR-0023 §6, finops.quality): its own envelope at the
worst case of one reference and one grader attempt, reserved once per run in the live domain against the
run's scopes, and metered as `quality_*` rows on the original run, never as savings."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from aiva_gateway.catalog import load_catalog
from aiva_gateway.config import REPO_ROOT
from aiva_gateway.core.enums import AttemptStage, ChargeType, Provenance
from aiva_gateway.finops.pricing import PriceBook
from aiva_gateway.finops.quality import (
    direct_frontier_measured_micros,
    quality_envelope,
    quality_idempotency_key,
    quality_ledger_rows,
    quality_reservation_request,
)
from aiva_gateway.finops.scopes import LIVE
from aiva_gateway.pipeline.context import AttemptRecord
from aiva_gateway.pipeline.interfaces import Reservation

CATALOG = load_catalog(REPO_ROOT / "config")
PRICES = PriceBook(CATALOG)
CLAUDE = CATALOG.model("claude-flagship")
# A stand-in for the grader until Optimize pins the OpenAI model (ADR-0023): any priced external model.
GRADER = CATALOG.model("gemini-flash-lite")
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


def run_row(**overrides: Any) -> Any:
    fields = {
        "run_id": "run_live1",
        "tenant_id": "gct",
        "department_id": "fraud",
        "application_id": "gct-fraud-investigator",
        "principal_pseudonym": "usr_presenter",
    }
    return SimpleNamespace(**(fields | overrides))


def attempt(stage: AttemptStage, model: Any, cost: int, *, status: str = "ok") -> AttemptRecord:
    return AttemptRecord(
        attempt_id=f"att-{stage.value}",
        stage=stage,
        model=model,
        started_at=NOW,
        completed_at=NOW,
        status=status,
        input_tokens=1000,
        output_tokens=200,
        cached_input_tokens=0,
        reasoning_tokens=0,
        latency_ms=None,
        ttft_ms=None,
        provider_model_version="test",
        provenance=Provenance.MEASURED,
        error_code=None,
        retry_of=None,
        charge_type=ChargeType.UPSTREAM_INFERENCE,  # the stage decides the ledger charge type
        cost_micros=cost,
    )


def test_the_envelope_holds_one_reference_and_one_grader_attempt_at_list_price() -> None:
    assert CLAUDE is not None and GRADER is not None
    envelope = quality_envelope(
        PRICES,
        reference=CLAUDE,
        reference_input_tokens=6000,
        reference_max_output_tokens=6144,
        grader=GRADER,
        grader_input_tokens=8000,
        grader_max_output_tokens=500,
    )
    reference = 6000 * 4 + 6144 * 20  # $4 / $20 per Mtok: 146,880 micro-USD, ADR-0023's "about $0.15"
    grader = PRICES.cost_micros(GRADER.id, input_tokens=8000, output_tokens=500)
    assert {line.model_id: line.amount_micros for line in envelope.lines} == {
        "claude-flagship": reference,
        "gemini-flash-lite": grader,
    }
    assert envelope.amount_micros == reference + grader == sum(line.amount_micros for line in envelope.lines)
    assert all(
        line.gpu_pool is None and line.gpu_micros == 0 for line in envelope.lines
    )  # never the GPU pool


def test_without_a_grader_only_the_reference_is_reserved() -> None:
    assert CLAUDE is not None
    envelope = quality_envelope(
        PRICES, reference=CLAUDE, reference_input_tokens=100, reference_max_output_tokens=50, grader=None
    )
    assert [line.model_id for line in envelope.lines] == ["claude-flagship"]
    assert envelope.amount_micros == 100 * 4 + 50 * 20


def test_one_reservation_per_run_against_its_scopes_in_the_live_domain() -> None:
    assert CLAUDE is not None
    envelope = quality_envelope(
        PRICES, reference=CLAUDE, reference_input_tokens=100, reference_max_output_tokens=50, grader=None
    )
    request = quality_reservation_request(run_row(), envelope)
    assert request.idempotency_key == quality_idempotency_key("run_live1") == "qc:run_live1"
    assert request.run_id == "run_live1" and request.domain == LIVE
    assert request.scope_chain == (
        ("tenant", "gct"),
        ("department", "fraud"),
        ("application", "gct-fraud-investigator"),
        ("user", "usr_presenter"),
        ("request", "gct-fraud-investigator"),
    )  # no workflow and no public-demo scope
    assert request.amount_micros == envelope.amount_micros and request.lines == envelope.lines


def test_each_call_is_a_quality_row_on_the_original_run() -> None:
    assert CLAUDE is not None and GRADER is not None
    attempts = [
        attempt(AttemptStage.QUALITY_REFERENCE, CLAUDE, 9_000),
        attempt(AttemptStage.QUALITY_GRADING, GRADER, 300),
    ]
    reservation = Reservation("res-qc", 150_000, (), Provenance.MEASURED)
    rows = quality_ledger_rows(run_row(), attempts, reservation, CATALOG)
    assert [(r.charge_type, r.amount_micros) for r in rows] == [
        (ChargeType.QUALITY_REFERENCE.value, 9_000),
        (ChargeType.QUALITY_GRADING.value, 300),
    ]
    assert {r.run_id for r in rows} == {"run_live1"} and {r.reservation_id for r in rows} == {"res-qc"}
    assert [r.attempt_id for r in rows] == ["att-quality_reference", "att-quality_grading"]
    assert [r.rate_card_id for r in rows] == [CLAUDE.rate_card_id, GRADER.rate_card_id]
    assert {r.measured_or_estimated for r in rows} == {"measured"}
    assert {r.rate_card_version for r in rows} == {CATALOG.rate_cards.version}


def test_only_comparison_attempts_are_metered_as_comparison_rows() -> None:
    assert CLAUDE is not None
    reservation = Reservation("res-qc", 1, (), Provenance.MEASURED)
    with pytest.raises(ValueError, match="not a comparison attempt"):
        quality_ledger_rows(run_row(), [attempt(AttemptStage.UPSTREAM, CLAUDE, 1)], reservation, CATALOG)


def test_the_measured_direct_frontier_is_the_reference_cost() -> None:
    assert CLAUDE is not None and GRADER is not None
    reference = attempt(AttemptStage.QUALITY_REFERENCE, CLAUDE, 9_000)
    grading = attempt(AttemptStage.QUALITY_GRADING, GRADER, 300)
    assert direct_frontier_measured_micros([reference, grading]) == 9_000  # the grader is never part of it
    failed = attempt(AttemptStage.QUALITY_REFERENCE, CLAUDE, 0, status="error")
    assert direct_frontier_measured_micros([failed, grading]) is None  # no reference answer, no figure
    assert direct_frontier_measured_micros([]) is None
