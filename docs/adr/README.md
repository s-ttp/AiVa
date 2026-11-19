# Architecture Decision Records

Short records of decisions that refine or extend the LLD. Format: context, decision, consequences.
Add a new numbered file for each decision; never rewrite an accepted ADR — supersede it.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-english-first-phase.md) | English-only first phase | Accepted |
| [0002](0002-model-pins-and-generator-separation.md) | Model pins and synthetic-data generator separation | Accepted |
| [0003](0003-unified-data-model.md) | Unified run data model for LLD §13 and §21.5 | Accepted |
| [0004](0004-gateway-pipeline.md) | Gateway pipeline, stage interfaces and spans | Accepted |
| [0005](0005-policy-engine.md) | Policy engine: OPA, restrict-only policies, two-phase evaluation | Accepted |
| [0006](0006-money-and-provenance.md) | Money as integer micro-USD and provenance labels | Accepted |
| [0007](0007-identity-and-ingress.md) | Identity, simulated test identities and trusted ingress | Accepted |
| [0008](0008-quota-engine-and-ledger.md) | Hierarchical quota engine, durable reservation journal and reconciliation | Accepted |
| [0009](0009-live-inference-and-optimization.md) | Live inference adapters, recorded fixtures, response caches and retrieval | Accepted |
| [0010](0010-control-center-sign-in.md) | Control Center sign-in and role-aware navigation | Accepted |
| [0011](0011-presidio-dlp-and-injection-detection.md) | Presidio DLP and prompt-injection detection | Accepted |
| [0012](0012-policy-lifecycle-approvals-and-tool-acl.md) | Policy lifecycle, Approval Queue and tool ACL | Accepted |
| [0013](0013-synthetic-data-training-and-serving.md) | Synthetic data, training and serving for the AiVa models (P1) | Accepted |
| [0014](0014-approved-budget-exceptions.md) | Approved budget exceptions: bounded, single-use cap lifts | Accepted |
| [0015](0015-insights-aggregation-and-reporting.md) | AiVa Insights aggregation, reporting access and exports | Accepted |
| [0016](0016-agentic-workflow.md) | Agentic workflow: governed steps, per-tool policy, one workflow budget | Accepted |
| [0017](0017-document-copilot-and-sandboxed-rendering.md) | Confidential document copilot: sovereign drafting, gateway-built files, sandboxed rendering | Accepted |
| [0018](0018-cim-returns-selection-gateway-builds-package.md) | The CIM returns selection and facts; the gateway builds the package | Proposed |
| [0019](0019-public-demo-replay-and-capped-live-runs.md) | Public demo: replay by default, capped live runs | Accepted |
| [0020](0020-sovereign-direct-retrieval.md) | Knowledge-base retrieval on the sovereign-direct path | Accepted |
| [0021](0021-presenter-live-demo-wake-sleep-and-node-status.md) | Presenter live demo: wake, sleep and node status | Accepted |
| [0022](0022-governed-live-web-search-for-market-research.md) | Governed live web search for market research | Accepted |
| [0023](0023-live-demo-answer-quality-comparison.md) | Live demo answer-quality comparison, with the one sovereignty exception for synthetic scenario 11 | Accepted |
| [0024](0024-gateway-database-backup-to-a-storage-bucket.md) | Space 2's database survives restarts through a private Storage Bucket | Accepted |
| [0025](0025-visitors-reach-the-gateway-directly.md) | Visitors reach the gateway directly; no Space-to-Space hop | Accepted |
| [0026](0026-openai-compatible-api.md) | An OpenAI-compatible API on the gateway | Proposed |
| [0027](0027-admin-keys-and-model-switches.md) | Application keys and model switches from the admin console | Accepted |
