"""Scenario 2 — Coding Assistant (LLD §15).

Primary proof: telecom automation generation, tests and policy-constrained models.

- Generation and tests: the assistant writes a synthetic CDR parser with pytest tests (replayed provider
  output). The generated tests are executed in an isolated interpreter with a minimal environment, and
  they pass.
- Policy-constrained models: a hint for an external model is honoured only inside the allowed set.
  - A prompt with a credential is blocked before any model call (GCT-SEC-002).
  - A prompt with a subscriber identifier takes the sovereign direct path.
  - `local-only` routing keeps the request off external providers.
"""

import ast
import asyncio
import re
import subprocess
import sys

from sqlalchemy import select

from aiva_gateway.db.models import AuditLogRow, ModelAttemptRow
from tests.conftest import chat_body, console
from tests.scenarios.conftest import EXTERNAL, PROVIDER, scenario_fixture

APP = "gct-coding-assistant"
_CODE = re.compile(r"```(?:python)?\n(.*?)```", re.DOTALL)


def _request(prompt: str, **extra) -> dict:  # type: ignore[no-untyped-def,type-arg]
    return chat_body(prompt, task="coding", scenario_id="coding-assistant", **console(APP), **extra)


async def _attempts(db, run_id: str) -> list[ModelAttemptRow]:  # type: ignore[no-untyped-def]
    rows = await db.execute(select(ModelAttemptRow).where(ModelAttemptRow.run_id == run_id))
    return list(rows.scalars())


async def test_generated_automation_and_its_tests_pass(
    scenario_services, tester_client, db, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    fixture = scenario_fixture("coding-assistant")
    payload = _request(
        fixture["prompt"], max_output_tokens=1200, routing={"preferred_model": "deepseek-v4-1-flash"}
    )
    async with tester_client(svc=scenario_services) as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed", body.get("error")
    # The hint is honoured because DeepSeek is inside the application's allowed, policy-eligible set.
    assert body["selected_model"] == "deepseek-v4-1-flash"
    assert body["response"]["provenance"] == PROVIDER
    assert "deepseek-v4-1-flash" in body["policy"]["allowed_models"]

    blocks = _CODE.findall(body["response"]["content"])
    assert blocks, "the assistant returned no code block"
    code = "\n".join(blocks)
    tree = ast.parse(code)
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "parse_cdr_roaming" in names and sum(n.startswith("test_") for n in names) >= 2

    # Run the generated tests in an isolated interpreter (no site dir, no inherited environment).
    module = tmp_path / "test_generated_cdr.py"
    module.write_text(code, encoding="utf-8")
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-I", "-m", "pytest", "-q", "-p", "no:cacheprovider", str(module)],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" in result.stdout


async def test_credentials_in_code_are_blocked_before_any_model(scenario_services, tester_client, db) -> None:  # type: ignore[no-untyped-def]
    prompt = scenario_fixture("coding-assistant")["secret_prompt"]
    async with tester_client(svc=scenario_services) as client:
        body = (await client.post("/v1/ai/chat", json=_request(prompt))).json()
    assert body["status"] == "blocked" and body["error"]["code"] == "POLICY_DENIED"
    assert "GCT-SEC-002" in body["error"]["rule_ids"]
    assert body["response"] is None and await _attempts(db, body["run_id"]) == []
    audit = (
        await db.execute(select(AuditLogRow).where(AuditLogRow.target_id == body["run_id"]))
    ).scalar_one()
    assert audit.action == "policy.deny" and "GCT-SEC-002" in audit.details["rule_ids"]


async def test_subscriber_identifiers_force_the_sovereign_model(scenario_services, tester_client, db) -> None:  # type: ignore[no-untyped-def]
    prompt = scenario_fixture("coding-assistant")["imsi_prompt"]
    payload = _request(prompt, routing={"preferred_model": "deepseek-v4-1-flash"})
    async with tester_client(svc=scenario_services) as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed", body.get("error")
    assert body["policy"]["effective_classification"] == "confidential"
    assert body["execution_path"] == "sovereign-direct" and body["selected_model"] == "gct-qwen-finetuned"
    assert body["cim"]["message"] == "CIM bypassed by mandatory sovereignty policy"
    assert {c["model_id"] for c in body["policy"]["denied_candidates"]} >= EXTERNAL
    assert {a.model_id for a in await _attempts(db, body["run_id"])} == {"gct-qwen-finetuned"}


async def test_local_only_routing_never_reaches_an_external_provider(
    scenario_services, tester_client, db
) -> None:  # type: ignore[no-untyped-def]
    prompt = scenario_fixture("coding-assistant")["prompt"]
    payload = _request(prompt, routing={"mode": "local-only", "preferred_model": "claude-flagship"})
    async with tester_client(svc=scenario_services) as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed", body.get("error")
    assert body["selected_model"] == "gct-qwen-finetuned"
    assert not EXTERNAL & set(body["policy"]["allowed_models"])
    assert not EXTERNAL & {a.model_id for a in await _attempts(db, body["run_id"])}
    assert body["response"]["provenance"] == "simulated"  # GCT-Qwen (Endpoint B) is simulated in CI
