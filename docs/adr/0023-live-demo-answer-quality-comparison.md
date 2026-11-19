# ADR-0023: Live demo answer-quality comparison

**Status.** Accepted. The owner decided this on 2026-10-21. It contains the one owner-approved exception to the
Appendix B sovereignty rule, limited to the synthetic sample of scenario 11.

> **Open-source build.** The answer-quality comparison ships, but the exception does not: AIPEL-INV-001 has no
> carve-out here, so confidential data never reaches the reference or the grader. The policy tests and
> `gateway/tests/invariants/test_quality_exception.py` assert that.

**Owners.**
- Govern: this ADR, the comparison policy (`AIPEL-QUAL-001` to `-003`, and the exception), `demo:compare`
  and audit.
- Optimize & Inference: the quality-check endpoint, the reference and grader calls, the grader's pin and
  `answer_sha256`.
- FinOps & Ledger: the comparison's envelope, metering and `direct_frontier_measured_micros`.
- Control Center UI: the comparison on `/live` and its required line.

**Context.** `/live` (ADR-0021) shows tokens, cost and an estimated direct-frontier figure, but nothing about
answer quality. On 2026-10-21 the owner asked: "should we also do a check of the output of the 3 use cases
(except the frontier use case) compared to frontier and display the answer quality check also as a KPI"
(05:12). The owner then answered three questions:
- Scope (05:18): "all use cases including the soverign".
- Grader (05:18): "use open ai frontier model. i will provide the api keys".
- Sovereign (05:19): asked how the sovereign use case should get a check without its confidential data
  leaving, the owner chose "Send it out anyway" over in-country checks with an offline evaluation (the
  recommendation) and a self-hosted grader. That option said: "Send the sovereign data to Claude and OpenAI.
  This breaks the non-negotiable sovereignty rule and its tests, and the demo would show confidential data
  leaving. Not recommended."

So this is an informed decision to make one exception to "confidential, restricted and sovereign-only
payloads never reach CIM or an external provider". This ADR keeps it as narrow as that decision allows:
- the data is synthetic: scenario 11's committed sample, never typed text;
- the served answer never changes;
- every other confidential egress is still denied;
- no existing test is edited or skipped.

**Decision.**

*1. What a comparison is.*
- After a live answer on `/live`, a presenter may ask for a comparison: `POST /v1/runs/{run_id}/quality-check`.
- **Reference:** the pinned frontier model (`frontier_baseline`) answers the same question on the full,
  untrimmed context. No CIM, no cache.
  - For market research (ADR-0022), that context includes the same screened web passages the served model
    received. They are held only in the gateway's process memory, keyed by run id, for at most 30 minutes,
    with a small cap on the runs held. They are never written to disk, Redis, records, spans or logs.
  - If they are gone (expired, dropped by the cap, or held by another gateway process), the comparison runs
    without them and says so. It never searches again.
- **Grader:** the pinned grader (`models.grader_model`, a `grader`-role model) judges the served answer
  against the reference with a rubric. It returns a verdict (`match`, `partial` or `miss`), a score from 0 to
  100 and the missed points.
  - The grader is an OpenAI model. Optimize pins it in `config/models.yaml` once the owner confirms the model
    id. It reads `AIVA_OPENAI_API_KEY`, which is never logged or printed.
  - The verdict is `estimated` (AI-judged), never `measured`.
- The grader sees only the question, the evidence, the reference answer and the served answer: nothing the
  reference was not allowed to see.
- The served answer is never changed, re-routed or replaced.

*2. Who may compare.*
- A new permission, `demo:compare` (`Permission.DEMO_COMPARE`), is granted to platform administrators,
  tenant administrators and testers.
  - It is never granted to the public visitor (`public_visitor`).
  - It is never available to an application credential, because `granted` requires a user.
- The endpoint checks authorization before the body: 401, then 403, then 422 (ADR-0007).
- The run must be the caller's own: the same principal pseudonym and application. Otherwise it returns 404.
- Live only: the run was served live and the gateway is in live mode. A replay, record or simulated run
  gets 409 and nothing is called. Comparison calls are never recorded (ADR-0009).
- Policy checks the presenter role again before each call (`AIPEL-QUAL-001`). A wrong grant alone does not
  open it.

*3. Bound to the served run.*
- Run records keep no text (Appendix B), so the request resends the original request and the served answer.
- The gateway checks:
  - the request against the run's `request_hash`;
  - the answer against `execution_run.answer_sha256`, a hash written when the answer is served (Optimize's
    migration).
- On a mismatch it returns 409 and calls nothing. No other text can be sent out under a run's name.
- Web passages are never resent by the client. The reference gets them only from the gateway's memory
  (point 1).

*4. Policy before each call.* There are two new egress stages, `quality_reference` and `quality_grading`.
1. Evaluate egress policy for both calls.
2. Reserve the envelope (point 6) only if both are allowed.
3. Evaluate again before each call (Appendix B).

