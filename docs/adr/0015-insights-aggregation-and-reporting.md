# ADR-0015: AiVa Insights aggregation, reporting access and exports

**Context.** LLD §21 makes consumption reporting mandatory: grouped token and spend totals must reconcile exactly
to the attempt-level ledger, actual, estimated and avoided usage must stay separate (§21.4, ADR-0006), users are
pseudonymous by default with audited identity resolution (§21.8), exports are asynchronous, RBAC-enforced,
time-limited and audited (§21.6), and the anomaly rules of §21.9 raise alerts. `report_aggregate_daily` existed
from P0 but nothing wrote it, and its grain (one row per application/department/category/model/path/day) could
not attribute tokens to the model of each attempt or keep provenance apart.

**Decision.**
- **Line grain.** Aggregates are built from three kinds of *line* per run: one `run` line (request counts,
  outcomes, cache hits, context tokens, reservations, modelled savings), one `attempt` line per model attempt
  (tokens, ledger charges, retries, latency) and one line per ledger row that is not an attempt's own charge
  (`adjustment`, or `charge`). Measures of different kinds never overlap, so any grouping sums exactly to the
  ledger. Each line carries 23 run dimensions (application, department, cost centre, pseudonymous principal,
  classification labels, path, strategy, CIM state, outcome, source policy, policy version, deny rule,
  environment, data designation) and 6 line dimensions (kind, model, provider, stage, provenance, rate-card
  version). `line_key` is a hash of all dimensions; rows are keyed by `(tenant, bucket, line_key)`.
- **Attribution and refresh.** Usage belongs to the UTC hour its run started. `report_aggregate_hourly` is
  rebuilt hour by hour (delete + insert) under a per-tenant advisory lock; each touched day is rolled up into
  `report_aggregate_daily` from the hours. A refresh is idempotent and can be re-run for any window. The
  incremental refresh rebuilds the hours of runs recorded, and of ledger rows written (late corrections), since
  the previous completed refresh started, minus a 10-minute margin for transactions still committing.
  `report_refresh` logs every refresh. The worker runs in the gateway every `AIVA_AGGREGATE_INTERVAL_SECONDS`
  (default 60, 0 = off), off the request path: a failure is logged and retried next interval.
  `aiva-admin aggregate [--from --to]` runs it on demand.
- **Classification labels** are computed in SQL once and shared by the aggregates and the trace list: a
  reviewed label wins; a prediction with `review_needed` or a calibrated confidence below the taxonomy's
  `review_confidence_threshold` is reported as `unclassified` (task category and complexity only: business
  function, sensitivity and language are not predictions). Taxonomy and classifier versions are dimensions.
- **Provenance.** Token and spend totals are split by attempt provenance (`measured`, `estimated`,
  `simulated`, `replayed`); a figure's label is the least trustworthy of its parts, and figures from
  synthetic traffic are never labelled better than `synthetic`, in the aggregates and in the trace list and
  detail alike (a run's attempts keep their own provenance as evidence). Avoided usage (full response-cache
  hits) is modelled and reported separately; GPU estimates are at best `estimated`.
