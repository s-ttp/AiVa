# ADR-0009: Live inference adapters, recorded fixtures, response caches and retrieval

**Context.** P1/P2 replace the simulated inference, CIM, cache and retrieval stages (LLD §3, §9, §11, §17,
§21.4). The LLD names "LiteLLM/provider clients" for inference. Model pins were re-verified against the
live providers on 2026-10-14 (`config/models.yaml`). Endpoints A (CIM) and B (GCT-Qwen) were deployed by
ML & Data on 2026-10-15 and verified the same day (see *Endpoints A/B* below).

**Decision.**

*Adapters.* Official provider SDKs, not LiteLLM: `anthropic` (Claude), `google-genai` (Gemini) and
`openai` for every OpenAI-compatible route (HF Inference Providers router for DeepSeek-V4.1-Flash, pinned
to `deepinfra`; vLLM on HF Inference Endpoints for GCT-Qwen and the CIM). Reasons: LiteLLM 1.82.7/1.82.8
shipped a credential stealer on PyPI (March 2026) and this process holds every provider key; it adds ~40
transitive packages; usage fields are mapped explicitly per provider; and the SDKs accept an injected HTTP
client, which the recorded fixtures need.
- Streaming everywhere; TTFT = first answer token (thinking excluded). Provider usage is authoritative and
  normalised to *total input* with `cached_input_tokens` as the discounted subset (Anthropic cache reads
  and writes are added back); thinking is billed output (`reasoning_tokens` is the breakdown).
- Adapters never retry. The orchestrator makes the single bounded retry so each attempt is policy-checked
  at egress, reserved and metered (Appendix B). Adapters classify failures (`TIMEOUT`, `RATE_LIMITED`,
  `UPSTREAM_ERROR`, `ENDPOINT_COLD_START`, …), wait out a short `Retry-After` (≤ 2 s) before returning a
  rate limit, and charge an `estimated` prompt + partial output when a failed call may have been billed.
- Per-model circuit breaker (5 consecutive provider failures, 30 s cooldown, single probe), in-process.
- Claude: effort `medium` set explicitly; server-side refusal fallbacks are *not* enabled, because a
  provider-chosen model switch would bypass the egress policy check and the pinned rate card.
- `max_output_tokens` bounds answer + thinking for all four providers (verified for Gemini), so the
  reservation's output bound holds.
- Models without credentials or endpoints fail closed (`CREDENTIALS_MISSING`, `ENDPOINT_NOT_CONFIGURED`);
  the sovereign model is only ever wired to a dedicated endpoint, never to the public router.

*Modes* (`AIVA_INFERENCE_MODE`): `simulated` (default), `live`, `record` (live + saves exchanges), `replay`
(recorded exchanges only, labelled `replayed`; a missing recording is `REPLAY_MISS`, never a network
call). Recordings sit at the HTTP transport below the SDKs, so CI exercises the real request building and
stream parsing. They store request/response bodies only — never headers or credentials. `simulated_models`
keeps listed models simulated (and labelled so) in live mode; it defaults to `gct-qwen-finetuned` and
`enterprise-cim`, so a live gateway without the endpoints configured still runs. Set
`AIVA_SIMULATED_MODELS='[]'` to call Endpoints B and A (below).

*Recording export* (`GET /v1/admin/recordings`):
- **Who.** Platform administrators only (`recordings:export`).
- **When.** Only a record-mode gateway answers; any other mode returns 404, after authorization.
- **What.** Every recording in `recordings_dir`, as one JSON bundle: `recordings[]` of `{name, content}`,
  where `content` is the fixture file's request and response. Each file is re-read through the recording
  format and its URL is cleaned again, so a bundle holds request and response bodies only, exactly as a
  committed fixture does. `tests/e2e/record_scenarios.py` writes them back as fixtures.
- **Withheld.** A file that is not a recording, is over 4 MB, or contains any of the gateway's configured
  secrets is left out and counted.
- **Audit.** Each export writes an audit row (`recordings.export`) with the counts by model label.

*Endpoints A/B.* HF Inference Endpoints in namespace `s-ttp` (AWS us-east-1, authenticated, vLLM v0.31.0,
`--max-model-len 16384`, min 0 / max 1 replica, scale to zero after 15 idle minutes):

| Endpoint | Model id | Served name | Weights (`weights_repo`@`weights_revision`) | GPU |
|---|---|---|---|---|
| A `enterprise-cim` | `enterprise-cim` | `enterprise-cim-lora` | `s-ttp/aiva-cim-qwen3-4b`@`739f46e9…` | 1x L4, $0.80/h |
| B `gct-qwen-finetuned` | `gct-qwen-finetuned` | `gct-qwen-lora` | `s-ttp/aiva-gct-qwen3-32b`@`f839f6e7…` | 1x A100 80GB, $2.50/h |

- Both serve merged bf16 fine-tunes. `provider_model_id`@`revision` stays the base-model pin (ADR-0002); the
  served weights are pinned in `weights_repo`/`weights_revision`.
- Before a call, the resolver reads the endpoint from the HF Endpoints API. The endpoint must serve exactly
  the pinned repository and revision, otherwise the call fails closed with `ENDPOINT_REVISION_MISMATCH`
  (not retried; the sovereign path still never falls back). A confirmed call is recorded as
  `<weights_repo>@<revision[:12]>` (model attempt `provider_model_version`, CIM `cim_revision`). An
  `AIVA_ENDPOINT_URLS` override cannot be checked, so its calls carry the name the server reports.
- GCT-Qwen requests also send `chat_template_kwargs: {enable_thinking: false}`; Endpoint B already defaults
  to it, so a redeploy without that default cannot add thinking tokens to answers.
- Cold start (measured 2026-10-15): a request to a scaled-to-zero endpoint gets `503 SERVICE_UNAVAILABLE`
  within about 0.5 s (no `Retry-After`) and wakes it. Endpoint A was running about 150 s later, Endpoint B
  about 195 s later. The adapter reports `ENDPOINT_COLD_START` at once, so the 60 s CIM timeout is never
  reached. The orchestrator's single retry also gets 503 and does not wait out the wake: the CIM falls back
  to the full eligible context, and the sovereign path returns a retryable 503
  (`SOVEREIGN_ENDPOINT_UNAVAILABLE`). Through a live gateway, both cold attempts took under 0.8 s in all
  (zero tokens, recorded with the pinned version). Wake both endpoints about four minutes before a demo.
- Run live against A/B: `AIVA_INFERENCE_MODE=live AIVA_ENDPOINT_NAMESPACE=s-ttp
  AIVA_SIMULATED_MODELS='[]' make dev` (needs `HF_TOKEN` with access to the namespace). Wake the endpoints
  first, or the first requests fall back as above. Re-record their fixtures with
  `AIVA_RECORD_PROVIDERS=1 uv run pytest gateway/tests/live -k endpoint` once both are running.

