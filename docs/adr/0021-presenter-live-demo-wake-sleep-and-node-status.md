# ADR-0021: Presenter live demo: wake, sleep and node status

**Status.** Accepted. This is an owner-approved feature (2026-10-20). It extends ADR-0019 for signed-in
presenters only; nothing changes for public visitors.

> **Open-source build.** The presenter's live demo page and its node-status, wake and sleep API are not part of
> this build. This record documents the reference deployment.

**Owners.**
- Govern: the `demo:wake` permission, audit, and this ADR.
- Optimize & Inference: the wake, sleep and node-status routes.
- Control Center UI: the `/live` page.
- Scenarios: the public-data application used by use case 4.

**Context.** The owner presents AiVa from a live page, `/live` in the Control Center, which follows the
owner's slide:
- an input box;
- four use-case buttons;
- the pipeline blocks, each lighting up as it is used;
- token and cost counters;
- the output.

The page is for presenters: users signed in through the demo sign-in (ADR-0019) who run live, not replay.
Endpoints A (`enterprise-cim`, 1×L4) and B (`gct-qwen-finetuned`, 1×A100) scale to zero. A cold start
takes about 150 s for A and about 360 s for B (measured in the 2026-10-15 window: A ready after 147 s, B after
356 s). So the page needs about 6 minutes before every use case is available. It wakes both in the background
when it opens, and a "Put to sleep" button scales them to zero when the presentation ends.

**Decision.**

*Who may wake and sleep the endpoints.*
- A new permission, `demo:wake` (`Permission.DEMO_WAKE`), is granted to platform administrators, tenant
  administrators and testers. The routes that wake Endpoints A and B, or put them to sleep, require it.
- It is never granted to the public visitor (`public_visitor`). It is never available to an application
  credential, because `granted` requires a user.
- ADR-0019 is unchanged for visitors: a visitor's live run never wakes an endpoint. A visitor may read node
  status, which needs `chat:live`, but cannot change it.
- Node status (`GET /v1/demo/nodes`) reports each node, `gateway`, `rlcd` or a model id, as `asleep`,
  `waking`, `ready` or `unavailable`.

*Audit.*
- Every wake and every sleep writes an `audit_log` row, `demo.endpoint_wake` or `demo.endpoint_sleep`.
- The actor is the presenter's pseudonym. The details are:
  - the endpoint's model id;
  - the state seen before the call;
  - the outcome (requested, already in that state, or failed).
- No token, URL or provider response body is recorded.
- A refused attempt (no `demo:wake`) is a 403 and calls nothing.

*Waking never happens by accident.*
- Reading node status uses the Endpoints API's state, never an inference call (`is_awake`, ADR-0019). So an
  open page does not keep an endpoint awake: the 15-minute idle scale-to-zero still applies.
- Only the explicit wake (on page open) and a presenter's live runs wake an endpoint.

*Cost.*
- While both endpoints are up, they cost about $3.30 an hour ($2.50 for the A100 and $0.80 for the L4;
  `config/rate_cards.yaml`, live prices fetched 2026-10-15).
- Every wake also bills at least the 15-minute idle tail, about $0.83 for both, unless "Put to sleep"
  scales them down first.
- A presentation of an hour therefore costs about $3.30 to $4.15. That is well under the $50 single-action
  threshold, so a presenter's wake needs no separate approval.

*Honest numbers on the page.*
- The "Direct frontier" comparison figure is always an estimate, labelled `estimated` (the owner's choice).
- The page makes no baseline call to the frontier model, so the figure is never shown as measured.
- Token and cost counters carry their provenance label, as everywhere else (Appendix B).

*Use case 4.*
- It runs on a new public-data application (Scenarios adds it to `config/tenant.yaml`). Its model
  allowlist excludes the sovereign model, so its live runs never need Endpoint B.
- Policy is unchanged. If the presenter's input classifies as confidential on that application, for example
  because DLP finds personal data, the run is blocked by AI policy. It is never routed to another model:
  confidential payloads take only the sovereign path (Appendix B).

**Consequences.**
- Presenters no longer need a separate warm-up before a demo, and they can stop the hourly cost as soon as
  they finish.
- Public visitors keep replay by default and capped live runs on endpoints that are already awake.
- `demo:wake` is the only way to wake an endpoint other than a signed-in user's live run, and every use is in
  the audit log.

**Amendment (2026-10-22): presenters read reports.** The presenter on `/live` signs in as the tester and also
opens the value page (`/week`), which reads Insights reports.
- The tester now holds `reports:read`, at the public visitor's scope (ADR-0019): the whole tenant, without
  the user dimension. That means no user filter or grouping, and run principals shown as `hidden`.
- It never exports (`reports:export`) or resolves identities (`reports:identity`).
- Roles still combine. A tester who also holds a role that sees users sees them. The visitor never does, even
  with another role.

**Amendment (2026-10-25): a sixth use case, code generation on the frontier (owner decision).** The page gets
a sixth use-case button, beside the five it has. The fifth, market research, is recorded in ADR-0022.
- Scenario 16 (`live-code-generation`, `live_demo_use_case: code-generation`) runs on the existing coding
  assistant application at the critical tier, which only the frontier model (`claude-flagship`) meets.
- It carries no evidence. Each of its three questions, the sample and two alternates, is a complete,
  synthetic telecom coding task.
- It is separate from "Frontier token optimization" (scenario 12), which stays as it is. Coding is not one of
  the CIM's trained tasks (`cim_trained_tasks` in `config/models.yaml`), so a coding request skips the CIM
  ("task outside the CIM's trained categories"). It therefore cannot show context packing. Scenario 12 shows
  the CIM packing a knowledge pack for the frontier model; scenario 16 shows the frontier model's work on code.
- Its runs need the gateway, RLCD and the frontier model only. Neither endpoint wakes for it.
- Its output budget is 8,192 tokens, and each question asks for a compact answer. The model's thinking counts
  against the budget: 4,096 truncated scenario 2's code answer (ADR-0009, 2026-10-18 table), and in the live
  check of 2026-10-25, 6,144 truncated two of scenario 16's three answers. At $20 per million output tokens, a
  run's output costs at most about $0.16, and its input under $0.01.
- Like the other live use cases, it stays out of the replay set, so no recording or replayed request body
  changes.
