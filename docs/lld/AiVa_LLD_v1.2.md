<!-- Source: AiVa_AI_Value_Assurance_LLD_v1_2.docx (14 October 2026), converted to Markdown.
     This file is the normative design reference for all workstreams. Do not edit the substance;
     record deviations and decisions in docs/adr/. -->

# Low-Level Design (LLD) | Enterprise AI Intelligence & Control Platform

GulfConnect Telecom (GCT) — fictional, synthetic proof of concept

Version 1.2 \| Brand and UI design baseline \| 14 October 2026

Status: proposed design, not deployed or benchmark-verified \| Working brand; trademark clearance pending

# 1. Executive design decisions

- AI Policy Enforcement Layer (AI-PEL) is the non-bypassable authority for identity, source IP, task/model/tool authorization, data sovereignty, prompt security, quotas and audit.

- RLCD is a ModernBERT-based, calibrated execution-plan optimizer; it may only select policy-eligible actions.

- Generalized Context Intelligence Model (CIM) is a fine-tuned Qwen3-4B-class model for cross-industry evidence selection and context packaging; GCT-specific rules are runtime profiles, not CIM weights.

- Verified confidential, restricted or sovereign-only traffic takes the direct GCT-Qwen path, bypassing CIM and all public inference.

- Eligible non-confidential traffic can use CIM then RLCD final routing across GCT-Qwen, Gemini Flash-Lite, DeepSeek-V3.2 and a pinned Claude flagship.

- An exact policy-valid cache hit can return before model inference; cache and budget enforcement remain within AI-PEL.

- POC uses three Hugging Face Docker Spaces, two separately managed GPU inference endpoints, and durable external data services.

- HF-hosted endpoints are not assumed to meet GCC data sovereignty: true sovereign claims require approved customer-controlled deployment and verified network/data boundaries.

# 2. Scope and non-functional objectives

Scope: ten synthetic telecom scenarios, bilingual Arabic/English, real API execution where configured, measurable prompt/context changes, response comparison, security controls, quota enforcement, editable DOCX/PPTX outputs. No real subscriber data.

| Objective       | Acceptance intent                                                                                    |
|-----------------|------------------------------------------------------------------------------------------------------|
| Security        | Default-deny, trusted app identity, source CIDR validation, no unauthorized external data transfer   |
| Quality         | Held-out task success and fact-preservation thresholds per scenario; never claim unmeasured accuracy |
| Cost            | Actual provider usage, pinned prices, CIM and local GPU costs separately, retries included           |
| Performance     | p50/p95 latency and cold-start status, time-to-first-token, context overhead                         |
| Reproducibility | Immutable run IDs, model revisions, policy version, prompt hashes, dataset revisions                 |
| Resilience      | Timeouts, circuit breakers, local-only fail closed, idempotent quota reservations                    |

# 3. Logical components

| Component                 | Implementation / responsibility                                                                                    |
|---------------------------|--------------------------------------------------------------------------------------------------------------------|
| Ingress/API               | FastAPI, authenticated app tokens/OIDC, trusted reverse proxy; rate-limit and request normalization                |
| AI-PEL                    | OPA policy decisions + gateway enforcement middleware; Presidio DLP, injection/response controls, quotas, tool ACL |
| RLCD                      | ModernBERT multi-head classifier: task, sensitivity, complexity, success-by-plan, routing confidence               |
| CIM                       | Generalized fine-tuned Qwen3-4B; JSON evidence/context package, references, omissions, missing facts               |
| Context Sufficiency Guard | Deterministic schema, source-reference, critical-fact and chronology checks; restore context on failure            |
| Retrieval/optimization    | BM25 + vector search, reranker, passage expansion, deterministic pruning, optional LLMLingua-2                     |
| Cache                     | Redis exact and policy-scoped semantic cache; optional provider prefix caching and local vLLM KV reuse             |
| Inference adapter         | LiteLLM/provider clients, pinned model revisions, streaming, retries, timeout and usage normalization              |
| Assurance                 | Groundedness, task checks, output DLP, fallback policy, document artifact validation                               |
| FinOps                    | Atomic preflight budget reservations, ledger reconciliation, caps, alerts and circuit breakers                     |
| Telemetry                 | OpenTelemetry traces, Postgres ledger/events, object storage for approved redacted artifacts                       |

# 4. Deployment topology

| Runtime                        | Hosting                               | Services / sizing guidance                                                                  |
|--------------------------------|---------------------------------------|---------------------------------------------------------------------------------------------|
| Space 1: gct-ai-control-center | HF Docker CPU                         | React frontend: Test Console, Admin Dashboard, policy manager, context inspector, budgets   |
| Space 2: gct-rlcd-gateway      | HF Docker CPU                         | FastAPI orchestration, ModernBERT, AI-PEL PEP, LiteLLM, cache/retrieval adapters, telemetry |
| Space 3: gct-context-lab       | HF Docker CPU                         | Dataset/evaluation UI, benchmarking, training-job coordination; not on live request path    |
| Endpoint A: enterprise-cim     | HF dedicated GPU endpoint             | Fine-tuned Qwen3-4B-class CIM; 24GB-class GPU starting hypothesis, validate workload        |
| Endpoint B: gct-qwen-finetuned | HF on-demand GPU endpoint             | GCT Qwen3-32B LoRA / vLLM; hardware/quantization validated empirically                      |
| External services              | Managed Postgres, Redis, object store | Persistent policy revisions, budget ledger, embeddings, experiments, artifacts              |
| External APIs                  | Google, Anthropic, pinned HF provider | Only when policy eligible; API keys exclusively backend                                     |