- **Traffic flow** (the overview's `flow`): each request is counted once, from its run dimensions. A request
  with no route is stopped: refused by policy, held for approval, over quota, or failed before routing. Any
  other request is counted under the route it took: response cache, sovereign direct, local fast, the CIM's
  package (a CIM-assisted run whose context strategy is not `full`) or full context. A run that failed after
  routing still counts under its route. The buckets partition the request count.
- **Packing** (`packing` on the overview and optimization reports) is measured as passages kept, not as
  context tokens against the caller's prompt, which counts retrieval as growth. On the requests that sent the
  CIM's package (Context Guard verified, or CIM-selected), it counts the passages the CIM was given and those
  its package kept, recall floor included. The counting rule is the run view's (`optimize.cim.passage_counts`)
  over the stored lists, so a week and a run always agree. It also reports the input tokens the answering
  model read on those requests (first calls). Two run-line measures hold the counts (`ins_0002`); history is
  filled by re-aggregating. A public visitor sees passages kept, not the prompt-to-sent token change.
- **Savings** (LLD §21.4, §22.7), modelled per served run against the pinned frontier model at the current
  price snapshot: baseline = frontier price of the original context and actual output; context step =
  baseline minus the frontier price of the constructed context; routing step = that minus the served model's
  list price; prompt-cache step = list price minus actual cost; cache hits avoid the frontier cost of the
  answer they reused. Every price is taken on one token basis: the served attempt's metered input tokens.
  The original context is scaled to that basis by the ratio of the gateway's estimates, so routing on the
  frontier model is exactly 0. Pricing the frontier on the estimate against a provider's count made routing
  negative whenever the provider counted more tokens than the estimate. Removed context is priced at up to
  three provider tokens per estimated token; a larger gap is template overhead. CIM calls, failed or retried
  calls, billed tool calls (web searches, their own step), ledger corrections and the local GPU estimate are
  added back as overhead steps, so the net is the
  whole cost of serving. Calls that measure a run rather than serve it are a measurement cost: the opt-in
  baseline comparison and a presenter's answer-quality comparison (ADR-0023: the `quality_reference` and
  `quality_grading` attempts and their ledger rows, with their own spend and token fields). It is shown after
  the net and never subtracted from the savings (LLD §22.7 lists CIM, GPU and retries as the overheads), so
  the net plus the measurement cost is exactly all actual API spend plus the GPU estimate. A baseline
  comparison's "actual" side leaves the measurement calls out. Not quality-adjusted.
  The baseline comparison is metered as `baseline_comparison`, and its result is reported
  separately with the run's quality pass/fail, never as a saving; baseline outputs are not graded in this
  phase. A public visitor sees one savings figure: that comparison, on the requests that really sent a
  baseline, with its pricing basis in one line; the full waterfall is for administrators and finance (Story
  mode). The routing step was reconciled on 2026-10-19 (one token basis, #146). Since 2026-10-19 the original context is the full context the CIM could prune
  (the messages and every eligible passage, rendered as sent, with the same estimator as the constructed
  context), so a full-context run has no context step; earlier runs count the caller's messages only.
- **Story mode hero figures** (`value_kpis` on the overview; owner, 2026-10-22). These are /week's four
  figures, on the same definitions as the /live "This session" counters (`live/counters.ts`, `quality.ts`),
  so a week of runs and a session of the same runs agree. A test checks this run by run, against each run's
  chat response.
  - *Cost*: AiVa's answering-model, CIM and billed tool-call spend, against FinOps' direct-frontier estimate,
    on the requests priced both ways (answered, or a full cache hit). The estimate is the run's frontier
    baseline above. For a cache hit, that is priced on the source run's upstream tokens as the cache entry
    models them. A cache hit whose source run is no longer on record has no frontier price and is left
    out of both sides. Local GPU hosting is left out, as on /live, and reported beside it
    (`hosting_excluded`).
    Ledger corrections and measurement calls are also left out, so the waterfall's net savings differ from
    this figure by them.
  - *Tokens*: an answered request's base is its original context and answer, and the context the CIM pruned
    is saved. A full cache hit's base and saving are the tokens it avoided. Blocked and failed requests
    add 0.
  - *Quality*: the presenters' comparisons (ADR-0023), read at request time from their `quality.compare`
    audit rows, never text. Each run counts once, with its latest comparison that called the reference, and
    has a score when the grader gave one. The mean is null when no run was compared, never 0. The provenance
    is estimated at best.
  - *Governance*: requests, those stopped by policy (`policy_denied` and `approval_required`), and the
    confidential-or-above requests served on the sovereign path.

  Six run-line measures (`ins_0004`) hold the per-run parts. Every figure keeps the labelling rules above.
- **Confidential egress** (governance report): `confidential_external_attempts` counts external attempts on
  confidential-or-above requests and must be 0. Answer-quality comparison calls on a run that meets ADR-0023's
  exception are counted apart (`confidential_comparison_attempts`, run-line measure, `ins_0003`) and shown as
  a disclosed warning, never hidden and never a leak. Both must hold: the gates the run record holds (tenant,
  scenario, exactly confidential, not local-only, synthetic data), checked by the aggregation itself, and the
  call's own egress policy events (its model, under the comparison's reservation) all `allow`, which on a
  confidential run means the whole exception held, synthetic sample and presenter's request included. Any
  other comparison call that left a confidential run counts as a leak.
