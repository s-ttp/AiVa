"""Provider tests run on recorded exchanges (`replayed`) and never contact a provider in CI.

Re-record with real provider calls (spends a few cents; needs HF_TOKEN, AIVA_ANTHROPIC_API_KEY and
GEMINI_API_KEY): `AIVA_RECORD_PROVIDERS=1 uv run pytest gateway/tests/live -q`. Recordings hold only
request and response bodies of synthetic prompts, never headers or credentials.

Endpoints A/B scale to zero: wake them first (the first request answers 503 for about four minutes) or
their tests record a cold start instead of an answer. A woken endpoint costs its hourly rate until it has
been idle for 15 minutes (ADR-0009).
"""

import os
from pathlib import Path
from typing import Any

from aiva_gateway.config import REPO_ROOT, Settings
from aiva_gateway.core.enums import Provenance
from aiva_gateway.inference.recording import REDACTED_ENDPOINT_HOST

RECORDING = os.environ.get("AIVA_RECORD_PROVIDERS") == "1"
MODE = "record" if RECORDING else "replay"
RECORDINGS_DIR = Path(REPO_ROOT, "gateway", "tests", "fixtures", "recordings")
EXPECTED_PROVENANCE = Provenance.MEASURED if RECORDING else Provenance.REPLAYED


def provider_settings(**extra: Any) -> Settings:
    return Settings(inference_mode=MODE, recordings_dir=RECORDINGS_DIR, **extra)


# Endpoints A/B (ML & Data). Recording resolves them through the HF Endpoints API, which also checks the
# served weights against config/models.yaml; replay pins the recorded URLs (their hosts are redacted on
# save) and never contacts the API.
ENDPOINT_NAMESPACE = os.environ.get("AIVA_ENDPOINT_NAMESPACE", "")  # your Hugging Face account, to record
ENDPOINT_URLS = {
    "enterprise-cim": f"https://{REDACTED_ENDPOINT_HOST}",
    "gct-qwen-finetuned": f"https://{REDACTED_ENDPOINT_HOST}",
}


def endpoint_settings(**extra: Any) -> Settings:
    endpoints: dict[str, Any] = (
        {"endpoint_namespace": ENDPOINT_NAMESPACE} if RECORDING else {"endpoint_urls": ENDPOINT_URLS}
    )
    return provider_settings(simulated_models=[], **endpoints, **extra)