*CIM serving contract.* System prompt `CIM_SYSTEM_PROMPT` + the `CimRequest` as canonical JSON; reply is a
`CimResponse` constrained by `response_format: json_schema` at temperature 0, using the runtime schema
(`CimResponse.model_json_schema()`, fields in declaration order; ADR-0013); served model name
`enterprise-cim-lora`. The CIM fine-tune is trained on exactly this format. Invalid JSON/schema
is retried once; truncation is not. Passages in `upstream_messages` must be wrapped exactly as
`evidence_block` does (escaped ID, evidence tags in the text neutralised; Govern's prompt-injection
hardening). The Context Guard stays authoritative and now also checks citations, wrapper integrity,
omitted passages holding required facts, timeline collapse, invented/altered numbers and false "missing"
claims. Fact values match as whole tokens, so "2" is not found in "2026" nor "major" in "majority" (Govern
review of #58). A real recording shows why: the untuned base Qwen3-4B returns schema-valid JSON but drops fact
values from the payload, and the Guard rejects it.

*CIM bypass and output cap* (ML & Data's scenario 5 measurements):
- **Derived from the Guard.** `passages_to_keep` (in the Context Guard module) finds the passages every
  package must keep: each holds a required-field or chronology value found in no other passage and no
  message. It uses the same `unpreserved_facts` function as the Guard's omitted-facts check, so the two
  cannot drift apart. The Guard's behaviour is unchanged.
- **Bypass reasons.** Before the call, the CIM is bypassed in two cases:
  - **nothing to omit:** every passage must be kept, so the only verifiable package is the full context;
  - **too large to build in time:** emitting the kept passages, with their wrapper and the conversation
    (in real tokens, at `AIVA_CIM_DECODE_TOKENS_PER_S`, default 70 from ML's Endpoint A measurement with
    n-gram speculation), would exceed 0.8 × the 60 s CIM timeout. Such a call would time out, be retried,
    and fall back.
- **Recorded as.** `skipped-sufficient`, with the copy "CIM bypassed: nothing to omit; full context used" or
  "CIM bypassed: package too large to build in time; full context used". The span records
  `aiva.cim.bypass_reason`. No CIM call and no CIM reservation are made.
- **Output cap.** The CIM's `max_tokens` is in real tokens, while budgets are in `estimate_tokens` units.
  Record lists measure 1.7 to 2.0 real tokens per estimated token. The cap is therefore twice
  (budget × 1.1 + 1,024), bounded by the endpoint's context minus twice the prompt's estimate.
  - When that leaves fewer than 256 tokens, the CIM is not called (`CIM_CONTEXT_EXCEEDED`) and the full
    context is used.
- **Long term.** The CIM returns ids and facts and the gateway builds the package (ML's option 2, ADR-0018).

*Knowledge passages and citation integrity* (scenario 4):
- **Rendering.** Knowledge-base passages start with the header the CIM is trained on:
  `[<doc_id> §<n>] <title> - <heading>` (`optimize/citations.py`). A section named `§<n> <heading>` keeps its
  number; an unnumbered section is numbered by its position in the document. The rendering is hashed with
  every document, so changing it changes the knowledge-base version and rolls every response-cache key. The
  gateway Space ingests the KB on every boot; elsewhere, re-run `aiva-admin kb-ingest`.
- **Citation fields** (`ContextProfile.citation`). In `enterprise-rag`, `document_id` and `section` name a
  passage's source; they are not facts to preserve. The Guard checks citation integrity instead of pinning
  every passage that carries one:
  - `citation-headers`: every kept passage keeps its header inside its evidence block;
  - `citation-resolution`: a citation fact names its source passage's own document or section. Inside an
    evidence block, a `[DOC-ID §N]` must be one that block's own source passage holds; outside blocks, it
    must name a kept passage or appear in the conversation. So a citation of an omitted passage never
    resolves, whether it heads CIM-written text or sits inside a kept passage's block (Govern's review). A
    dangling citation fails the Guard and the full context is used;
  - numeric fidelity still applies to every kept passage.
- **Honest labelling.** The Guard verifies that nothing the profile requires was lost, not that what the CIM
  omitted was irrelevant. `GuardResult.relevance_unverified` lists those omissions (under a citation profile
  every omitted passage, otherwise every omitted knowledge passage). A passing package with any is reported as
  `cim-selected`, never `verified`: copy "Context package generated; CIM selected the passages (N omitted,
  relevance not verified)", strategy `prune`, and the run record's `guard_details` holds `sufficiency_basis`
  and `relevance_unverified` (count and ids).
- **Failure codes.** Next to each Guard failure message, `guard_details.failure_codes` holds a stable code in
  the same order, so reports count failures by kind without parsing messages (Insights,
  `GET /v1/reports/quality`). The codes are `unknown_reference`, `selected_and_omitted`,
  `required_field_missing`, `reported_missing_but_present`, `source_not_selected`, `value_not_verbatim`,
  `value_not_preserved`, `dangling_citation`, `citation_lost`, `citation_header_lost`, `wrapper_integrity`,
  `context_loss`, `chronology`, `numeric_fidelity`, `token_budget_exceeded` and `empty_selection`.
- **Non-empty selection** (2026-10-18, a safety finding in the shipped recordings). In S6's notice step the
  CIM kept none of 13 tool outputs. The request was `general` with no required fields, so nothing the
  profile requires could be lost, and the package passed as `verified`. Endpoint B, given no evidence,
  invented the incident's region and date. A package that omits every passage of a request with evidence
  now fails the Guard (`empty_selection`), and the full eligible context is sent ("Context sufficiency
  check failed; safe fallback applied"). This holds for any profile and any task, coding included: the
  Guard cannot tell a legitimately empty selection from a CIM that was never trained on the request's shape
  (ML & Data adds such requests to the v6 dataset). Upstream bodies change only where the shipped CIM
  selected nothing: S2's coding request and S6's four workflow steps.
- **Recall floor** (`keep_top_k`). The top k retrieved passages, in rank order, are added back to the CIM's
  package before the Guard verifies it (`with_passages`); the record lists them in `recall_floor_added`.
  They also count as kept for the bypass. The `enterprise-rag` default is k = 2, from an offline re-score of
  ML's served predictions on its 121 held-out RAG cases (measured, offline; Wave 3 repeats it live). Ranking
  is the gateway's reranker; every case passed the Guard:

  | k | tokens saved | gold passage recall | gold facts covered |
  |---|---|---|---|
  | 0 | 65.6% | 83.2% | 83.2% |
  | 1 | 64.3% | 85.5% | 85.5% |
  | 2 | 53.7% | 94.4% | 94.4% |

  k = 2 recovers 20 of the 30 gold passages the CIM omitted and still saves more than half the tokens; a
  missing policy section is an answer that cannot cite its source.
- **`keep_all_passages`** (default false) is for applications that accept no omission: every passage is kept,
  so the CIM is bypassed ("nothing to omit"), and the Guard rejects any omission.
- **Answer citations** (assurance check `answer-citations`, `assurance/citations.py`). The assurance stage
  also receives the run's upstream payload and candidate evidence IDs (`AnswerContext`) and checks every
  citation in the delivered answer against what the model was actually given:
  - A citation is a KB citation `[DOC-ID §N]` in the KB's document-ID grammar (uppercase segments joined by
    hyphens, at most 40 characters; at most three section digits), or a candidate evidence ID (supplied or
    retrieved, identifier-shaped, at least three characters with a letter) mentioned as an exact whole token. Nothing else counts, so reported
    references are always identifiers, never free model text.
  - It resolves when its header (or the user's own message) or its `<evidence id="…">` block was in the
    upstream payload. A citation of a passage the CIM omitted, or one never retrieved, is unresolved.
  - It reports and never blocks or rewrites. `assurance.answer_citations` is `resolved`, `unresolved` (count
    and up to 10 identifiers), `none` (the answer cites nothing) or `not_checked` (no upstream payload: a
    full response-cache return, a blocked run, a run halted before upstream, or an answer assurance did not
    deliver). The span carries the unresolved count only (`aiva.assurance.unresolved_citations`).
  - **Evidence coverage** = resolved citations ÷ cited, recorded as `assurance_result.evidence_coverage`;
    null when nothing is cited or the check did not run. The upstream payload is used in-run only and never
    persisted.

*Per-run execution mode* (ADR-0019):
- **Two adapter sets.** At startup the gateway builds the configured set (`AIVA_INFERENCE_MODE`) and a
  per-run replay set: replay transports, placeholder credentials, and Endpoints A/B at the URL their
  exchanges were recorded against, read from the recordings. The HF Endpoints API is never called. Nothing
  in the replay set can open a connection.
- **Replay miss.** In the per-run replay set, a miss (a free-text prompt) is answered by the simulated
  adapter for the same model (the simulated CIM for Endpoint A), labelled `simulated`. It is not a fallback
  to another model: the model, the policy checks before the call and the metering are unchanged. The
  configured set stays strict, so CI's own replay mode still catches stale recordings.
- **Resolution** (`inference/modes.py`, at ingress):
  - with no request, a public visitor runs in replay and everyone else in the configured mode;
  - `replay` always uses the per-run replay set;
  - `live` needs a live-capable gateway (configured `live` or `record`), else 409
    `EXECUTION_MODE_UNAVAILABLE`, and Govern's gate (`authorize_execution_mode`: 403 `LIVE_NOT_PERMITTED`
    for a user without `chat:live`; application credentials keep the configured mode). It is never
    silently downgraded.
- **Recorded as.** `ChatResponse.execution_mode` and span attribute `aiva.execution_mode`. Provenance still
  says what each value is. The mode is part of the response-cache scope, so answers never cross modes. FinOps'
  money domain follows it (`run_domain(ctx.execution_mode, ...)`): only live and record runs spend money.
- **Never wake.** A public visitor's live run reads endpoint state from the HF Endpoints API (cached 30 s;
  an API error counts as asleep) and never calls a scaled-to-zero endpoint:
  - once the path is fixed, and before the first billable stage, the upstream RLCD will select is known (its
    plan does not depend on the CIM's output). If it is a sleeping endpoint, the run is refused with nothing
    reserved or metered: a blocked run with `LIVE_DEMO_UNAVAILABLE` (reason `endpoint_cold`, not retryable,
    outcome `error`; `contracts/demo.py`). This holds on the sovereign, local-fast and CIM-assisted paths.
    The orchestrator has no fallback for an unavailable upstream, and the sovereign path never falls back;
  - a sleeping Endpoint A, read in the CIM stage before its reservation, is bypassed ("CIM bypassed: the CIM
    endpoint is asleep and is not woken; full context used");
  - final routing checks the selected model again, as a backstop for a budget reservation that narrowed
    the eligible set.
- **Invariant.** On a gateway configured live with live credentials present, a replay run, recorded or not,
  makes no DNS lookup or connection outside the host (`invariants/test_execution_mode.py`).

*Reranker default* (owner decision, measurements of 2026-10-15):
- **Configuration.** Measured offline on a 4 vCPU container: the gateway's retrieval over the 514-chunk
  synthetic KB (`data/synthetic/kb/gct_kb.jsonl`) with bge-base embeddings, BM25 and RRF, and
  `bge-reranker-base@2cfc18c9415c` (fp32 ONNX, 2 threads unless noted). Queries and gold come from ML & Data's
  121 held-out enterprise-rag cases (`s-ttp/aiva-cim-sft@0b786e807f5c`, 179 gold passages). Provenance:
  measured (offline); the Wave 3 benchmarks repeat it live.
- **Gold mapping.** 139 of the 179 gold sections are in the KB with identical text; 40 come from 5 documents
  not in the KB. Every policy exists in 10 near-identical entity versions and no question names the entity,
  so exact-document recall is close to chance; *family recall* counts the right section of the right
  policy for any entity.
- **Retrieval** (recall at k passages; latency per request):

  | configuration | family R@2 | family R@4 | family R@8 | exact R@2 | passages returned | rerank p50 / p95 |
  |---|---|---|---|---|---|---|
  | fusion only (no reranker) | 43.6% | 49.7% | 61.5% | 14.5% | 8.0 | – (retrieval p50 52 ms) |
  | reranker, 16 candidates | 43.6% | 47.5% | 53.1% | 12.3% | 5.5 | 1,543 / 1,895 ms |
  | reranker, 8 candidates | 44.1% | 49.2% | 59.8% | 12.3% | 5.0 | 825 / 984 ms |

  Pairs are about 100 tokens (p95 124), so a shorter max_length changes nothing; more threads speed one
  request but not throughput on a CPU-bound Space.
- **Recall floor** (`keep_top_k`, served CIM predictions on the same 121 cases, Guard v3; every case passes):

  | k | ranking | tokens saved | gold passage recall | gold facts covered |
  |---|---|---|---|---|
  | 1 | reranker | 64.3% | 85.5% | 85.5% |
  | 1 | fusion | 63.4% | 85.5% | 85.5% |
  | 2 | reranker | 53.7% | 94.4% | 94.4% |
  | 2 | fusion | 53.1% | 94.4% | 94.4% |

- **Decision.** The reranker never improved top-2 recall and lowered recall at 4 and 8 (its relevance floor
  drops passages), at about 1.5 s p50 and 1.9 s p95 per RAG request. It is off by default and kept as an
  option for knowledge bases where a cross-encoder helps. The k = 2 floor holds with fusion ranking.

*Response caches* (Redis). Exact keys hash scope, policy bundle hash, KB version, allowed models, messages,
evidence ids **and content hashes**, and max tokens. Personal requests (any first-person reference in a user
message, DLP identifiers, fraud, confidential or above) are cached per end user only, and not at all
without an end-user identity; this fails closed (#29). Applications with `cache_sharing: per_user` (the
customer-facing support chatbot) cache every answer per end user. Public demo visitors (ADR-0019) all hold
the same role, so their cache is scoped to the visitor session's own pseudonym ("Scoped to the visitor
session"): a visitor's first run of a scenario takes its real path, and only their own repeats hit, exact or
semantic. `baseline_comparison` is part of the exact
and semantic keys, so a request for the comparison is never served an answer cached by a run that made no
baseline call (nor the reverse). Other requests share within tenant + application + role set + classification
(LLD §11). Semantic reuse only for single-turn, evidence-free,
DLP-clean, public/internal, non-personal requests, with cosine ≥ 0.95 **and** an identical entity signature (numbers,
codes, proper nouns, negations): bge rates "roaming rates for Europe" vs "…for Asia" at 0.939. A cache hit
keeps the provenance of the run that produced it. The key is built at lookup, before RLCD runs; if RLCD then tightens the effective classification, the answer is not stored, so it can never be served to a lookup at the lower level. Redis stores vectors and signature hashes, never prompts;
a Redis failure is a miss.

*RLCD v1* (LLD §10; model and thresholds from ML & Data, ADR-0013):
- **Model.** `s-ttp/aiva-rlcd-modernbert`@`cd53974b9859`: ModernBERT-base with four calibrated heads (task
  category, sensitivity, complexity, context need). It runs in-process on CPU and is loaded exactly as
  `ml/jobs/rlcd_predict.py` loads it: masked mean pooling, then softmax(logits / temperature).
- **Input.** Every message as `[role] content`, in order (the training format).
- **Loading.** The pinned revision downloads with `HF_TOKEN` and loads in the background at startup (about
  16 s on 4 vCPU). With `AIVA_RLCD_BACKEND=model`, `/health/ready` is 503 while the model loads
  (`checks.rlcd_model = "loading"`), so the Space proxy answers `GATEWAY_STARTING` (retryable). A failed
  load (for example a Hub outage or a token scope problem) is ready but degraded: `rlcd_model` reads
  `failed`, the reason is in the log, and the labelled heuristic serves, so replays it routes miss honestly
  and are labelled `simulated`. Deploy's smoke test requires `rlcd_model == "ok"`. As a backstop, a run that
  still arrives while the model loads waits for it, up to 60 s.
- **Inference.** It runs on one worker thread, never on the event loop, with as many torch threads as the
  container's CPU quota allows. `os.cpu_count()` reports the host's cores; 64 threads on 4 CPUs took 4.5 s per
  prediction instead of 40 ms.
- **Identical inputs, identical decisions.** Runs with the same input share one prediction while it is in
  flight, and later runs reuse it: the last 1,024 predictions are remembered in memory, by input hash.
- **Time budget.** 2 s of the model's own time per input. Waiting for the worker behind other inputs does not
  count, but is capped at 30 s. A prediction over budget is dropped for that run; when it completes, later
  identical runs reuse it. A failed prediction is not remembered.
- **Fallback.** Without a prediction the router is the P0 heuristic, recorded as `heuristic-v0` and never
  as the model. The `rlcd1` span says why (`aiva.rlcd.fallback`: `loading`, `unavailable`, `queue`,
  `timeout` or `error`). Such a run is not stored in the response cache while the model is serving, because
  identical requests must not inherit a weaker classification from it.
- **Why** (Scenarios' S1 rehearsal, 2026-10-16). The S1 sample prompt flipped between sovereign-direct
  (confidential) and local-fast (internal). The model scores it confidential at 0.9994, identically on 1, 2
  and 4 threads. The flips were runs without a prediction:
  - runs during the load;
  - runs whose 2 s budget ran out while queued behind other runs (21 of 40 concurrent S1 runs);
  - and possibly oversubscribed torch threads.
  The response cache then served those runs' internal answers to later identical requests.
- **Thresholds** (validation split): task category 0.60, complexity 0.80, context need 0.60. A head below
  its threshold is `unclassified`:
  - the run record stores no label for it, so Insights reports `unclassified`;
  - routing keeps the heuristic's value, so an unclassified complexity cannot pick a costlier model or open
    the local fast path.
- **Declared task.** A caller's declared task wins over the predicted one.
- **Sensitivity is tighten-only.** The prediction is the highest level L with P(class ≥ L) ≥ 0.5. The
  orchestrator applies it only if it is stricter than the effective classification, then re-evaluates
  policy. One parity row is a confident miss (gold confidential, predicted internal at 0.9997); DLP and the
  declared label still decide that run.
- **Run record.** Head confidences are recorded as `measured`, with the model revision as the classifier
  version.
- **Backend** (`AIVA_RLCD_BACKEND`): `model` (the gateway Space), `fixture` (ML's recorded parity outputs,
  used offline in CI) or `heuristic` (the default).
- **Dependencies.** PyTorch (CPU wheel index) and transformers are the optional `rlcd` extra
  (`uv sync --all-packages --extra rlcd`), so CI needs neither them nor the 596 MB weights.
- **Parity check.** `AIVA_RLCD_PARITY=1 uv run pytest gateway/tests/live/test_rlcd_parity.py` reproduces
  ML's 20 parity rows (identical argmax, probabilities within 1e-3).

*Retrieval.* BM25 (in memory per tenant) + pgvector HNSW cosine, reciprocal-rank fusion, and an optional
cross-encoder rerank with a provisional relevance floor (off by default since the measurements below;
`AIVA_RERANKER_BACKEND=onnx` turns it on). Embeddings `BAAI/bge-base-en-v1.5` and reranker
`BAAI/bge-reranker-base` run in-process on CPU via ONNX Runtime at pinned revisions; no text leaves the
gateway for embedding. Access control is applied before ranking: tenant, application grant, validity
dates, DLP-clean chunks and classification public/internal only (retrieval runs only on the CIM-assisted
path, so confidential documents can never be pulled into a CIM or external request). The KB version is part
of every cache key. KB input is ML & Data's JSON Lines export, one row per section (`doc_id, version,
title, section, text, classification, applications[]`), loaded with `aiva-admin kb-ingest`.

**Consequences.**
- CI never spends; `AIVA_RECORD_PROVIDERS=1 uv run pytest gateway/tests/live` re-records (cents).
- Changing the CIM prompt or a recorded request invalidates its recording (re-record).
- Breaker state is per replica; a shared breaker needs Redis if Space 2 scales out.
- Rate card gaps for FinOps: Anthropic cache-write tier (1.25×) is not priced (no cache_control is sent,
  so writes are zero today); the HF router also reports an `estimated_cost` usable for reconciliation.
- Space 2 downloads ~1.5 GB of ONNX weights on first retrieval/semantic-cache use (pre-warm in deploy).
- RLCD v1 adds the `rlcd` extra to the gateway image: torch CPU (~730 MB installed) and transformers. It
  also adds the 596 MB model, downloaded at startup or baked in via `AIVA_RLCD_LOCAL_DIR`, and about 1.6 to
  2.0 GB RSS. Measured on 4 vCPU: p50 218 ms and p95 398 ms per prediction.
- Live runs remain run-level `simulated` until the budget reservation is no longer simulated (FinOps).

## Amendment (2026-10-18): the baseline comparison's own output budget and effort

**Context.** The opt-in baseline comparison sends the run's upstream messages to the pinned frontier model
(Claude Opus 5.5). It shared the request's `max_output_tokens` (default 1,024), and Opus 5.5 thinking cannot
be disabled and counts against `max_tokens`. Three of the five shipped baseline recordings stopped at
`max_tokens`; S5's spent all 1,024 tokens on thinking and returned no text. The five recorded request bodies
were re-sent on 2026-10-18 with `max_tokens` 4,096 (usage read only, about $0.42):

| Baseline | Effort low: output (thinking) | Effort medium: output (thinking) |
|---|---|---|
| S2 | 3,512 (365) | 4,096 (2,100), stopped at `max_tokens` |
| S4 | 720 (95) | 780 (181) |
| S5 | 1,741 (424) | 2,132 (705) |
| S6 chat | 1,436 (170) | 1,634 (218) |
| S8 | 453 (101) | 427 (116) |

For S2, S5 and S6 the answer alone exceeds 1,024 tokens at either effort, so neither a lower effort nor more
room alone is enough.

**Decision.**
- The baseline call gets max(the request's `max_output_tokens`, `baseline.max_output_tokens`) and effort
  `baseline.effort` (`config/models.yaml`: 6,144, about 1.75 times the largest measured, and `low`). The
  answering model, Claude included when RLCD selects it, keeps the request's budget and
  `AIVA_ANTHROPIC_EFFORT`.
- The budget is computed once, when the reservation is built (`RunContext.baseline_max_output_tokens`). The
  envelope's baseline line and the call use the same number, because preflight checks a reservation's
  coverage by model, not amount. The request cap and every budget apply to the larger line.
- The inference span records `aiva.baseline.max_output_tokens` and `aiva.baseline.finish_reason`.

**Consequences.**
- At the default request budget the reserved worst case of a baseline run rises by (6,144 − 1,024) ×
  $20/MTok, about $0.10, released at settlement.
- Measured spend of the five scenario baselines rises from $0.133 (truncated) to $0.200. The baseline call is
  overhead, reported separately and never as a saving (ADR-0015), so the modelled savings do not change.
- Every baseline request body changes, so its recordings are re-recorded: the public replay set's S2, S4,
  S5, S6 chat and S8 baseline variants, and the CI fixtures. Until then those baseline attempts miss replay.
- Effort `low` trades reasoning depth for a complete answer; baseline outputs are not graded in this phase.

## Amendment (2026-10-18): heading-aware fusion, off until the next CIM swap

**Context.** Retrieval fuses BM25 and vector rankings (RRF). The deployed KB holds 8 policy families in 10
operating-unit versions each (GCT Group is the canonical version, e.g. GCT-POL-014). Those versions share almost
all their words, so the right version's answering section often ranks below another unit's. S4 is the visible
case: GCT-POL-014 §3 ranks third, behind GCT-POL-120 and GCT-POL-118 sections, so the recall floor (top 2)
does not keep it, and the CIM omits it.

Measured on 2026-10-18 at no cost. The queries are the 150 enterprise-rag rows of ML & Data's v6 test split
(`s-ttp/aiva-cim-sft@7c3b65c`), whose gold chunks are all in the deployed KB. Retrieval used the pinned ONNX
embedder, no reranker, limit 8:
- 66 questions name a unit; their target is that unit's answering chunk.
- 84 name none; their target is the GCT Group version's answering section, as S4 expects.

| Metric | Fusion | + headings |
|---|---|---|
| Named unit: strict recall @2 / @4 / @8 | 0.70 / 0.81 / 0.89 | 0.68 / 0.77 / 0.86 |
| Named unit: other units' chunks in the top 8 | 5.9 | 6.1 |
| No unit: GCT Group target @2 / @4 / @8 | 0.10 / 0.18 / 0.48 | 0.20 / 0.40 / 0.54 |
| No unit: unit versions covered in the top 8 (of 10) | 6.9 | 7.2 |
| S4: rank of GCT-POL-014 §3 | 3 | 1 |

**Decision.** `AIVA_RETRIEVAL_HEADING_FUSION` adds a third ranking: BM25 over each passage's section heading
alone (the text after " - " on its citation header line).
- It is off by default, and off is byte-identical to fusion.
- When on, the setting is part of the KB version, so answers retrieved under one ranking are never served from
  the cache as answers of the other.
- It is enabled together with the next CIM swap, so S2, S4 and S8 are re-recorded once.

*Scoped per context profile (2026-10-18, with the v7 swap).* Enabled for every request, heading fusion also
reorders the retrieved passages of S6 (network operations), which bypasses the CIM, so its full-context bodies
change on all three S6 requests. The ranking was never measured there. So it is a context-profile setting
(`heading_fusion`), on only where it was measured: `enterprise-rag` (S4). `AIVA_RETRIEVAL_HEADING_FUSION` stays
off; set, it still applies to every request. The KB version includes the opted-in profiles, so answers
retrieved under one ranking are still never served from the cache as answers of the other. S8 has no context
profile and keeps plain fusion.

**Consequences.**
- **The gain:** questions that name no unit find the canonical version twice as often in the top 2.
- **The cost:** questions that name a unit lose 1 to 3 of 66 in strict recall, within the noise of this set.
- **S4 recovers through the recall floor, not through better CIM selection.** At rank 1, GCT-POL-014 §3 is
  kept whatever the CIM selects. ML & Data's gate still judges the CIM's own selection.
- **Unit confusion itself is not solved by headings.** About 6 of the top 8 chunks still come from other
  units' versions on named-unit questions. An entity-aware ranking measured far better: named strict@2 0.88, and
  GCT Group target@2 0.77. It needs a per-document unit field (a KB schema change) and query-side unit
  detection, so it is a separate decision after the next CIM.

## Amendment (2026-10-22): no unverified trim is served, and routing counts in-house GPU time

Owner-approved after nine live answer-quality comparisons on Space 2 (ADR-0023). Three runs scored 45 to 54 for
reasons in this ADR's scope:
- **A frontier-optimization run** sent 1 of 65 passages (157 of 6,155 tokens). Its sufficiency was
  `cim-selected` (relevance not verified), and it scored 45; the grader named two facts the dropped passages
  held. The same question with the full context scored 100.
- **Two free-text questions** routed to the sovereign Qwen on its $0 token rate ("lowest blended price") and
  scored 52 and 54: each gave one entity's fee or deadline where the sources differ by entity. The two runs
  Gemini Flash-Lite served scored 85 and 93.

**Decision 1: a package that omits passages on the CIM's judgement alone is never served.**
- A package the Guard passes but whose `relevance_unverified` is not empty is treated as a failed check:
  sufficiency `failed-fallback-applied`, copy "Context sufficiency check failed; safe fallback applied"
  (LLD §22.8), and the full eligible context is sent.
- Full context, rather than the CIM's selection plus the unverified passages: putting back every passage the
  Guard cannot rule out leaves out at most the few passages it can (those with no citation header, which pin no
  required value). Full context is already within the reservation, never under-serves, and is what a failed
  check sends.
- `relevance_unverified` holds every passage the package leaves out on the CIM's judgement alone: those it
  lists as omitted and those it does not mention. A v2 selection can name a passage in neither list: CIM v7
  did on 4 of ML & Data's 150 held-out RAG cases, and 2 of those passages were needed (measured offline,
  `s-ttp/aiva-eval-runs` `cim-v7-test-v1-vs-v7`).
- A package with nothing unverified is unchanged (`verified`).
- The run record keeps `relevance_unverified` (count and ids), and `sufficiency_basis` reads "relevance-unverified:
  ... full context sent". `cim-selected` stays in the contract for runs recorded before this amendment.
- **Consequence: the frontier-optimization use case saves no tokens.** All 65 of its passages carry a citation
  header, so any passage the CIM omits is unverifiable, and every run now sends the full context (0 tokens
  saved; it was 6,155 to 157). So does S8, the shipped replay set's only trim (1 of 8 passages, 658 to 123
  tokens). The CIM call is still made and metered. Savings come back only when the Guard can
  verify that an omitted passage is irrelevant, which is a separate decision.

**Decision 2: RLCD compares the estimated cost of serving the request, an in-house model's GPU time included.**
- Before, it compared blended list prices per million tokens, so a GPU-served model priced at $0 per token won
  every request it qualified for.
- `optimize.rlcd.request_costs`: for each qualified model, its list-price tokens at the request's size (the
  full eligible context's input bound and the request's `max_output_tokens`), plus, for an in-house model,
  FinOps' per-attempt GPU estimate (`PriceBook.gpu_worst_case_micros`, the reservation's: 120 s at the
  endpoint's hourly rate). It is sized on the full context, so the choice does not depend on the CIM's output.
- At 6,000 input and 1,024 output tokens: Gemini Flash-Lite $0.0010, DeepSeek $0.0018, Claude $0.0451, the
  sovereign Qwen $0.0833. Everyday standard-tier requests go to Gemini and high-tier ones to DeepSeek.
- **The ceiling overstates GPU time.** The outcome does not depend on it: even at 10 s of A100 time Qwen costs
  $0.0069, several times Gemini's or DeepSeek's tokens. It can rank Claude below Qwen, which matters only where
  a request may use those two alone. No application allows Qwen without Gemini and DeepSeek today. A typical
  GPU time per request from FinOps would sharpen it (follow-up).
- **Policy is unchanged.** RLCD still chooses only inside the policy-eligible set. The sovereign direct path
  offers only the sovereign model, which serves confidential, restricted and sovereign-only requests with no
  fallback (invariant). The local fast path, which offers only in-house models, is unchanged.
- The plan's reason now reads "lowest estimated cost per request among qualified models: <model>".
- **Consequences for the demo scenarios** (measured live on 2026-10-22, after the first re-record failed its
  gates):
  - **Gemini Flash-Lite writes longer alarm lists than the sovereign model did.** S5's correlation ran to
    1,035–1,942 output tokens over three takes (about 2.5 characters per token: alarm ids and times) and stopped
    at the default 1,024; the sovereign model had answered in about 600. S6's analysis step ran to 638 against
    the workflow default of 600. Flash-Lite does not think unless asked, and the adapter does not ask: no
    recording has thought tokens, so the whole budget is answer. S5 now caps at 3,072, and the Control Center's
    workflow default is 1,024. These are scenario settings; no gate changes.
  - **Gemini Flash-Lite rarely cites unprompted.** On S4's eight shipped passages it cited GCT-POL-014 in 0 of 6
    answers to the bare question and 2 of 6 when asked. DeepSeek cited it in 3 of 6 and 6 of 6, Claude in 2 of
    2. S4, whose proof is citations, now asks for one and runs at the high tier (DeepSeek). Routing citation
    profiles to a model that cites, in general, is a separate decision (follow-up).

**Recordings.** The CIM's requests are unchanged. Decision 2 moves S4, S5, S6 and S8 from the sovereign model
to Gemini Flash-Lite, and decision 1 sends S8 the full context (its answer and its baseline). So 9 of the 17
shipped replay requests miss: S4, S5, S6 and S8, each with its Baseline-comparison variant, and S6's workflow.
The other 8 replay byte-identical bodies. Those four scenarios are re-recorded with the scenario-test and
endpoint-test fixtures, quoted to the owner first. The recorder (`tests/e2e/record_scenarios.py`) follows: each
route names its answering model, so re-recording them wakes Endpoint A only, and S8's safe fallback is its
honest recorded state.

## Amendment (2026-10-22, option A): the Guard verifies an omission by the question-term rule

Owner-approved as Guard option A after #204: a passage the CIM leaves out may be verified irrelevant by a
deterministic rule, so a package can save tokens again without serving an unchecked trim.

**The rule** (`context_guard.omission_verified`). A left-out passage is verified irrelevant only if all of these
hold:
- it has a citation header;
- it shares no content term with the question (every user turn) or with the CIM's required facts. Citation
  fields are excluded, since they name sources;
- it is no sibling of a kept passage, i.e. not in the same document family: the header's title without its
  entity, so every section of every entity's version of a policy is one family.

Content terms are lowercased words of three or more letters and any number, plural-stemmed. They exclude
function words, the tenant's name ("gct") and the POC's data label ("synthetic"), which are on nearly every
passage and question, and citation brackets. The Guard reports such passages in `relevance_verified`, and
every other left-out passage in `relevance_unverified`.
- **A package with no unverified omission is `verified`.** The run record's basis reads "question-term-verified:
  …" and keeps both id lists. The span carries `aiva.guard.relevance_verified`.
- **Put back what the rule cannot verify.** When a package leaves out passages the rule cannot verify and others
  it can, the orchestrator puts the unverified ones back (`with_passages`) and drops only the verified ones. The
  Guard checks the result again, because a passage put back can make another one its sibling, and repeats
  until nothing unverified is left out. That re-check is bounded by the full context, which the fallback
  would send, not by the CIM's target budget: a package with passages put back is never larger than the full
  context. The package is then `verified`, and the record lists `relevance_put_back`. When no left-out passage is verified, putting everything back would be the full
  context, so the full context is sent (decision 1). This refines decision 1, which sent the full context only
  because the Guard could then rule out almost nothing.

**Calibration.** Measured offline, $0. ML & Data's served CIM predictions were run through the gateway's code
(recall floor k = 2 on fusion ranking, Guard v3, full-context fallback). Gold recall counts the passages each
case needs:

| CIM predictions (cases) | served before #204: saved / gold recall | decision 1: saved / recall | rule alone (all or nothing): saved / recall | **rule with put-back**: saved / recall | rule errors |
|---|---|---|---|---|---|
| v1 (121 held-out RAG cases, `aiva-cim-sft@0b786e8`) | 53.1% / 94.4% | 0.0% / 100% | 0.6% / 100% | **19.4% / 100%** (104 cases trimmed) | 0 of 444 omissions |
| v7, served (150 cases, `aiva-cim-sft@da9e8a9`, `cim-v7-test-v1-vs-v7`) | 59.5% / 78.0% | 0.0% / 100% | 0.3% / 100% | **12.6% / 100%** (129 cases trimmed) | 0 of 1,023 omissions |

A rule error would be a needed passage that the rule verified irrelevant. Prefix-5 stemming, which matches more
aggressively, verifies slightly fewer passages and also makes no error.

**Why put-back.** Most held-out questions name no entity, so the CIM keeps one entity's version of a policy
and omits the others. Those omissions are siblings and cannot be verified: 730 of 1,023 in the v7 set. Alone,
the rule saves only where every omission is verified. With put-back, a package still drops every passage the
rule verifies.

**The shipped replay set saves 0 tokens.** S8's seven omissions are all sections of the kept roaming-bundles
document, so they are siblings. None is verified, so S8 still sends the full context. No upstream body of the 17
replayed requests changes.

## Amendment (2026-10-22, grounded answers): routing and temperature by what the answer is grounded in

Owner-approved (option R′, through the coordinator) after two live answer-quality comparisons on Space 2
(ADR-0023) contradicted each other: the cost-optimized API sample scored 93 at 05:08 and 25 later, when Gemini
Flash-Lite answered "please specify which GCT entity" instead of giving each entity's fee.

**Measurement** (local, $0.107; no endpoint, nothing on Space 2). S2's exact upstream bodies were sent N=5 times
per arm. They were captured through a local gateway (the API sample's 8 passages at 727 / 793 tokens, as on S2)
or taken from the shipped replay set (#209). The pinned grader needs an OpenAI key the container does not have,
so answers were scored as follows:
- a fact rubric from the passages: the API sample's 7 entity fees, Oman's 48 hours and "no other timelines";
  the free-text question's 8 entity windows and "no refund timeline". It scores S2's 25-point answer 0, and a
  complete answer 89 (it under-credits one "not given" phrasing);
- whether the model asked instead of answering;
- the recorder's grounding gates, invented facts and truncation.

Gemini Flash-Lite:

| arm | API sample min / mean (asked) | free text min / mean | S8 gate fails | S5 gate fails (at the cap) | S6 chat / analyse fails |
|---|---|---|---|---|---|
| default (1.0) | 0 / 16 (3) | 0 / 71 | 3/5 | 2/5 (2, at 3,072) | 0 / 0 |
| 0.3 | 0 / 20 (4) | 89 / 89 | 5/5 | 0 (0) | 0 / 0 |
| 0 | **0 / 0 (5)** | 89 / 89 | **5/5** | 0 (0) | 0 / 0 |
| "answer for each entity" instruction, 1.0 | 0 / 42 (1) | 89 / 93 | 3/5 | 0 (0) | 0 / 0 |
| instruction, 0 | 0 / 0 (5) | 89 / 89 | 0/5 | 0 (0) | 0 / 0 |

- Its modal API-sample answer asks which entity. Temperature 0 makes that deterministic, and the instruction
  does not override it.
- Its modal S8 answer says the evidence lacks the rate, which is wrong: AED 160.00 is in the passage.
- At its default temperature S5 sometimes lists every alarm, unrelated ones included, until it hits the cap.

DeepSeek V4.1 Flash, same bodies:

| arm | API sample | free text | S8 gate fails | S5 gate fails | S6 chat (1,024) | S6 plan / analyse / notice |
|---|---|---|---|---|---|---|
| default | 89 on 5 of 5 (asked 0) | 89 on 5 of 5 | 1/5 | 2/5 (invented "P1-class") | 1/5 (stopped at 1,024) | 0 / 0 / 0 |
| **0** | **89 on 5 of 5 (asked 0)** | **89 on 5 of 5** | **0/5** | **0/5** | **1/5 (invented "approximately 06:30 UTC"; the record says 06:42)** | 0 / 0 / 0 |

Neither model is best everywhere. **The split is the kind of evidence:**
- On **knowledge passages only** (the API sample, the free-text question and S8), DeepSeek at 0 is complete
  and Gemini is not, at any temperature.
- On **operational records** (S5, S6 chat, S6 analyse), Gemini is clean, and at 0.3 S5's runaway lists stop.
  DeepSeek invented a time on S6 chat. Gemini at 0.3 was also checked on S6 plan and notice: 0/5 fails each.

**Decision 1: what an answer is grounded in.** `optimize.rlcd.grounding` reads it from the request's full
eligible evidence (supplied, retrieved and web passages), never the CIM's selection, the same way RLCD's costs
are sized:
- `knowledge`: every passage opens with a citation header (KB sections and web results). This is the line the
  Context Guard already draws.
- `records`: at least one passage has no citation header (incident and site records, alarms, tool outputs).
- `none`: no evidence.

S5 and S6 retrieve eight KB passages each beside their records, so the rule is "any record present", not
"any knowledge passage present"; otherwise they would move as well. S6's plan step is sent only the incident
id and retrieves KB passages only, so it is `knowledge`.

**Decision 2: qualification by grounding.**
- `models.yaml` `grounding.<kind>.qualified: false` takes a model out of RLCD's qualified set for that kind.
  Gemini Flash-Lite is not qualified for `knowledge`, so knowledge-grounded standard-tier requests go to the
  cheapest qualified model, DeepSeek. Requests grounded in records, and ungrounded ones, keep Gemini.
- RLCD still chooses only inside the policy-eligible set. If no other eligible model qualifies, the unqualified
  one still serves, and the plan says so: RLCD never refuses for want of a better model.
- A caller's preferred model is honoured only among qualified models, as with the quality tier.
- The plan's reason names the exclusion: "answer grounded in knowledge passages only: gemini-flash-lite not
  qualified".
- The sovereign direct and local fast paths are unchanged: Qwen carries no grounding settings.

**Decision 3: answer temperature by grounding.**
- `grounding.<kind>.temperature` is sent with the upstream call (`InferenceRequest.temperature`, an additive
  field). The settings are:
  - DeepSeek: 0 for `knowledge` and `records`;
  - Gemini: 0.3 for `records`;
  - everything else: the provider's default, as is every ungrounded request.
- The baseline call keeps its default, and Claude takes none: its thinking is always on, so the catalog refuses
  a temperature for an Anthropic model.
- The OpenAI-compatible adapter sends `temperature`, and the Gemini adapter `generationConfig.temperature`.
  `None` leaves the field out, so ungrounded request bodies are byte-identical to before.

**Cost.** DeepSeek's tokens cost twice Gemini's. That is about $0.0001–0.0003 more per request at the API
sample's size, and $0.0008 at 6,000 input and 1,024 output tokens ($0.0018 against $0.0010).

**Assurance: citations without brackets.** DeepSeek writes citations as headings ("**GCT-POL-014 §2 (GCT Group)**")
and short forms ("POL-118 §3–§4"), so S4's recorded answer reported `answer_citations: none`.
`assurance.citations` now also reads a bare `DOC-ID §N`:
- It counts only when its document is one the model was given: the same ID, or the only given ID ending in
  `-<ID>`.
- A list cites each section, and a range each section in it.
- A bare reference to a document the model was not given is ordinary text.
- The check stays model-agnostic and report-only. S4's recorded answer now resolves 8 of 8.
- Added 2026-10-25: a record named by its own identifier cites its passage. That is the last segment of a
  path-shaped evidence ID (`ALM-7790` for `noc/ALM-7790`), when it has a letter and a digit and names one
  candidate only. S5's T=0.3 answer writes alarm IDs that way (Gemini did in 4 of 5 takes at its default
  temperature too), so it read `none`. It now resolves 6 of 6. No upstream body changes.

**Consequences for the demo scenarios.** Routes on the deployed configuration:
- **S8 and S6's plan step** move from Gemini to DeepSeek at 0.
- **S5, S6's chat, and S6's analyse and notice steps** stay on Gemini, at 0.3.
- **S4 and market research** (high tier, already DeepSeek) now run at 0.
- **The live API sample** (S14) moves to DeepSeek.
- **S1, S3, S9, S10 and S2** (sovereign, or Claude at the critical tier) are unchanged.

**Recordings.**
- These shipped replay requests miss by design, each with its Baseline-comparison variant where it has one: S4, S5, S6 chat, S6's workflow and S8. Their CIM and baseline bodies
  are unchanged; only the answering call differs.
- The scenario-test and endpoint-test fixtures they touch were re-recorded with this change (about $0.02).
- The recorder names each workflow step's answering model (`WORKFLOW_SERVED`).

**Not chosen:**
- Lowering Gemini's temperature alone (option T) locks in its modal answers on knowledge passages.
- An instruction to answer for each entity (option I) does not change them.
- Routing every grounded request to DeepSeek (option R) fails S6's chat 1 time in 5 on an invented time.

## Amendment (2026-10-25): coding requests do not search the knowledge base

Scenario 16's coding questions each drew 8 retrieved passages on the CIM-assisted path. The CIM is bypassed
for code (`cim_trained_tasks`), so all 8 reached the frontier model as full context, and on stage the SQL
answer opened by saying none of them applied. The synthetic KB holds tariffs and policies, with no
technical material, so for code a retrieved passage is noise. It also sends internal KB text to an external
model for nothing. S2's 8 roaming-bundle and policy passages, with their preamble, were 1,255 of its 1,428
input tokens.

**Decision.** `config/context_profiles.yaml` `retrieval_skip_tasks` lists the task categories (taxonomy
`task_category`) whose CIM-assisted runs search neither the knowledge base nor the web. It holds `coding`.
- The task is the run's own: declared, or the RLCD's prediction.
- The retrieval stage is skipped with "Knowledge base not searched: coding requests do not use it", and
  `passages_retrieved` is null, as on any run without retrieval.
- Evidence the request supplies is still sent.
- The CIM bypass and its message are unchanged, because the task check runs before the evidence check.

**Why a list, not "outside `cim_trained_tasks`".** Retrieval grounds the answer; the CIM trims context. The
other untrained task, `generation`, often needs the KB ("draft a notice about the roaming change"). And
training the CIM on coding later would otherwise switch retrieval back on silently.

**Recordings.** S2's shipped Claude exchange was re-recorded without the passages (1 take, $0.024). Its
Baseline-comparison variant sends the same body. The S2 scenario-test fixture (DeepSeek, now ungrounded,
so at its default temperature) was re-recorded too. The other 15 shipped requests replay byte-identical.

## Amendment (2026-10-27): RLCD v2, retrained on natural requests

v1 was trained on template-shaped prompts and does not generalise to the way people write. On ML & Data's
frozen natural test split (2,233 synthetic rows) its task macro-F1 is 0.221. It routed 45.2% of
confidential-or-above requests too low even with DLP alongside, and every sovereign-only one. The owner
approved a retrain as a drop-in replacement.

**Decision.** The gateway loads RLCD v2: `s-ttp/aiva-rlcd-modernbert`, the pin in `optimize/rlcd_model.py`
and `config/models.yaml`.
- **Drop-in.** The heads, classes, class order, `[role] content` rendering, mean pooling, `max_length` 1024 and
  file layout are v1's, so the loader and `ModelRouter` are unchanged.
- **Measured** on the frozen natural test split, scored once (`ml/reports/rlcd-v2-gates-run1.md`):
  - macro-F1: task 0.934, sensitivity 0.983, complexity 0.816, context need 0.981;
  - confidential-or-above requests routed too low, with DLP alongside: 0.5%;
  - CPU p95 on the gateway's flavour: 530 ms.
  - The rows come from the same generator as the training data. On 16 fresh hand-written prompts the
    coordinator's check scored about 70-80%, so real traffic scores lower.
- **Thresholds unchanged.** `MIN_CONFIDENCE` and `SENSITIVITY_TIGHTEN_AT` were re-checked on v2's calibrated
  validation curves (realistic prior). The figures are in the code comments; a lower tighten cut-off gains
  at most 0.2 points. Sensitivity still only tightens.
- **Routes.** Every demo scenario's sample prompt, every live demo alternate and the document copilot's
  artifact request route as before under v2: the same path, model, effective classification and CIM state.
  This was checked in-process on ML's recorded v1 and v2 outputs, and the 17 shipped recordings replay
  byte-identical with v2 routing. Free-text requests change as intended: v2 labels three card probes
  `coding` (so the knowledge base is not searched and the CIM has nothing to package) and one `generation`
  (a task the CIM is not trained on), on the same model.
- **Parity.** `gateway/tests/fixtures/rlcd/rlcd-parity-v2.jsonl` holds ML's 85 rows with v2's outputs, and
  v1's beside them. `FixtureRlcd` replays them in CI; `tests/live/test_rlcd_parity.py` reproduces them
  from the pinned weights.
- **Rollback.** v1 (`cd53974b98590daaf6d53b8952f6f9883d98a1c6`) is kept privately at
  `s-ttp/aiva-rlcd-modernbert-v1@db20e9922bbd`. A rollback reverts this change and points `RLCD_REPO` and
  `RLCD_REVISION` there.