- **Access.** Every report applies a row-level scope in SQL: tenant-wide for platform and tenant
  administrators, finance analysts and security auditors; the department for department managers; owned
  applications for application owners (union of roles). A `tenant_id` other than the caller's, or a
  department or application filter outside the scope, is refused with 403; a trace outside the scope is 404.
  Users are pseudonyms (`usr_…`) or service principals (`service:<application>`); a user run recorded without
  a pseudonym is reported as `unattributed`, never by a user ID or as the application. Resolving a pseudonym
  needs the new `reports:identity` permission (tenant administrators and security auditors), a stated reason,
  and writes an audit row naming the pseudonyms.
- **Exports.** `POST /v1/reports/exports` records the request, the requester's scope snapshot and an audit row,
  then renders the CSV in the background from the same report queries. Exports are bound to the requester's
  pseudonym, never a user ID. Only the requester can download it, until `AIVA_REPORT_EXPORT_TTL_SECONDS`
  (default 1 hour); expired content is purged and refused with 410. Each download re-checks the caller: it
  still needs `reports:export`, and its current scope must still cover the recorded scope snapshot, else 403
  (audited as `report.export.download_refused`). Every download is audited. Files carry metadata lines
  (filters, period, timezone, scope, generation time, taxonomy and classifier versions, pricing snapshot, data
  designation), pseudonymous users only, never prompt text or resolved identities, and text cells are
  neutralised against spreadsheet formula injection. Rendering runs in a task of the gateway process, so a
  restart orphans exports in flight: when the worker starts it records every export still queued or running as
  failed (`interrupted`, audited as `report.export.interrupted`), and each interval does the same for any in
  flight longer than 10 minutes; a late renderer never revives a failed export. This assumes a single gateway
  process, as each POC Space runs: a second replica starting would fail exports another replica is still
  rendering, so running several needs a per-process owner column or a lease on export jobs (Wave 3).
- **Quality report** (`GET /v1/reports/quality`): output assurance, CIM sufficiency, Context Guard verdicts and
  answer-citation coverage per application, context profile or execution path, with a trend. It is computed
  from the run records at request time (`computed_at`), not from the aggregates, under the same scope, filters
  and provenance cap. Guard failures, escalation reasons and check names are returned as fixed codes, never as
  stored text. Coverage is `not_measured` until runs with citations are checked, never 0, and every 0/0 rate is
  null.
- **Anomaly rules** (`config/insights.yaml`): per-application token spikes against the trailing hourly average,
  expensive-model spend share changes, repeated retries and high output-token ratios, and per-user
  consumption against the median of the application's other users. They run on the hourly aggregates after
  each refresh and write through the FinOps alert store (`record_alerts(..., on_conflict="ignore")`) with a
  deterministic ID per tenant, rule, scope and hour (`alr_` + 32 hex), so re-runs never duplicate or lose an
  alert. Each alert carries the least trustworthy provenance of its inputs; user-scoped alerts name the
  pseudonym only. They never touch budget counters or the ledger. Under the alert scope filter, user-scoped
  alerts are visible to the tenant-wide roles only, not to application owners or department managers.
- **Synthetic traffic.** `aiva-admin traffic` runs a seeded plan through the real pipeline in-process (as
  `POST /v1/ai/chat` after authentication) with generated pseudonymous Test Console identities, simulated or
  replayed inference only, designated `synthetic`. Its core plan covers every §21.10 dimension.

**Consequences.** Reports read aggregates and are as fresh as the last refresh (`aggregates_as_of` in every
report). Modelled savings use the current price snapshot, so re-aggregating after a rate-card change restates
them; ledger amounts are never restated. The §21.10 two-language case is deferred with the Arabic phase
(ADR-0001): the language dimension is recorded and reported, but traffic is English only. The chart colours of
the Insights views are local to `insights/charts.css` until the UI workstream promotes them to tokens.
