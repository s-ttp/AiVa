# ADR-0008: Hierarchical quota engine, durable reservation journal and reconciliation

**Context.** LLD §12 requires atomic, idempotent budget reservations across nine hierarchical scopes, a
durable Postgres ledger with periodic reconciliation, alerts and caps that concurrent requests and retries
can never exceed. LLD §21.2 makes Redis ephemeral live state and Postgres the system of record. On HF
Spaces Redis may restart empty mid-month, so the live counters must be rebuildable.

**Decision.**
- **Envelope.** Before the first billable stage the gateway reserves the worst case: every upstream
  attempt (initial + bounded retry) at the full output allowance, plus the CIM call and the opt-in
  baseline. Shared scopes (tenant, department, application, user, workflow) hold the whole envelope; each
  model the run may call gets a *line* for its model scope and, when served on a priced GPU endpoint, its
  GPU pool. Before RLCD's final plan (CIM path) the lines cover every eligible model; after it, the chosen
  one. Preflight refuses any CIM, upstream or baseline call the reservation does not cover. A `request`
  budget bounds one envelope; `workflow` budgets accumulate across runs sharing a `workflow_id`.
- **Buckets.** Spend is counted per scope and period (`application|gct-support-chatbot|monthly|2026-10`),
  daily and monthly for calendar scopes and lifetime for workflows, for every scope a run touches, budgeted
  or not, so a budget created mid-period starts from the true spend. `scope_id: default` budgets are
  templates applied to every scope of their type. GPU-pool buckets meter estimated GPU cost (busy time x
  hourly rate, labelled `estimated`), separate from API spend (LLD §12, §21.4).
- **Atomicity.** One Lua script checks every capped bucket and applies to all or none; amounts move only
  through `HINCRBY` on decimal strings, so counters stay exact 64-bit integers. Keys share a `{tenant}`
  hash tag. The script refuses a stale budget snapshot (content hash published in Redis), so cap changes
  are enforced by every process from the next reservation.
- **Idempotency.** The reservation record is keyed by the idempotency key: a repeated reserve returns the
  open reservation; a settled one cannot fund another run; a repeated settle is a no-op. API-level
  `Idempotency-Key` handling prevents duplicate runs.
- **Exhaustion.** The strictest action among exhausted budgets wins (`block` > `approval_required` >
  `lower_cost_route`). A lower-cost route only removes models from the policy-eligible set (the denied
  model or pool, or the costliest tier) and re-plans with RLCD, so it can never cross a confidentiality
  boundary or leave the allowlist. Zero-cost reservations are never denied by spend caps.
- **Durability.** Each reservation is journaled in `budget_reservation` (no foreign key to the run, which
  is written later) before it is returned; if that write fails the hold is undone and the request fails
  closed. Settlement records committed spend per bucket in the same transaction as the attempt-level
  ledger. A missing Redis bucket is rebuilt from the journal (settled spend + `budget_adjustment` + open
  holds) before any check runs against it. Redis or Postgres unavailable → `QUOTA_STORE_UNAVAILABLE`.
- **Reconciliation** (in-process every `AIVA_RECONCILE_INTERVAL_SECONDS`, `aiva-admin reconcile`, or
  `POST /v1/admin/budgets/reconcile`): settles stale open reservations from the ledger; releases orphans
  (no run record) with a `reservation_expired` alert to review provider billing; releases Redis-only
  holds; propagates ledger corrections (`adjustment` rows, never updates) into budgets as append-only
  `budget_adjustment` rows; resets counter drift only on quiescent buckets (nothing held, unchanged while
  the ledger was read).
- **Alerts.** Thresholds (default 80%/95%) fire on *committed* spend crossing them, once per bucket and
  period; `hard_cap` fires when spend reaches the cap or a reservation is denied. Overruns, expired holds,
  ledger corrections and counter corrections are also alerts. Alerts never replace enforcement.
- **Provenance.** A reservation enforced by the live store is labelled `measured` (the amount held is
  exact); its size is a worst-case bound. Budget `spent` carries the least trustworthy label among the
  charges it counts; forecasts are always `estimated`.

**Consequences.** Caps hold exactly while actual spend stays within the reservation. With live providers
the input token count must come from the provider tokenizer before preflight (P1): an overrun is real spend,
so it is committed and raises `reservation_overrun`. An orphaned reservation is released rather than
charged, keeping budgets equal to the attempt-level ledger; its alert asks finance to check the provider
bill. Redis must run with `maxmemory-policy noeviction`.