The rules are:
- **`AIPEL-QUAL-001`:** a comparison stage without an explicit presenter request is denied. An explicit
  request means `quality.requested` from the endpoint, on a user principal with a presenter role and never
  `public_visitor`.
- **`AIPEL-QUAL-002`:** the reference must be the bundle's `quality.reference_model_id` (`frontier_baseline`)
  and the grader its `quality.grader_model_id`. With no grader registered, there is no grading call.
- **`AIPEL-QUAL-003`:** the grader is denied when external providers are denied for the request, outside the
  exception. That covers confidential and above, `local-only` routing and any policy document.
- **The reference** keeps `AIPEL-EGRESS-001`: it must be in the request's allowed set.

The non-sovereign use cases (frontier optimization, inference API, market research) need no exception.
Normal policy decides for them, including for a presenter's free text. Points 2, 3, 6 and 7 still apply.

*5. The exception: scenario 11's synthetic sample only.* `quality_exception` holds only when every gate holds:
- a comparison stage, with an explicit presenter request (as in `AIPEL-QUAL-001`);
- the pinned model (as in `AIPEL-QUAL-002`);
- the scenario `live-sovereign-routing`;
- `quality.synthetic_sample`: the request's messages and evidence are exactly one of the scenario's committed
  samples: one user message with `sample_prompt`, or one of its committed `alternate_prompts` (2026-10-25
  amendment), and `sample_evidence` in order.
  - The gateway computes this (`govern/quality.py`). The run binding (point 3) makes sure it is the text
    that was served.
  - Typed or edited text, an attachment or any other change gets no exception. The page says "Comparison is
    available for the synthetic sample only."
- the deployment's data designation `synthetic`, and the tenant `gct` in both the input and the bundle;
- an effective classification of exactly `confidential`. Restricted and sovereign-only data never leave;
- a routing mode other than `local-only`.

When the exception holds, `AIPEL-INV-001` and `AIPEL-EGRESS-001` do not deny these two calls. Nothing else
changes:
- deny actions still deny;
- with the same facts, upstream, baseline, validation and CIM calls are still denied;
- the served answer takes the sovereign direct path.

The scenario and tenant are literals in the policy. Widening the exception needs a policy change and its own
decision, not a configuration edit.

*6. Budget (FinOps).*
- **Its own envelope.** A comparison reserves at request time, separately from the run.
  - The idempotency key is `qc:{run_id}`: one check per run. A repeated POST returns the same result and
    never reserves or spends twice.
  - It reserves against the run's application and tenant budgets in the live domain, never the GPU pool.
  - Its lines: the reference at the full untrimmed input bound with its own output cap, and the grader at a
    fixed maximum output.
- **Refusals.** A refused envelope means "comparison not reserved", with nothing partly called. An
  `approval_required` budget follows ADR-0014. The worst case is about $0.15 per comparison.
- **Sovereign lines** are reserved only when policy allows both calls under the exception. Otherwise
  `covers()` refuses, as today.
- **Metering.** Each call is a model attempt with charge type `quality_reference` or `quality_grading`,
  `measured` from provider usage. It is a ledger row on the original `run_id`, with its own
  `reservation_id` and the run's scope chain. It counts as measurement cost, never as savings.
- **Direct frontier.** `direct_frontier_measured_micros` takes the reference cost.
  `direct_frontier_estimated_micros` stays beside it (ADR-0021), and `/live` shows the measured figure when
  there is one. The sovereign model's GPU estimate is unchanged.

*7. Records.*
- Stored: the verdict, score, model ids, costs and outcome. Never text.
- `audit_log` gets `quality.compare` with the presenter's pseudonym, the run id, the scenario id,
  `exception_applied`, the verdict, score, model ids, costs and outcome. Never the question, evidence, an
  answer or a missed point.
- Spans use `aiva.quality.*` attributes through `redact_attributes`.
- The reference answer and the missed points are only in the HTTP response: never stored and never cached.

*8. What the page says.* On scenario 11, the comparison always shows: "Comparison only: this synthetic
confidential request was sent to Claude and OpenAI to grade it. AiVa's answer stayed on the sovereign
model."
- It does not say "in-country". In this POC the sovereign model runs on a dedicated endpoint outside the
  country, and its info balloon on `/live` says where.

*Tests.*
- No existing test is edited or skipped. That includes `test_confidential_never_reaches_cim_or_external` and
  FinOps' `test_calls_outside_the_reservation_are_refused`.
- `policies/aiva/quality_test.rego` covers:
  - the exception with every gate;
  - one negative per missing gate, naming its rule;
  - the served path unchanged.
- `gateway/tests/invariants/test_quality_exception.py` covers the same against the running policy engine
  and the real bundle, with real scenario 11 runs.

**Consequences.**
- Presenters can show answer quality as a KPI on every use case, the sovereign one included.
- On scenario 11, the demo deliberately shows synthetic confidential data leaving AiVa for two external
  providers. That is the owner's informed choice. It is:
  - labelled on the page;
  - audited per comparison;
  - limited to committed synthetic data. Typed text never gets the exception, so no real customer data can
    take this path.
