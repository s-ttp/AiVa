"""How the OpenAI-compatible API reports a run the gateway finished without an answer (ADR-0026)."""

from types import SimpleNamespace
from typing import Any, cast

import pytest

from aiva_gateway.api.openai_compat import render
from aiva_gateway.contracts.common import ErrorBody
from aiva_gateway.core.enums import Outcome, RunStatus


def _refused(http_status: int) -> tuple[int, dict[str, Any]]:
    ctx = SimpleNamespace(http_status=http_status)
    error = ErrorBody(
        code="LIVE_DEMO_UNAVAILABLE",
        message="The model endpoint is asleep, and key calls from the internet do not wake it.",
        remediation="Retry when it is running.",
        details={"reason": "endpoint_cold", "resets_at": None},
    )
    response = SimpleNamespace(
        status=RunStatus.BLOCKED,
        outcome=Outcome.ERROR,
        response=None,
        error=error,
        run_id="run_x",
        approval=SimpleNamespace(approval_id=None),
        routing=None,
    )
    return render(cast(Any, ctx), cast(Any, response), ignored=[], key="oai:gen-x")


@pytest.mark.parametrize(
    ("http_status", "expected"), [(200, 503), (500, 500), (502, 503), (503, 503), (504, 503)]
)
def test_a_run_the_gateway_finished_is_never_a_502_or_504(http_status: int, expected: int) -> None:
    # Space 2's front door replaces 502 and 504 with its own "starting" page (gateway-proxy.sh), so the
    # caller would never see why the gateway refused the run.
    status_code, body = _refused(http_status)
    assert status_code == expected
    assert body["error"]["code"] == "live_demo_unavailable" and body["error"]["type"] == "api_error"
    assert body["aiva"]["details"]["reason"] == "endpoint_cold" and body["aiva"]["retryable"] is False
