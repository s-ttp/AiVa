"""The reservation input bound must cover what providers actually bill (checked on recorded exchanges)."""

import json
import re
from pathlib import Path

from aiva_gateway.config import REPO_ROOT
from aiva_gateway.core.canonical import estimate_tokens
from aiva_gateway.inference.tokens import input_token_bound

RECORDINGS = sorted(Path(REPO_ROOT, "gateway", "tests", "fixtures", "recordings").glob("*.json"))
_INPUT = re.compile(r'"(?:input_tokens|prompt_tokens|promptTokenCount)"\s*:\s*(\d+)')


def _messages(body: dict) -> list[dict[str, str]]:  # type: ignore[type-arg]
    if "contents" in body:  # Gemini
        system = body.get("systemInstruction") or body.get("system_instruction") or {"parts": []}
        parts = [*system["parts"], *(p for c in body["contents"] for p in c["parts"])]
        return [{"role": "user", "content": p["text"]} for p in parts]
    messages = [{"role": m["role"], "content": str(m["content"])} for m in body["messages"]]
    if body.get("system"):  # Anthropic
        messages.append({"role": "system", "content": str(body["system"])})
    return messages


def test_bound_covers_every_recorded_provider_count() -> None:
    assert RECORDINGS
    undercounted = 0
    for path in RECORDINGS:
        recording = json.loads(path.read_text())
        messages = _messages(recording["request"]["body"])
        billed = max(int(n) for n in _INPUT.findall(recording["response"]["body"]))
        assert input_token_bound(messages) >= billed, path.name
        undercounted += sum(estimate_tokens(m["content"]) for m in messages) < billed
    # The display estimate is not a bound: it undercounts some providers (why reservations use this one).
    assert undercounted > 0
