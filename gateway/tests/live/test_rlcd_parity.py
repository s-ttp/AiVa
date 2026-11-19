"""Parity of the gateway's RLCD loading with ML & Data's reference outputs (ml/fixtures/rlcd-parity-v2.jsonl,
also s-ttp/aiva-eval-runs runs/rlcd-v2-parity).

Opt-in like re-recording: needs the `rlcd` extra and HF_TOKEN, and downloads the pinned 596 MB model.
`AIVA_RLCD_PARITY=1 uv run pytest gateway/tests/live/test_rlcd_parity.py -q`. With `AIVA_RLCD_LOCAL_DIR` it
loads a snapshot from that folder instead (a staging copy before the pin is public). CI replays the same
rows' expected outputs through `FixtureRlcd` instead (tests/unit/test_rlcd_model.py).
"""

import os
from pathlib import Path

import pytest

from aiva_gateway.optimize.rlcd_model import ModernBertRlcd
from tests.unit.test_rlcd_model import ROWS, VERSION, messages

pytestmark = pytest.mark.skipif(
    os.environ.get("AIVA_RLCD_PARITY") != "1", reason="opt-in: downloads the pinned RLCD model"
)


async def test_pinned_model_reproduces_the_reference_outputs() -> None:
    local = os.environ.get("AIVA_RLCD_LOCAL_DIR")
    rlcd = ModernBertRlcd(
        token=os.environ.get("HF_TOKEN"), local_dir=Path(local) if local else None, timeout_seconds=30.0
    )
    await rlcd.start()
    assert rlcd.ready
    try:
        for row_id, row in ROWS.items():
            prediction = await rlcd.classify(messages(row_id))
            assert prediction is not None and prediction.model_version == VERSION
            for head, expected in row["expected"].items():
                assert prediction.top(head)[0] == expected["label"], (row_id, head)
                assert prediction.probabilities[head] == pytest.approx(expected["probs"], abs=1e-3)
    finally:
        await rlcd.aclose()