Deployment note: Space local filesystem is ephemeral. Separate deployment manifests should support customer VPC/on-prem production where AI-PEL, RLCD, cache, CIM as permitted, and GCT-Qwen are within the approved trust boundary.

# 5. Trust boundaries and network

- Frontend cannot call model providers or GPU endpoints directly; only backend has credentials.

- Backend must use service-to-service auth, TLS, outbound egress allowlists and scoped secrets.

- Source IP derives from trusted ingress/proxy configuration; never trust arbitrary client-supplied X-Forwarded-For.

- Confidential payloads, logs and artifacts must remain in approved region/environment; in HF demonstration use synthetic data and label sovereign path as simulated.

- Per-tenant isolation: namespace caches, budgets, embeddings, documents, audit, and access tokens.

- CIM is not allowed to see confidential/restricted/sovereign-only request content under the locked direct-path policy.

# 6. Prompt and decision flow

01. Ingress: authenticate app/user; capture trusted source; normalize messages/attachments

02. AI-PEL: evaluate identity, CIDR, task, tags, DLP, classification, model/tool ACL

03. AI-PEL: deny / approval / redact / enforce sovereign-direct and budget caps

04. Exact cache: validate tenant, policy, source, scope, version and TTL

05. If safe hit: return cached approved answer; audit avoided inference

06. RLCD initial: predict task, context needs, quality and execution-path eligibility

07. If confidential/restricted/sovereign-only: direct GCT-Qwen (CIM bypass)

08. Else if validated local-fast eligibility: direct GCT-Qwen (CIM bypass)

09. Else: retrieve approved context -\> CIM -\> sufficiency guard -\> RLCD final plan

10. Reserve worst-case spend / GPU quota atomically before billable execution

11. Preflight provider, final payload, token budget, policy and tool permissions

12. Execute -\> output checks -\> optional eligible retry/fallback -\> response

13. Reconcile tokens/costs; store trace; write cache only when eligible

Implementation nuance: budget reservations occur before any billable CIM, retrieval service, or inference operation; step 10 above also applies to CIM calls earlier in the path. The gateway should reserve a workflow envelope up front and allocate sub-reservations before each chargeable stage. No route may invoke an endpoint prior to applicable authorization and reservation.

# 7. AI-PEL policy specification

| Policy domain    | Inputs                                                  | Enforcement                                                           |
|------------------|---------------------------------------------------------|-----------------------------------------------------------------------|
| Identity/source  | app_id, role, trusted IP/CIDR, environment              | allow/deny, allowed tasks, deployment zone                            |
| Data sovereignty | declared classification + DLP-derived classification    | most restrictive wins; direct sovereign model; external egress denied |
| Routing          | verified tags, model allowlist, quality tier            | mandatory vs preferred constraints; RLCD within eligible set          |
| Tools/agents     | app role, tool/action, approval state                   | read-only / approval / deny before every tool call                    |
| Prompt/response  | prompt injection, secrets, PII, exfiltration indicators | block, redact, local-only, approval; scan retrieved text and output   |
| FinOps           | tenant, app, user, model, request, GPU pool             | RPM/TPM, token caps, daily/monthly budgets, reservations              |
| Governance       | policy version, exception, audit level                  | versioned publication, rollback, immutable decision record            |

```yaml
policy_id: GCT-CONF-001
match: {classification_any: [confidential, restricted, sovereign-only]}
enforce:
  execution_path: sovereign-direct
  allowed_models: [gct-qwen-finetuned]
  cim: bypass
  external_providers: deny
  output_dlp: required
  audit_level: enhanced
priority: 1000
```

Rule precedence: deny overrides allow; sovereign-only overrides model preferences; mandatory constraints intersect; conflicts result in deny, not implicit relaxation. Inline user-supplied tags are hints only, never authorization. Application metadata must be signed/authenticated. CIDR matching requires a trusted ingress-observed address.

# 8. API contracts

```http
POST /v1/ai/chat
Authorization: Bearer <app-credential>
Idempotency-Key: <uuid>
{
"messages": [{"role":"user","content":"Investigate synthetic SIM-swap alert"}],
"routing": {"mode":"hybrid", "tags":["fraud-investigation"]},
"data_classification":"confidential",
"quality_tier":"high", "max_output_tokens":1200,
"scenario_id":"fraud-management", "trace":true
}
```

Response:

```json
{"run_id":"...", "status":"completed|blocked|approval_required",
"selected_model":"gct-qwen-finetuned", "execution_path":"sovereign-direct",
"cim_invoked":false, "response":{...}, "usage":{...}, "policy":{...}}
```

Other endpoints: POST /v1/policy/evaluate; GET/POST /v1/admin/policies; POST /v1/admin/policies/test; POST /v1/admin/policies/publish; GET /v1/admin/budgets; POST /v1/admin/budgets; POST /v1/evaluations/run; GET /v1/runs/{id}; GET /v1/runs/{id}/trace; GET /health/live and /health/ready. Administrative APIs require privileged role and audit.

# 9. Generalized CIM: design and interface

Base candidate: Qwen3-4B-class instruct checkpoint; generalized fine-tuning on cross-domain context-management tasks. Exact checkpoint, license, context length and revision must be pinned during build. The GCT context profile is configuration, not model-specific training.

CIM request:

```json
{ "task":"...", "messages":[...], "evidence":[{"id":"doc-1#p4","text":"..."}],
"context_profile":"fraud-investigation", "target_budget_tokens":4000,
"required_fields":["event_timestamp","event_type","source_id"] }
```

CIM output:

```json
{ "intent":"...", "required_facts":[{"field":"event_timestamp","value":"...","source_id":"..."}],
"selected_evidence_ids":["..."], "omitted_evidence_ids":["..."],
"missing_fields":[], "upstream_messages":[...], "self_reported_complete":true }
```

CIM must not invent facts, collapse distinct timestamps, modify numerical values, or claim self-reported completeness as verified. Context Guard independently checks source spans, mandatory fields, chronology, citations, numerical fidelity and token budget. On failure: restore missing source passages; retry CIM only if budget/policy permits; otherwise use full eligible context or escalate.

# 10. RLCD decision model

Initial ModernBERT heads: task class, sensitivity, context sufficiency, complexity, estimated downstream quality by model/context strategy, and expected latency. Calibrate on held-out datasets; log version and confidence. RLCD action = (path, cache strategy, retrieval strategy, context strategy, model, validation level).

Decision objective: minimize total expected execution cost and latency subject to calibrated quality SLA, context-preservation rules, policy eligibility, token budgets and quota availability. Use measured outcomes to fit supervised decision models before attempting contextual bandits. Hard constraints must not be learned or overridable.

# 11. Caching and context economics

| Mechanism               | Guardrails / measurement                                                                                            |
|-------------------------|---------------------------------------------------------------------------------------------------------------------|
| Exact response cache    | Canonical request hash + tenant/app/role/classification/policy/KB version/TTL; return before CIM                    |
| Semantic response cache | Similarity threshold plus factual equivalence, no cross-user account/fraud reuse; policy validation                 |
| Retrieval/context cache | Versioned source references; enforce document ACL and freshness                                                     |
| Provider prompt caching | Provider-specific prefixes, billed cached-token tier; avoid double counting                                         |
| Local KV/prefix cache   | vLLM/LMCache where supported; latency/GPU throughput benefit, not API cost at \$0 rate                              |
| Context pruning         | Show removed passages and reasons; never remove mandatory facts                                                     |
| Cost waterfall          | Cache avoided calls -\> removed tokens -\> model-rate difference -\> provider cache tier; include CIM, GPU, retries |

# 12. Quota engine and bill-shock controls

Hierarchical quotas: enterprise, tenant, department, application, API key/user, model, request, agent workflow, and GPU pool. Redis atomic reservation (Lua/transaction), durable Postgres ledger and periodic reconciliation. Set alert thresholds (e.g. 70/85/95%) and hard cap. Reserve maximum permitted cost for output, retries, tool calls and CIM. Release unused reservation; use idempotency keys to avoid double charges. Streaming and multi-step agents must respect remaining envelope.

```text
Budget lifecycle: POLICY_CHECK -> RESERVE -> EXECUTE -> METER -> RECONCILE
On insufficient balance: BLOCK | APPROVAL_REQUIRED | eligible lower-cost route
Never downgrade confidentiality or route outside allowlist to save money.
GPU local API token price = $0 only as a POC billing convention;
report GPU-hours, utilization and estimated infrastructure cost separately.
```

# 13. Data model and observability

| Table / object    | Key fields                                                                          |
|-------------------|-------------------------------------------------------------------------------------|
| runs              | run_id, tenant_id, app_id, scenario_id, status, timestamps, routing_mode            |
| policy_decisions  | run_id, policy_version, matched_rule_ids, classification, allowed_models, decision  |
| context_packages  | run_id, cim_revision, evidence_refs, retained/removed IDs, validation, token_counts |
| inference_calls   | run_id, provider, model_revision, retries, input/output/cached tokens, latency      |
| budget_ledger     | reservation_id, scope, estimate, actual, released, currency, pricing_version        |
| cache_events      | run_id, cache_type, scope, hit, source_run, invalidation_reason                     |
| assurance_results | run_id, quality checks, evidence coverage, output DLP, escalation                   |
| artifacts         | run_id, object_ref, document_type, schema/figure validation, access_policy          |

OpenTelemetry spans: ingress -\> policy -\> cache -\> RLCD1 -\> retrieval -\> CIM -\> context_guard -\> RLCD2 -\> quota -\> inference -\> assurance -\> reconciliation. Every span records durations and non-sensitive metadata; prompts and PII should not appear in default traces. Detailed prompt inspector stores synthetic or access-controlled redacted payloads only.

# 14. AiVa POC user experience (see Section 22 for branding and UI standards)

| Test Console view    | Required information                                                                                               |
|----------------------|--------------------------------------------------------------------------------------------------------------------|
| Scenario launcher    | Ten scenarios, editable synthetic prompt, application profile, trusted-IP simulation, classification, routing tags |
| Prompt inspector     | Original, constructed, final submitted payload, removed/retained passages, no fictitious submission on cache hit   |
| Policy decision      | Identity, CIDR rule, matched policies, allowed pool, denied candidates, version                                    |
| Execution graph      | Cache return vs sovereign direct vs CIM-assisted; RLCD decision reasons                                            |
| Context intelligence | CIM package, required-fact coverage, omissions, sufficiency guard and recovery                                     |
| Response comparison  | Actual upstream output vs fixed frontier baseline where policy allows; quality checks                              |
| Savings              | Actual token accounting, provider cost, GPU estimates, retries, latency and quality-adjusted savings               |
| Artifacts            | Editable DOCX/PPTX generation and preview, source/figure preservation checks                                       |

Admin Dashboard: Policy Management (test/publish/rollback), Budget & Quotas, Model Registry, Context Profiles, Routing Rules, Cache Metrics, Security Events, Model Quality, FinOps, Audit/Trace Explorer. UI must clearly separate synthetic illustration from live measurements.