**Amendment (2026-10-15): money domains.** ADR-0019 makes the Control Center public, with replayed runs by
default. Replayed and simulated runs spend no money, but until now they drew on the same counters as live
spend: public replay traffic could exhaust the tenant and application budgets and block live runs, the
owner's included. LLD §23 still needs the budget demo (80%/95% alerts, hard-cap denial, approvals) to work in
replay.
- **Domains.** Every bucket belongs to one money domain:
  - `live`: runs that can spend money (inference mode `live` or `record`, and per-run `execution_mode` once
    ADR-0019's per-run selection lands);
  - `illustrative`: replayed and simulated runs;
  - `visitor:<pseudonym>`: a public visitor's replayed and simulated runs, so one visitor cannot fill the
    illustrative caps for everyone else.
- **Enforcement.** The same budgets, caps, thresholds and exhaustion actions apply in every domain. A run
  reserves, is checked and commits only in its own domain, so a budget denial is still shown in replay.
  Fail-closed behaviour is unchanged. Live buckets commit only `measured` and `estimated` charges. Ledger rows
  keep their recorded amounts and provenance in every domain, so Insights is unchanged.
- **Keys.** Illustrative keys keep the original format; other domains prefix it (`live:tenant|gct|…`).
  Existing journal rows and counters, all from simulated runs, are therefore illustrative without a data
  migration.
- **Readouts.** Budget readouts report the domain of the gateway's configured mode first and the other
  domain alongside (`other_domain`). Illustrative figures are never labelled `measured` or `estimated`.
  The workflow readout reads the domain its steps run in.
- **Alerts.** Alerts carry their domain (`budget_alert.domain`). A visitor's own alerts are kept for that
  visitor and left out of the admin and Insights alert lists, so visitors' demos cannot flood them.
- **Growth.** Visitor buckets expire a day after their period, and workflow buckets a day after last use.
  Counter-drift reconciliation does not sweep them; a missing counter is rebuilt from the journal. Their
  journal rows are period-scoped and can be pruned by a later job.

Consequence: live caps count money only, and replay demos show budgets filling, labelled `replayed` or
`simulated`. Heavy illustrative traffic from signed-in users can still fill the shared illustrative caps until
the period resets; a budget owner can raise them.

**Reservation labels.** A reservation is labelled `measured` only in the live domain, where the store holds
an exact amount. Outside it the hold is illustrative and carries the run's label (`replayed` or `simulated`),
in the run response and in its journal row alike. Reconciliation therefore selects the reservations the quota
store holds (those journaled with allocations) rather than skipping every `simulated` label.
**Amendment (2026-10-15): public demo scope and visitor live window (ADR-0019).** A public visitor's live run
is capped twice, and both caps are checked before any billable stage:
- **The `public_demo` scope** (scope_id is the tenant; daily or monthly; $10/day in `config/tenant.yaml`)
  joins the chain of a visitor's live runs only. It counts their whole cost: the envelope plus the GPU
  worst case when reserved, and API plus estimated GPU cost when committed. A sovereign run, whose token
  price is $0, still counts. With no `public_demo` budget configured, public live runs are refused
  rather than left uncapped. Signed-in users' live runs never draw on the scope.
- **The visitor live window** allows at most `AIVA_PUBLIC_LIVE_RUNS_PER_WINDOW` (5) live runs per visitor
  pseudonym in a rolling `AIVA_PUBLIC_LIVE_WINDOW_SECONDS` (3600). It is a Redis sorted set, checked by one
  Lua script and idempotent per run. A slot is taken just before the reservation and given back when the
  run made no model call (refused by the budget or the endpoint, or stopped earlier). It is not journaled,
  because ADR-0019 accepts that clearing cookies resets it; the daily budget is the durable bound. An
  unreachable store fails closed.

Either limit refuses a visitor's live run with `LIVE_DEMO_UNAVAILABLE` (`details.reason`: `budget`,
`visitor_limit`, or Inference's `endpoint_cold`; `details.resets_at`). The run is never downgraded.
Visitors never raise budget approval requests, since they cannot use the queue. `GET /v1/demo/live-budget`
reports the public budget left today, rounded down to 10%, and the visitor's runs left this hour, with no
amounts.

**Amendment (2026-10-20): billed tool calls (web search).** The owner approved real web search through Tavily.
A tool call that a provider bills is reserved before it runs and metered after, like a model call (LLD §12,
Appendix B).
- **Price.** `config/rate_cards.yaml` gains `tool_rate_cards`, one card per tool with a price per billed unit.
  `web.search` is `rc-tavily-search`: $0.008 per API credit. A basic search is 1 credit and an advanced search
  2. The 1,000 free credits a month are not netted. Amounts are integer micro-USD (rate cards from
  `rc-2026-10-20`).
- **Reserve.** Before the reservation, a stage sets `ctx.planned_tool_units` (tool ID -> worst-case billed
  units). `build_envelope` adds their price to the reservation in the shared scopes and the public-demo scope.
  A tool is never a model line and never gets a model or GPU-pool budget.
- **Check, then call.** `finops.tools.check_tool_call` refuses (`RESERVATION_INSUFFICIENT`) a call the run's
  reservation does not cover, before it is made. `record_tool_call` meters the units the provider billed. A
  call billed past the plan is still recorded, because it is real spend.
- **Ledger.** Each call is one `usage_ledger` row of charge type `tool_call`, with no attempt and the tool's
  rate card. It settles as a `tool` charge, in the shared scopes only. In the live domain it counts only when
  it is money (`measured` or `estimated`); reconciliation reads it back the same way.
- **Response.** `cost.tool_micros` (part of `total_api_micros`) and `cost.tool_provenance`. The direct-frontier
  estimate excludes tool calls: it assumes the frontier gets the same context with no search.

**Amendment (2026-10-21): the answer-quality comparison (ADR-0023 §6).** A presenter's comparison is budgeted
apart from the run it compares (`finops.quality`).
- **Envelope.** One line per model at the worst case of one attempt with no retries: the frontier reference at
  the full untrimmed input bound and its output cap, and the grader at its fixed output cap. A grader is never
  called for more than its line covers.
- **Reservation.** In the live domain, against the run's tenant, department, application and user scopes, the
  request cap and each line's model scope. It never uses a workflow, the public-demo scope or a GPU pool. The
  idempotency key is `qc:{run_id}`. While the hold is open, a repeated reserve returns it. After settlement it
  raises `RESERVATION_ALREADY_SETTLED`, so a run is compared at most once. The caller serializes concurrent
  requests for one run, because only one settle counts.
- **Ledger.** Each call is a model attempt of stage `quality_reference` or `quality_grading` and one
  `usage_ledger` row with the charge type of the same name. The row is on the original `run_id`, with the
  comparison's own `reservation_id` and the attempt's provenance (`measured` from provider usage). It is
  measurement cost, like `baseline_comparison`, and never savings. The served run's reservation and spend are
  unchanged. Migration `fin_0005` widens the attempt stage (and the Insights aggregate `stage`) to 32 characters
  for these names.
- **Direct frontier.** `direct_frontier_measured_micros` is the reference call's cost, or null without a
  reference answer. It sits beside the estimate (ADR-0021) and never replaces its provenance.

**Amendment (2026-10-26): internet key calls and the in-house GPU endpoints (ADR-0027 §3).** ADR-0027 lets
three applications take key calls from the internet, and the sandbox's $5 monthly cap bounds their API spend.
It does not bound GPU hosting. A GPU estimate commits only to its GPU-pool bucket, and no API budget sees the
idle tail of a woken endpoint. So a leaked key could keep the sovereign endpoint (A100, $2.50/h) and the CIM
endpoint warm without touching that cap. The owner decided (2026-10-26) to add both limits below. A run is an
*internet key call* when its request policy decision reports `matched_network_id: "internet"` (an
authenticated application key admitted only by the `internet_callers` grant) and it runs in the live domain.
- **Never wake a sleeping endpoint.** The ADR-0019 guard for a public visitor's live run also applies to an
  internet key call (`Orchestrator._stays_asleep`).
  - The endpoint its plan will call is checked before the first billable stage, and the final choice again
    before the upstream reservation.
  - A sleeping CIM endpoint is bypassed, and the full context is used.
  - A sleeping upstream endpoint, the sovereign one included, refuses the run with nothing reserved or
    metered: `LIVE_DEMO_UNAVAILABLE`, `details.reason: "endpoint_cold"`, not retryable. The run is never
    rerouted, so a confidential key call fails closed while the sovereign endpoint sleeps.
  - A key called from one of its application's trusted networks, a user and a Test Console run can still
    wake an endpoint, as before.
- **A GPU budget for internet key calls.** These runs also draw on the tenant's `internet_gpu` scope. One scope
  covers the three applications, and its meter is `gpu`.
  - It holds the envelope's estimated GPU lines (every pool: sovereign and CIM) and commits the run's GPU
    estimate, labelled `estimated` and never `measured`. It never counts API spend, which the applications'
    budgets already cap.
  - `config/tenant.yaml` sets it to $5.00 a month with `block` (the owner's value, adjustable).
  - It is checked atomically with the other scopes before any billable stage. A run with no GPU line reserves
    nothing in it, so an exhausted GPU budget never refuses an external-only answer.
  - Without a configured `internet_gpu` budget, an internet key call's live run is refused
    (`internet_gpu:unconfigured`) rather than left uncapped, as for the public demo.
