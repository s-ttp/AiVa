# ADR-0004: Gateway pipeline, stage interfaces and spans

**Context.** Several workstreams build different parts of the LLD §6 flow in parallel.

**Decision.**
- The orchestrator (`gateway/src/aiva_gateway/pipeline/orchestrator.py`) encodes the §6 order explicitly.
  Each stage is a `Protocol` in `pipeline/interfaces.py`; implementations live in the owning package
  (`govern/`, `finops/`, `optimize/`, `inference/`, `assurance/`) and are wired in `services.py`.
- P0 ships placeholder implementations. Anything that fabricates output (simulated inference, simulated
  CIM, unlimited quota) is labelled `simulated` in the run record and the response.
- Every run emits the same 13 spans in order: `ingress, policy, cache, rlcd1, retrieval, cim,
  context_guard, rlcd2, quota, preflight, inference, assurance, reconciliation`. Skipped stages emit a span
  with `aiva.stage.status=skipped` and a reason (e.g. "CIM bypassed by mandatory sovereignty policy").
- Span attributes are allowlisted; prompt text, PII and secrets never enter spans.
- Policy is evaluated at request time and again at every egress (CIM call, upstream call, tool call).
  Quota reservation happens before any billable stage.

**Consequences.** Workstreams change implementations without touching the orchestrator; changes to the
orchestrator or interfaces go through small dedicated PRs.