# 15. Ten locked synthetic GCT scenarios

| \#  | Scenario                        | Primary proof                                                                |
|-----|---------------------------------|------------------------------------------------------------------------------|
| 1   | Customer Support Chatbot        | Long history, context pruning, tariff RAG, cache and savings                 |
| 2   | Coding Assistant                | Telecom automation generation, tests and policy-constrained models           |
| 3   | Telecom Fraud Management        | Evidence chronology, confidential direct path, no punitive autonomous action |
| 4   | Enterprise RAG                  | Approved documents, citations, retrieval/context sufficiency                 |
| 5   | Network Operations Intelligence | Alarm correlation, high complexity, source integrity                         |
| 6   | Agentic Workflow                | Multi-step service incident, per-tool policy and cumulative budgets          |
| 7   | Arabic–English Customer Support | Bilingual preservation, quality and routing                                  |
| 8   | Repeated Queries & Caching      | Exact/semantic hit safety, policy versions, avoided inference                |
| 9   | Security & Data Exfiltration    | Prompt injection, DLP, external-model denial, audit                          |
| 10  | Confidential DOCX/PPTX Copilot  | Sovereign direct route, editable outputs, critical figures preserved         |

# 16. Training, evaluation and benchmark design

GCT-Qwen: synthetic GCT domain dataset target 12,000 train / 1,500 validation / 2,000 held-out, with task stratification. Generalized CIM: proposed cross-domain ~13,000 train examples, separate validation/test; benchmark GCT plus unseen industry cases. RLCD: train on actual outcomes for model × context strategy; separate training/validation/test splits and prevent document/template leakage.

| Benchmark             | Comparisons / gates                                                                                   |
|-----------------------|-------------------------------------------------------------------------------------------------------|
| Context effectiveness | Full context vs deterministic retrieval/pruning vs generalized CIM; required facts and answer quality |
| Routing effectiveness | Base Qwen vs GCT-tuned Qwen vs RLCD plan; same prompt/decoding where feasible                         |
| Confidentiality       | Confidential + Claude-preferred tag must force direct Qwen, zero CIM/external calls                   |
| FinOps                | Concurrency and retries cannot exceed hard spend caps; exact ledger reconciliation                    |
| Cache safety          | Tenant/version/identity separation; account-specific requests cannot cross-user hit                   |
| Cold start            | Report endpoint readiness, queue time and total response latency                                      |
| Document outputs      | Open generated DOCX/PPTX, check structure, figures, citations and editability                         |

Model quality comparison caveat: base Qwen on HF provider vs tuned Qwen on separate GPU does not isolate latency or infrastructure cost. API provider prices must be pinned; no fabricated measurements.

# 17. Failure handling and security tests

- OPA unavailable -\> fail closed for new privileged requests; cached response only if explicitly policy-valid and offline decision still verifiable.

- CIM timeout/invalid JSON -\> bounded retry then eligible full-context fallback; no cross-boundary route.

- Sovereign endpoint unavailable -\> queue, return retryable error or escalate; never silently use public provider.

- Quota reservation store unavailable -\> fail closed for billable inference.

- Provider rate-limit/timeout -\> bounded retries counted against reserved workflow budget; circuit breaker.

- Retrieved prompt injection -\> treat source text as untrusted evidence, never instructions; enforce tool ACL.

- Context guard failure -\> restore facts; do not pass unchecked package.

- Cross-tenant cache or artifact access -\> deny and audit.

# 18. Delivery plan and acceptance gates

| Phase                | Deliverables                                                                 | Exit gate                                                    |
|----------------------|------------------------------------------------------------------------------|--------------------------------------------------------------|
| P0 Foundation        | 3 Spaces scaffolds, API auth, OPA, Redis/Postgres, telemetry, synthetic data | Policy tests and trace IDs working                           |
| P1 Model endpoints   | CIM LoRA + Qwen LoRA, inference adapters, health/cold-start UX               | Real structured CIM and local Qwen calls                     |
| P2 Intelligence      | RLCD classifier, retrieval, context guard, caching, model routing            | Measured benchmark traces, no context loss in critical cases |
| P3 Governance/FinOps | Policy editor, CIDR/app rules, budget reservation/ledger, injection/DLP      | Fail-closed and bill-shock tests pass                        |
| P4 Experience        | Ten scenarios, prompt inspector, savings, admin analytics, DOCX/PPTX         | Reproducible investor/customer demo                          |
| P5 Hardening         | Threat model, load tests, revisions, backup, deployment portability          | Security and cost acceptance report                          |

# 19. Configuration and operational runbooks

Pin and version: all model IDs/revisions, tokenizer, provider names, inference regions, API prices, policies, context profiles, retriever embeddings, cache TTL, benchmark datasets and validation thresholds. Keep environment secrets in managed secret stores. Use migrations for Postgres; health/readiness probes; structured error codes; scheduled GPU prewarming; retention and deletion policies; reproducible deployment manifests.

# 20. Open decisions before implementation

- Confirm actual GPU SKU, quantization, max context and throughput for CIM and 32B Qwen.

- Confirm external model IDs, HF provider availability, region, API pricing and contractual data terms.

- Define quantitative per-scenario quality SLA and critical-fact preservation gates.

- Define source-IP trust model behind HF proxy; simulated IP profiles in demo vs enforceable real ingress.

- Select budget currency and monthly/department/app quota defaults.

- Determine artifact rendering/conversion isolation and malicious-file scanning requirements.

- Define production sovereign deployment target and independent residency/security verification.

