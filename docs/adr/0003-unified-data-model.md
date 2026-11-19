# ADR-0003: Unified run data model for LLD §13 and §21.5

**Context.** LLD §13 (data model) and §21.5 (reporting schema) describe overlapping tables with different
names: `runs`/`execution_run`, `inference_calls`/`model_attempt`, `policy_decisions`/`policy_event`,
`budget_ledger`/`usage_ledger`.

**Decision.** One canonical schema, using the §21.5 names where they overlap and keeping both concepts
where they differ:

| LLD §13 | LLD §21.5 | Canonical table |
|---|---|---|
| runs | execution_run | `execution_run` (PK `run_id`, unique `trace_id`) |
| policy_decisions | policy_event | `policy_event` (one row per evaluation stage) |
| inference_calls | model_attempt | `model_attempt` (stage = cim / upstream / validation / baseline) |
| budget_ledger | usage_ledger | `budget_reservation` (reserve/commit/release) + `usage_ledger` (append-only charges) |
| context_packages | optimization_event | `context_package` and `optimization_event` (both kept) |
| cache_events, assurance_results, artifacts | — | `cache_event`, `assurance_result`, `artifact` |
| — | classification_event, report_aggregate_daily | same names |

Additional tables: tenants, departments, cost centers, applications and credentials, principals,
trusted networks, policy versions, models, rate cards, budgets, taxonomy versions, trace spans,
audit log, idempotency records, approval requests.

`usage_ledger` and `audit_log` are append-only (database triggers reject UPDATE/DELETE); corrections are
new rows. No prompt text is stored in reporting tables.

**Consequences.** API responses expose `run_id` and `trace_id`; reporting joins on `run_id`.
