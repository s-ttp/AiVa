# AiVa scenarios: reproducible demo

The ten locked synthetic GCT scenarios of LLD §15 are defined in `config/scenarios.yaml`. Scenario 7
(Arabic–English) is deferred by ADR-0001. Every other scenario has synthetic fixtures in
`gateway/tests/fixtures/scenarios/` and an integration test that asserts its primary proof.

## Run the demo

```bash
bash scripts/dev-services.sh                         # Postgres, Redis, OPA
uv run pytest gateway/tests/scenarios -v             # all scenarios on replayed provider fixtures
```

CI runs the same tests and never contacts a provider:

- **External models** (Gemini Flash-Lite, DeepSeek V4.1 Flash, Claude) answer from recorded exchanges in
  `gateway/tests/fixtures/scenarios/recordings/`. Their output and usage are labelled `replayed`.
- **GCT-Qwen (Endpoint B) and the CIM (Endpoint A)** are simulated and labelled `simulated` until ML & Data
  deploys them.
- **Knowledge base:** ML & Data's synthetic KB (`data/synthetic/kb/gct_kb.jsonl`) plus the scenario rows in
  `gateway/tests/fixtures/scenarios/kb_scenarios.jsonl`: the add-on that explains scenario 1's bill, and
  scenario 4's approved and never-retrievable roaming policies (confidential, expired, not yet in force,
  another application's).
- **Response cache:** the scenarios that need it get it in a namespace of their own. Its embedding is a
  constant stand-in, because the pinned bge model is not downloaded in CI. That isolates the
  factual-equivalence check.

Re-recording the external exchanges spends a few cents. It needs `HF_TOKEN`, `AIVA_ANTHROPIC_API_KEY` and
`GEMINI_API_KEY`:

```bash
AIVA_RECORD_PROVIDERS=1 uv run pytest gateway/tests/scenarios -q
```

Changing a fixture, a prompt, the KB rows or a context-packaging rule changes the provider request. The
recording then misses (`REPLAY_MISS`) and must be re-recorded.

## Scenarios

| # | Scenario | Application | Test | What the test proves |
|---|---|---|---|---|
| 1 | Customer Support Chatbot | gct-support-chatbot | `test_s01_customer_support.py` | Tariff RAG: the KB passage that explains the 15 AED increase is retrieved. Pruning: a 33-turn history is packaged into the latest turns plus that passage, the rest is omitted, and the Guard verifies the package (constructed < original tokens). Savings: the answer runs on DeepSeek (it is grounded in KB passages only), the pinned Claude baseline is a separate charge, and the repeat is a full cache hit with no upstream call and a modeled avoided cost. |
| 2 | Coding Assistant | gct-coding-assistant | `test_s02_coding_assistant.py` | The replayed DeepSeek answer is a CDR parser plus pytest tests, and those tests pass in an isolated interpreter. A prompt with a credential is blocked (GCT-SEC-002). A test vector with an IMSI goes to GCT-Qwen only. `local-only` routing never reaches an external model. |
| 3 | Telecom Fraud Management | gct-fraud-investigator | `test_s03_fraud_management.py` | A confidential investigation with a Claude-preferred hint goes sovereign-direct, with zero CIM and zero external calls. Every event timestamp reaches GCT-Qwen unchanged. Suspending a line is denied (not granted to the application). |
| 4 | Enterprise RAG | gct-knowledge-portal | `test_s04_enterprise_rag.py` | Only approved, in-force, application-granted internal documents are retrieved. Kept passages are cited in the payload, and the answer cites a kept document (GCT-POL-014, the Group policy, among them). Confidential, expired, not-yet-in-force and other applications' roaming policies are never retrieved. Evidence without `document_id`/`section` fails the Guard, and the full context is used instead. |
| 5 | Network Operations Intelligence | gct-noc-assistant | `test_s05_network_operations.py` | Sixty alarms at `critical` quality assess as complex and route to the frontier model (replayed Claude), which correlates the MW-12 failure. The Guard verifies source values, timestamps and numbers. A package that alters a timestamp is rejected, and the tampered value never reaches the model. |
| 6 | Agentic Workflow | gct-incident-agent | `test_agentic_workflow.py` (PR D) | Each tool call is decided by the tool policy stage of the preceding governed step. Steps draw on one workflow budget. Injected tool output blocks side effects. There is no autonomous punitive action. Approval and resume are tested, plus the §17 failure cases. |
| 8 | Repeated Queries & Caching | gct-support-chatbot | `test_s08_repeated_queries.py` | Exact and semantic hits return with no attempt and no reservation, and the avoided cost is modeled. A different region is refused despite an identical embedding. A new policy version misses, and rolling back re-pins the hash so its entries hit again. Account questions never cross users. |
| 9 | Security & Data Exfiltration | gct-knowledge-portal | `test_s09_security_exfiltration.py` | Plain and obfuscated injection and credentials are blocked before any model call, and audited with rule IDs and policy version. A subscriber identifier keeps the request off every external model, even a preferred one. Injected evidence reaches the model only inside its wrapper. A leaked secret in model output is withheld. No prompt text is stored in spans, policy events or the audit log. |
| 10 | Confidential DOCX/PPTX Copilot | gct-doc-copilot | `test_s10_document_copilot.py` | The catalog's board pack is drafted sovereign-direct (GCT-Qwen only, CIM bypassed) into an editable PPTX and DOCX. Every critical figure and citation survives into the file the sandbox opened, and the download takes an edit. The §16 checks catch altered figures, invented numbers, missing or unknown citations and thin structure. §17: other owners and tenants get 404 and are audited, only generated bytes render, and the sandbox has no network, processes or disk. |

## Running a scenario in the Test Console

Sign in as the demo tester, choose the scenario's application profile and paste the fixture prompt. The
console address `192.0.2.10` is in the `net-test-console` trusted network. Live runs that wake
Endpoint A or B are coordinated first, so they never collide with a benchmark.

## Known limits (stated, never hidden)

- **Context pruning in scenario 1** is performed in CI by a deterministic stand-in CIM, labelled
  `simulated`. It shows how the gateway treats a pruned package (Guard verification, accounting,
  payload), not the CIM's own judgement. A replayed Endpoint A recording replaces it once the endpoint
  is live.
- **The RLCD** serves the lowest estimated cost per request among qualified models, the sovereign model's GPU
  time included (ADR-0009, 2026-10-22): Gemini Flash-Lite at the standard tier, DeepSeek at the high tier,
  GCT-Qwen on the sovereign path. Gemini does not qualify to answer from knowledge passages only, so DeepSeek
  answers those at the standard tier too, at temperature 0; requests carrying operational records stay on
  Gemini, at 0.3 (ADR-0009, 2026-10-22 grounded-answers amendment). Scenario tests that demonstrate a specific external model use a routing hint
  (`routing.preferred_model`), which is honoured only inside the policy-eligible set.