# Appendix A. Open-source candidate stack

FastAPI • ModernBERT • Qwen3 • PEFT/TRL • vLLM • LiteLLM • OPA • Presidio • NeMo Guardrails • Haystack • LLMLingua-2 • Redis • Postgres/pgvector • OpenTelemetry • React/Tailwind. Each requires pinned license, revision, security review and benchmark validation.

# Appendix B. Architectural invariants

- Policy is evaluated before optimization and again at final egress/tool boundaries.

- No confidential/restricted/sovereign-only payload is submitted to CIM or external providers.

- CIM is a mandatory deployed component for the eligible context-intelligence path, but not a mandatory per-request invocation.

- RLCD cannot weaken AI-PEL constraints or override budget reservations.

- No semantic response cache reuse across customers for account-specific/fraud data.

- Every displayed savings claim must be traceable to actual measured tokens/prices, with infrastructure costs clearly separated.

# 21. AI Consumption Intelligence & Reporting

LLD v1.1 extension \| Mandatory POC capability \| 14 October 2026

## 21.1 Purpose and scope

The AI Consumption Intelligence & Reporting subsystem attributes every gateway execution to an authenticated tenant, application, user or service principal, department, business intent, prompt classification, model, execution path and cost center. It provides enterprise-to-trace drilldown, cost showback/chargeback, optimization analysis, policy effectiveness, quota visibility and privacy-aware exports. It must distinguish measured usage from estimates and synthetic demo data.

## 21.2 Reporting architecture and data flow

- Ingress identity and trusted source metadata → immutable request/trace ID → AI-PEL decision and RLCD classification → cache/CIM/upstream execution events → provider usage reconciliation → normalized usage ledger → materialized aggregates → authenticated Admin Dashboard and CSV export.

- Space 2 (gct-rlcd-gateway): OpenTelemetry spans and structured event emission, classification enrichment, accounting, ingestion API, policy-linked event capture and report queries.

- Postgres: append-only logical usage events, attempt-level usage records, classification dimensions, rate cards, policy versions, budgets and daily/hourly aggregates. Redis: ephemeral live counters and budget reservations, not the system of record.

- Space 1 (gct-ai-control-center): dashboards, role-aware filters, drilldowns, CSV export and links to trace detail. No additional GPU is required. Space 3 (gct-context-lab) may read sanitized benchmark aggregates for CIM evaluation.

- Background aggregation/reconciliation worker: can run as a controlled service in the gateway deployment for POC; production should separate ingestion and reporting workloads for scale.

## 21.3 Classification taxonomy

| Dimension         | Example values                                                 | Source / governance                           |
|-------------------|----------------------------------------------------------------|-----------------------------------------------|
| Business function | Support; Fraud; Network; Finance; IT; HR                       | Authenticated app mapping; versioned taxonomy |
| Task category     | Q&A; summarization; coding; RAG; analysis; generation; agentic | RLCD prediction plus app-provided hints       |
| Business intent   | Billing dispute; roaming inquiry; SIM swap; incident triage    | Application-specific sub-taxonomy             |
| Complexity        | Simple; moderate; complex; advanced                            | Calibrated RLCD classification                |
| Sensitivity       | Public; internal; confidential; restricted                     | Most restrictive AI-PEL/DLP result            |
| Language          | Arabic; English; bilingual; other                              | Classifier / deterministic detection          |
| Execution path    | Cache return; sovereign direct; CIM assisted                   | Actual gateway decision                       |
| Context strategy  | Full; prune; retrieve; compress; evidence-preserving           | Actual context plan and CIM status            |
| Outcome           | Success; quality fail; policy denied; quota denied; timeout    | Observed execution and assurance events       |

Record taxonomy_version, classifier_model_version, predicted_label, calibrated_confidence, and optional reviewed_label. Use stable category IDs; preserve historical classification at event time. Low-confidence labels should be shown as unclassified/review-needed rather than asserted as fact.

## 21.4 Token and cost accounting rules

- Record original_context_tokens, constructed_context_tokens, upstream_submitted_input_tokens, upstream_output_tokens, CIM_input_tokens, CIM_output_tokens, cached_input_tokens, reasoning_tokens (if reported), retries/fallback tokens and total actual provider-billable usage per attempt.

- Full-response cache hit: no upstream inference attempt; record avoided_call=true and modeled avoided tokens/cost separately from observed usage. Provider prompt-cache discounts are distinct from avoided calls.

- Attribute CIM processing overhead, retrieval/reranking overhead, upstream model costs, retry/fallback costs and local GPU estimated cost separately. The POC local-Qwen API-token price may be \$0 while GPU cost is nonzero.

- Use provider-returned usage as authoritative where available, with tokenizer estimates labeled estimated. Preserve rate_card_id, provider_model_version, pricing effective date, currency and cache tiers.

- Do not double count savings: compare the same task against a pinned frontier baseline; separate cache avoidance, context reduction, model-rate differential and provider prompt-cache benefit. Quality-adjusted savings require both outputs to meet the agreed quality threshold.

- For denied requests, record policy/quota decision and any local processing overhead, but no fictional upstream token consumption.

## 21.5 Logical storage schema