- Appendix B's sovereignty rule now has one written exception. Any other, whether another scenario, real
  data, or restricted or sovereign-only data, needs its own decision.

**Amendment (2026-10-21): web passages for the reference, and the page's wording.** This changes no policy.
- Point 1: on market research, the reference gets the run's screened web passages from the gateway's memory,
  so both answers are judged on the same evidence. ADR-0022 notes this in its consequences.
- Point 8: the required line said "AiVa's answer stayed in-country". That is not true of this POC's hosting,
  so it now says "AiVa's answer stayed on the sovereign model".

**Amendment (2026-10-21): an even comparison, an anchored rubric.** Owner-approved after the score sat at about
65 to 68 on every live comparison. This changes no policy.
- **Why the score was low.** Three causes, from the day's live comparisons:
  - The reference had a larger budget: `max(request, 6,144)` output tokens against the served model's 1,024.
    On market research the reference wrote 2,172 output tokens to DeepSeek's 993; on the sovereign run, about
    three times Qwen's 360.
  - The score asked "how much of the reference's key information the answer gives", so a longer reference
    meant more key points and a lower score.
  - The score had no anchors, so "partial" clustered at 65 to 70.
- **Point 1, the reference.** It answers within the served run's budget: the request's `max_output_tokens`
  for its answer, plus a thinking allowance of 1,024 tokens.
  - Opus thinking cannot be turned off and counts against `max_tokens` (ADR-0009, 2026-10-18 amendment).
    The provider has no separate thinking budget, so the allowance is added to the cap.
  - The allowance is sized from measured effort-low thinking. The recorded effort-low baselines used 0 and 67
    thinking tokens; the day's live references used 107 and 487. 1,024 is about twice the largest.
  - The reference is asked to answer as a helpful assistant normally would, at a length that suits the
    question. When it thinks less than the allowance, its answer could run past the request's cap by the
    difference; the instruction keeps it to a normal length.
  - A reference cut off at its limit (`stop_reason: max_tokens`) is graded, never as if complete. Its note
    says so ("The reference stopped at its length limit, so it is incomplete"), and the grader is told.
  - The reservation's reference line is sized to the new cap. At the default request cap of 1,024 that is
    2,048 output tokens instead of 6,144, which lowers the worst case.
- **Point 1, the grader's rubric.** It judges whether the answer gives the information the question needs.
  The reference is one good answer, not a checklist, and the evidence settles facts.
  - Reference content the question does not need does not count. Length, format, wording and extra detail do
    not matter. What the reference note says the reference lacks does not count against the answer.
  - Score anchors: 90 to 100, every key point present and correct; 70 to 89, minor gaps; 40 to 69, important
    gaps; 0 to 39, wrong, refused or failed.
  - The grader returns only the score and at most five missed points (needed information that is missing or
    wrong, empty when nothing needed is missing), in the same strict JSON schema.
  - The verdict is derived from the score in the gateway, so the two never disagree: `match` from 90,
    `partial` from 40, `miss` below 40.
- **Calibration.** `gateway/tests/live/test_quality_calibration.py` checks the anchors with real calls: the
  reference against itself scores at least 95, its first third scores below 60, and the same pair graded twice
  stays within 5 points. It spends a few cents, so it runs only with `AIVA_QUALITY_CALIBRATION=1` and both
  keys. Scores before this amendment are not comparable with scores after it.

**Amendment (2026-10-25): scenario 11's question pool.** The owner chose to rotate the questions of all five
`/live` use cases, the sovereign one included, so each click asks the sample question or one of two
committed alternates.
- **The owner's decision.** On 2026-10-25 at 08:58 the owner chose "Rotate all 5" over the recommended "keep
  sovereign fixed". That option said: "As above, plus a pool for the sovereign case. That widens your ADR-0023
  exception from one committed sample to a few, so it needs a short ADR amendment and Govern's review."
- **Point 5's synthetic-sample gate** now holds for one user message equal to `sample_prompt` or to one of the
  scenario's committed `alternate_prompts`, with `sample_evidence` unchanged and in order
  (`govern/quality.py::is_synthetic_sample`).
- **What makes a question committed.** It is in `config/scenarios.yaml`, reviewed with the change that adds
  it. The loader requires exactly two alternates per live use case, each distinct and a valid request.
  Scenario 11's two (Scenarios' #228, reviewed by Govern) ask only about synthetic customer ACC-SYN-40217,
  from the same evidence.
- **Unchanged:**
  - the policy and every other gate;
  - the served path: each question takes the sovereign direct path, with no external call except the
    explicit comparison;
  - edited or typed text, another scenario's question, an attachment or changed evidence never counts.
- A further widening, such as a new question or different evidence, is again a reviewed change to
  `config/scenarios.yaml`. Nothing typed on the page ever qualifies.
