# ADR-0014: Approved budget exceptions

**Context.** A budget with `on_exhaustion: approval_required` turns a denied reservation into a `budget`
approval request (ADR-0012). When the request is resubmitted, Govern claims the approval for the new run and
passes its ID to the quota stage as `ReservationRequest.approval_id`. Until now FinOps ignored it, so the
resumed run was denied again. LLD §12 requires that caps never be exceeded by concurrent requests or
retries. An approved exception therefore has to be bounded, single-use and auditable, and it must never
widen what policy allows (LLD §17, Appendix B).

**Decision.**
- **FinOps verifies the approval itself** (`finops/approvals.py`) and never trusts the pointer. It applies
  the approval only when every one of these holds:
  - it belongs to the request's tenant and application;
  - its kind is `budget`;
  - it is `consumed` by this run (`consumed_by_run_id`);
  - it has not expired;
  - it names budgets and a positive approved amount;
  - no reservation has used it yet.

  Anything else is ignored, so the reservation meets the ordinary caps and fails closed. The denial message
  then says why the approval was not applied.
- **Approved amount.** This refines ADR-0012. `QuotaDeniedError.amount_micros` carries what the denying
  budget needed for the request: its share of the envelope.
  - For shared scopes and the request cap, that share is the whole envelope.
  - For a model budget, it is the model's line.
  - For a GPU-pool budget, it is the GPU estimate.

  The orchestrator records that amount as the approval's `approved_amount_micros`. For a GPU pool, the
  whole envelope's API amount would be $0 on the sovereign path, so the approval could never apply.
- **Bounded lift.** An applied approval lifts only the caps of the budgets it names, each by at most the
  approved amount, for one reservation.
  - The reserve script admits a bucket while `spent + reserved + amount <= cap + approved amount`.
  - Every other bucket keeps its plain cap.
  - If a named budget's share now exceeds the approved amount (because prices or the eligible pool changed
    since approval), the approval is not applied. The run is denied and raises a new budget approval for
    the new amount.
- **Single use, at three layers:**
  1. A durable check: no `budget_reservation` row carries the approval.
  2. An atomic marker in the reserve Lua script. The marker is checked and set in the same step that
     reserves, so concurrent uses are serialised and exactly one wins. The others are re-evaluated against
     the plain caps.
  3. A unique index on `budget_reservation.approval_id` (migration `fin_0002`). It catches a second use
     after Redis lost the marker: that hold is undone and the call fails with `409 BUDGET_APPROVAL_USED`.

  If the journal write fails for another reason, the hold is undone and the marker is freed. The approval
  funded nothing, so Govern's retry can still use it.
- **Evidence.**
  - The reservation journal records `approval_id`.
  - An append-only audit row, `budget.approved_exception` (actor `system/finops`), lists the approval, the
    lifted budgets and their amounts.
  - Each bucket taken past its plain cap raises a `budget_alert` of kind `approved_exception`, labelled
    `estimated` because it is the worst-case reservation.
- **Cap arithmetic only.** Eligibility, egress checks, sovereignty and lower-cost narrowing are unchanged.
  A confidential request still takes the sovereign direct path.

**Consequences.**
- A resumed budget approval completes once, within the approved amount. A replay, a second concurrent use,
  an expired approval, an approval claimed by another run or a larger envelope gets the ordinary caps.
  Where the budget asks for approval, that raises a fresh approval request.
- **Release interaction.** Govern returns a claimed approval to `approved` when the run fails on a
  retryable dependency error (`release_approval`). If the reservation was journaled before that failure,
  FinOps counts the approval as used. A retry is then denied with "it has already funded another
  reservation" and raises a new budget approval. This fails closed and never double-funds. Making such
  retries reuse the approval would need the second reservation to charge against what the first one spent.
  That is deferred.
- **Follow-up (parked).** The cache-write price tier: the Anthropic adapter folds cache-write tokens into
  `input_tokens`. That is exact today because the gateway sends no `cache_control`. A separate rate-card
  tier is needed before prompt caching is enabled (with Optimize).

**Amendment (2026-10-19): no approval for a budget already past its cap (owner decision).** An approval lifts
a cap by the request's own share only, so it can admit the request only while the budget's bucket holds at
most its cap. Past the cap, `spent + reserved + share > cap + share` on any resume, and the approval could
never be used.
- **The approval still covers exactly the request's share.** It never covers the shortfall, so an approver
  never authorizes someone else's overspend.
- **No approval is offered past the cap.** When the budget whose `approval_required` governs the denial
  already held `spent + reserved > cap` before the request, no `budget` approval request is created. The run
  is `blocked` (`QUOTA_EXCEEDED`, outcome `quota_denied`) and the error carries `details.reason:
  "budget_already_exhausted"`. Its message leads with "Budget already exhausted; raise the budget." and its
  remediation asks a budget owner to raise the cap.
- **This includes refused resumes.** Where the decision above says a refused resume "raises a new budget
  approval", it raises none while the budget is past its cap. An approval that was claimed but never used is
  still returned to `approved` (ADR-0016 workflows).
- **At the cap, an approval is still offered.** With `spent + reserved == cap` (including a $0 cap with
  nothing held, the usual "approve every request" budget), the approval admits exactly the request.
- **Fail-closed is unchanged.** The reserve script, the bounded lift and single use are as above. The rule
  only stops issuing approvals that could not be used. The usual way to reach this state is a cap set below
  what the period has already spent; the quota stage reports it as `QuotaDeniedError.already_exhausted`.