| Entity                 | Essential fields                                                                                                                                       |
|------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------|
| execution_run          | trace_id PK; tenant_id; application_id; principal_pseudonym; department_id; cost_center_id; ingress_source_policy; created_at; outcome; execution_path |
| classification_event   | trace_id; taxonomy_version; classifier_version; function; category; intent; complexity; sensitivity; language; confidence                              |
| model_attempt          | attempt_id; trace_id; stage (CIM/upstream/validation); model; provider; input/output/cached/reasoning tokens; latency_ms; status; retry_of             |
| usage_ledger           | ledger_id; attempt_id; rate_card_id; charge_type; measured_or_estimated; amount_usd; reservation_id; reconciled_at                                     |
| optimization_event     | trace_id; cache_type; cache_hit; context_before/after; removed_passages_count; preservation_result; avoided_call                                       |
| policy_event           | trace_id; policy_version; matched_rule_ids; action; deny_reason; budget_scope; reservation_id                                                          |
| report_aggregate_daily | date; tenant; application; department; task category; model; path; request_count; token totals; spend; savings; cache_hits                             |

Use trace_id for cross-system correlation and immutable attempt_id for individual provider calls. Store raw prompts only in separately authorized, retention-controlled trace storage when explicitly enabled; do not include prompt text in reporting tables.

## 21.6 Reporting API contracts

- GET /v1/reports/overview?from=&to=&tenant_id= — KPI totals, spend, tokens, cache rate, quality pass rate, cost variance.

- GET /v1/reports/consumption?group_by=application\|department\|user\|category\|model\|provider\|path&filters= — paginated groupings.

- GET /v1/reports/classification?group_by=business_function\|task_category\|intent\|complexity\|sensitivity\|language — distribution and trend.

- GET /v1/reports/optimization?group_by=application\|strategy\|model — measured context reduction, cache effects, savings and overhead.

- GET /v1/reports/governance?group_by=policy\|application\|source_policy — denied calls, quota events, confidential routing and exception counts.

- GET /v1/reports/runs/{trace_id} — authorized request drilldown with attempt-level usage, classifications, policy decisions and validation outcomes.

- POST /v1/reports/exports — asynchronous CSV export, RBAC-enforced, time-limited download, audited.

## 21.7 Dashboard information architecture

- Executive overview: time window, enterprise total requests/tokens/API spend/GPU estimated spend, cost trends, cache hit rate, optimization savings and quality outcomes.

- Consumption explorer: Enterprise → department → application → pseudonymized user/service principal → prompt category → model → trace, with filters for date, environment, language, sensitivity, execution path and source policy.

- Classification intelligence: workload mix, top business intents, complexity shifts, Arabic/English mix, low-confidence/unclassified share and model suitability by task.

- Model and context efficiency: CIM invocation rates, local bypass rates, tokens before/after, cache hit types, provider prompt-cache tokens, per-model latency and retries.

- FinOps and governance: budget consumed/reserved/remaining, forecast and anomalies, hard-limit denials, source-CIDR policy matches, local-only enforcement and policy version changes.

- Export and showback: cost-center summaries, downloadable filtered tables, scheduled-report capability as future scope (not required for POC).

## 21.8 Security, privacy and access control

- Role-based and tenant-scoped authorization: platform administrator, tenant administrator, finance analyst, department manager, security auditor and application owner; restrict user-level views by organizational scope.

- Pseudonymize user IDs in default reports. Authorized identity resolution must be logged and separately permissioned. Avoid exposing raw source IP to broad audiences; report matched source policy/CIDR where sufficient.

- Do not place prompt text, subscriber identifiers, secrets, document contents or raw PII in aggregate reporting. Apply minimum retention, encryption at rest/in transit and export expiry.

- Classifications may be sensitive metadata. Enforce data minimization and retention for per-run traces, classification records and audit events independently.

- Confidential requests still generate safe consumption metadata; sovereign direct path bypasses CIM but not metering, AI-PEL, assurance or audit.

## 21.9 FinOps alerts and anomaly rules

Trigger configurable alerts for sudden per-application token spikes, expensive-model share changes, repeated retries, high output-token ratios, unusual user consumption, quota threshold crossings (e.g. 80/95/100%), and provider-billing discrepancies. POC alerts can be shown in the Admin Dashboard; email/webhook integration is configurable follow-on scope. Alerts never replace hard preflight reservations and enforced spend caps.

## 21.10 Acceptance tests for reporting

- Generate synthetic traffic across at least three applications, multiple pseudonymous users, two languages, several task categories, both sovereign-direct and CIM-assisted paths, and at least one full-response cache hit.

- Verify that grouped token totals reconcile exactly to the attempt-level ledger for the same time window; show separate actual, estimated and avoided usage.

- Drill down from application to user, category, model and trace; verify RBAC prevents cross-department or cross-tenant data exposure.

- Show a confidential request routed directly to GCT-Qwen with zero CIM and zero external provider attempts, while retaining consumption attribution.

- Trigger an application budget threshold and hard cap; verify budget reservation, denial, alert, reconciliation and reporting.

- Verify classifier version/taxonomy version are retained and ambiguous prompts can be marked unclassified.

- Export a filtered CSV and verify no prompt body, subscriber PII or unauthorized user identities appear.

- Display a pinned frontier comparison with non-double-counted savings and visible quality pass/fail.

## 21.11 Delivery integration

Add reporting events and ledger tables during the gateway foundation phase; build classification and aggregation queries alongside RLCD/CIM integration; deliver the dashboard, role-aware drilldowns and exports before final POC acceptance. Reporting is mandatory, not a future optional analytics add-on.

# 22. AiVa brand identity and UI design system

Status: normative POC design baseline. This section applies to the Control Center, POC Test Console, Admin Dashboard, reporting, exported reports, screenshots, and investor demonstrations. Branding is provisional until trademark and domain clearance.

## 22.1 Brand strategy and naming

