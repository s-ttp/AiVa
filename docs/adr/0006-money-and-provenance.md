# ADR-0006: Money as integer micro-USD and provenance labels

**Decision.**
- All monetary amounts are integers in micro-USD (`*_micros`, 1 USD = 1,000,000). Prices are stored per
  million tokens in micro-USD. No floats in cost paths. Currency is USD (§20).
- Every measured quantity carries a provenance label: `measured` (provider-reported), `estimated`
  (tokenizer or model estimate), `simulated` (placeholder or demo), `replayed` (recorded fixture),
  `synthetic` (generated demo traffic). Reports separate actual, estimated and avoided usage (§21.4).
- Local GPU API token price is $0 by POC convention; GPU cost is reported separately as estimated.

**Consequences.** Aggregates reconcile exactly to the ledger; UI shows provenance on every KPI.

**Amendment (2026-10-15): prices and amounts.** This adds to the decision above; it changes none of it.
- Provenance labels apply to amounts (cost, spend, savings, forecasts), never to prices.
- A price is configuration. It is cited by rate-card `id`, rate-card `version`, `source` and
  `effective_from`. There is no "configured" label.
- Prices convert to integer micro-USD exactly. The catalog loader rejects a negative price or one with
  more than 6 decimal places.
- The GPU hourly rate (`gpu_per_hour`) is a configured price. The GPU cost of a request (busy time ×
  rate) is an amount and is labelled `estimated`.
- GPU-served models have a $0 token price by POC convention. The UI shows it as "$0 (GPU-served; GPU
  cost reported separately)", never as free.

**Amendment (2026-10-20): direct-frontier estimate (live demo).** The chat response's
`cost.direct_frontier_estimated_micros` is what the request would have cost sent straight to the pinned
frontier model (`frontier_baseline`) with no AiVa optimization (owner decision: always estimated, no
baseline call).
- It is the frontier's list price, with no cache discount, of the full original context and the served
  answer's output tokens. The original context is counted on the served attempt's metered token basis, the
  same as the savings waterfall's frontier baseline (`reporting.lines.metered_original`), so the live
  counter and Insights agree for the same run. On a cache hit it is the frontier price of the reused answer.
- It is arithmetic only. No call is made on any path, the sovereign path included.
- It is labelled `estimated`, or `replayed`/`simulated` when the tokens it is priced on are, and is never
  `measured`. It is null when no answer was produced. It is never mixed with the measured baseline
  comparison (`baseline_micros`) and is never written to the ledger.