| Element          | Specification                                                                                    |
|------------------|--------------------------------------------------------------------------------------------------|
| Master brand     | AiVa (capital A, lower-case i, capital V, lower-case a); use AiVa in prose and interface labels. |
| Meaning          | AI Value Assurance.                                                                              |
| Category         | Enterprise AI Intelligence & Control Platform.                                                   |
| Tagline          | Govern every request. Optimize every token. Prove the value.                                     |
| Three pillars    | Govern · Optimize · Observe.                                                                     |
| Tone             | Precise, calm, credible, secure, engineering-led; avoid exaggerated AI claims.                   |
| Brand constraint | Do not assert name/domain/trademark availability. AiVa is a working brand until legal clearance. |

## 22.2 Product nomenclature

| Surface / capability        | User-facing name    | Technical designation                              |
|-----------------------------|---------------------|----------------------------------------------------|
| Overall UI                  | AiVa Control Center | gct-ai-control-center HF Space                     |
| Policy and quota management | AiVa Govern         | AI-PEL, OPA, DLP, quota guard                      |
| Routing and context         | AiVa Optimize       | RLCD, generalized CIM, cache and execution planner |
| Reporting and FinOps        | AiVa Insights       | AI Consumption Intelligence & Reporting            |
| Interactive POC tests       | AiVa Test Console   | POC Test Console                                   |
| Local sovereign model       | GCT Sovereign Qwen  | GCT-Qwen3-32B or resource-adjusted variant         |
| General context specialist  | Enterprise CIM      | Generalized fine-tuned Qwen3-4B                    |

Brand labels do not rename deployment resources automatically. Retain existing endpoint and Space identifiers unless a separate migration is approved; document user-facing aliases in configuration.

## 22.3 Visual identity and design tokens

| Token            | Suggested value | Usage                                              |
|------------------|-----------------|----------------------------------------------------|
| brand.navy       | \#101D34        | Primary brand background, key headings, navigation |
| brand.teal       | \#10A79D        | Accent, selected navigation, meaningful emphasis   |
| brand.tealLight  | \#75DCCC        | Dark-background highlights                         |
| surface.canvas   | \#F7F9FC        | Light-mode app canvas                              |
| surface.card     | \#FFFFFF        | Cards, tables, drawers                             |
| text.primary     | \#17253D        | Body and data labels                               |
| text.muted       | \#607086        | Secondary text                                     |
| border.subtle    | \#DDE5ED        | Panel boundaries and dividers                      |
| semantic.success | \#16805D        | Validated / policy-compliant                       |
| semantic.warning | \#B76B12        | Quota warning / needs attention                    |
| semantic.danger  | \#B83B46        | Denied / failed / exfiltration blocked             |
| semantic.info    | \#2764B3        | Informational route or diagnostic state            |

Use semantic colors only when meaning is explicit; pair every color-coded state with text and/or icon. Prefer neutral backgrounds and restrained accents. Do not color-code arbitrary model providers as security status.

## 22.4 Typography, layout, iconography and responsiveness

- Typography: use Inter or a system sans-serif for English UI; Noto Sans Arabic or an equivalently legible Arabic family for Arabic. Body 14–16 px, secondary 12–14 px, section heading 20–24 px, KPI value 26–34 px. Use tabular numerals for costs, token counts and latency.

- Spacing: adopt an 8 px base grid (4/8/16/24/32 px). Use 12–16 px card corners and consistent 1 px dividers; reserve shadows for overlays.

- Layout: desktop left navigation 240–264 px; content max width around 1440 px; responsive stacked cards and scrollable tables on smaller viewports. Support 1280 px laptop as the baseline demonstration resolution.

- Icons: consistent outline icon set; icons supplement, never replace, text. Logo wordmark is “AiVa” with optional teal dot; do not imply the logo has undergone legal clearance.

- Accessibility: target WCAG 2.2 AA, keyboard access, visible focus, descriptive labels, accessible charts/tables, contrast checks, and reduced-motion support.

- Localization: English and Arabic interfaces; use RTL layout for Arabic, preserve LTR rendering for IPs, model IDs, JSON, currency and trace IDs. Test mixed-script tables and copied text.

## 22.5 Information architecture and navigation

| Top-level area       | Views                                                                                                                                | Primary purpose                                                         |
|----------------------|--------------------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------|
| AiVa Test Console    | Scenario library; run workspace; Response Comparison; Prompt & Context Inspector; Routing Decision; Cache & Tokens; Savings Analysis | Demonstrate individual execution with genuine traces and measurements.  |
| AiVa Admin Dashboard | Overview; Govern; Optimize; Insights; Quality & Assurance; Audit; Settings                                                           | Administer policy, monitor operational health and explain AI economics. |
| Govern               | Applications & Identities; Trusted Networks; Policy Rules; Model Allowlist; Tool Permissions; Budgets & Quotas; Approval Queue       | Define and enforce enterprise constraints.                              |
| Optimize             | Routing Policies; CIM Profiles; Cache Policies; Model Registry; Benchmarks                                                           | Configure optimization without overriding hard policy.                  |
| Insights             | Consumption Overview; Applications; Users; Prompt Categories; Models; Savings; Quotas; Export Center                                 | Explore token use, business intent and value delivered.                 |

## 22.6 Core UI interaction requirements

- Global filters: time range, tenant, department, application, user or pseudonymous user, environment, model, classification, task category, source policy and execution path. Show active filters and offer reset.

- Report drilldown: Enterprise → Department → Application → User → Prompt Category → Model → Execution Trace, enforcing permissions at every level; never rely solely on front-end filtering.

- Policy editor: create/draft/validate/simulate/review/publish/rollback; show effective policy, rule priority, conflicting rules, verified identity, trusted ingress IP and CIDR match. Require explicit approval for high-impact changes.

- Test Console: always show original prompt and context (only to authorized testers), classification, firewall/AI-PEL decisions, budget reservation, RLCD decision, CIM invoked/bypassed, context sufficiency, selected model, submitted payload, actual response, tokens, latency, cost, quality and trace ID.

- CIM bypass visualization: confidential/restricted/sovereign-only routes must explicitly say “CIM bypassed by mandatory sovereignty policy”; eligible local fast paths may say “CIM skipped: context sufficient.”

- Cache visualization: on full response-cache hit, show “No upstream inference call”; do not display fabricated submitted-model tokens. Distinguish provider prompt-cache discounts from full-response cache avoidance.

- Budget feedback: show cap, spent, reserved, forecast, alert threshold and remaining amount. Denials must include rule ID and actionable remediation without leaking sensitive policy details.

- Report exports: CSV initially; PDF optional. Exported metadata must include selected filters, timezone, period, generation timestamp, classification version, pricing snapshot and a synthetic/actual-data designation.

## 22.7 Dashboard visualizations and reporting rules

| Visualization            | Recommended encoding                                 | Guardrail                                                                        |
|--------------------------|------------------------------------------------------|----------------------------------------------------------------------------------|
| Token consumption trend  | Stacked area or lines by application/model           | Separate input, output, CIM and retries; avoid double counting.                  |
| Application/user ranking | Horizontal bars and sortable table                   | Respect row-level access and user pseudonymization.                              |
| Prompt taxonomy          | Stacked bars by business function and task category  | Expose unknown/low-confidence bucket and taxonomy version.                       |
| Model mix                | Stacked bars or matrix by provider and routing path  | Differentiate sovereign simulation from genuine deployment.                      |
| Savings waterfall        | Baseline → cache → context → routing → retries → net | Show measured vs estimated values and baseline assumptions.                      |
| Budget utilization       | Progress bars and threshold markers                  | Warn at configured levels; never imply estimated costs are invoiced totals.      |
| Policy decisions         | Time-series and denial-reason breakdown              | Avoid revealing raw prompts or sensitive rule internals to unauthorized viewers. |
| Quality vs cost          | Scatter plot with quality and latency filters        | Show confidence, sample size and comparable workload cohort.                     |

## 22.8 Status, states, and copy conventions

| State            | Preferred UI copy                                                        |
|------------------|--------------------------------------------------------------------------|
| Approved         | Allowed by policy                                                        |
| Blocked          | Blocked by AI policy                                                     |
| Restricted route | Sovereign direct route required                                          |
| Approval         | Approval required                                                        |
| CIM active       | Context package generated; sufficiency verified                          |
| CIM failed       | Context sufficiency check failed; safe fallback applied                  |
| Cost metric      | Measured provider API cost / Estimated local GPU cost                    |
| Savings metric   | Estimated savings versus pinned baseline (methodology available)         |
| POC demo         | Synthetic GCT data · Illustrative UI / Measured execution, as applicable |

Use explicit empty/loading/error states; never silently substitute fake metrics or successful traces. Provide an explanation tooltip for all derived KPI calculations.

## 22.9 Frontend implementation guidance

- Implement a shared React component library with design tokens, accessible form controls, tables, filters, cards, dialogs, toasts and charts; keep brand colors in CSS variables to enable controlled rebranding.

- Expose typed REST/OpenAPI contracts from the FastAPI gateway. Use server-authorized queries, pagination and cursor-based trace browsing; never expose provider secrets, raw credentials or unrestricted policy payloads to the browser.

- Adopt a common event envelope with trace_id, tenant_id, application_id, user pseudonym, task category, classification confidence/version, policy decision/version, path, model, token usage, cost fields and timestamp.

- Provide a “Data provenance” affordance on every KPI: measurement source, calculation, time window, pricing version, cache accounting, and limitations. Do not label a simulation as a production sovereignty guarantee.

- Use consistent components for badges, tables and charts, but avoid decorative status colors; ensure visual tests cover long Arabic labels, narrow screens, zero usage, large numbers and high-density reports.

## 22.10 Brand/UI acceptance criteria

- AiVa brand name, expanded meaning, tagline and three pillars appear consistently on the login/landing page, primary navigation, reports and demo materials.

- Every scenario trace shows its AI-PEL policy decision, RLCD route, CIM state, cache status, token/cost accounting and measured-versus-estimated labels.

- An authorized administrator can filter consumption by application, user, prompt category, model, classification and date; navigate to a trace and export filtered data.

- An unauthorized user cannot view another department’s user-level records, confidential prompts or protected policy internals, including via direct API requests.

- Policy simulator demonstrates app identity plus trusted source CIDR and confidentiality constraints; confidential requests always bypass CIM without bypassing policy checks.

- Budget demo exercises 80%/95% alerts, atomic reservation, hard-cap denial, cache-hit treatment and actual-usage reconciliation.

- English and Arabic flows meet keyboard/RTL/contrast checks; UI labels are clear and chart equivalents are accessible.

- No screenshot, dashboard, or export presents synthetic, estimated, or illustrative values as verified production measurements.

## 22.11 HLD/LLD alignment and rollout

Apply AiVa as the working product brand across future HLD diagrams, test plans, UX prototypes and investor artifacts. Retain GCT as the fictional reference tenant and telecom-specific sovereign model. No new Space or GPU endpoint is introduced by branding or UI guidance. Update the design-system implementation before finalizing POC screenshots and acceptance evidence.
