# ADR-0019: Public demo: replay by default, capped live runs

**Context.** The owner decided on 2026-10-15 that the Control Center (Space 1, the POC display) is public
and that everything else stays private: the gateway (Space 2), the Context Lab (Space 3), Endpoints A/B
and the model and dataset repositories. Public visitors must be able to launch the scenario tests and see
the value of the platform. The owner chose recorded replies by default, with an optional live run limited
by a daily cap and a per-visitor limit. Two earlier decisions do not fit a public audience as they stand:
ADR-0010's passwordless demo sign-in must never be reachable from a public Space, and ADR-0009's inference
mode applies to the whole process.

**Decision.**

*Topology.*
- Space 1 serves the built UI and an nginx proxy at `/gateway/` that calls Space 2 server-side.
- The proxy sends `Authorization: Bearer <HF token>`, a fine-grained token that can read Space 2 only, so
  that HF's edge lets the call through. The visitor's AiVa token moves to `X-AiVa-Authorization`.
- The proxy forwards request headers from an allowlist only, and adds a proxy-identity header. The gateway
  trusts the forwarded client address only when that header is present and valid.
- With `AIVA_INGRESS=platform-proxy` the gateway never reads `Authorization`, which carries the HF token.
  It takes the AiVa credential only from `X-AiVa-Authorization`.
- Behind the proxy, a run's observed source address is null: the visitor is on the internet, and policy
  denies network-gated applications. The attested client address only keys rate limits, through an HMAC
  client reference. The raw address is never recorded. CIDR demos keep using the console's simulated
  source IP (ADR-0007).
- Space 3 uses the same pattern.
- Space 1's repository is public, so it holds only the built UI and the proxy configuration: no secrets and
  no gateway code. The HF token is a Space secret.

*Visitors and sign-in.*
- A visitor's browser calls `POST /v1/auth/public/session`. It is accepted only with a valid proxy proof,
  or in the `local` and `test` environments. It mints a 2-hour token for the seeded principal
  `u-public-visitor` (role `public_visitor`, department `public-demo`).
- The token carries a random session id, and the visitor's pseudonym is derived from it with an HMAC. So
  each browser session is its own pseudonymous visitor: runs, artifacts, workflows and quotas are keyed on
  it, and visitors cannot see each other's. Session minting is rate-limited: 5 per source per hour, 200
  per hour tenant-wide.
- What the role may do:
  - launch tests: console chat, agent workflow and artifact runs under a simulated identity, and policy
    dry runs;
  - hold `chat:live`;
  - read its own runs and traces, and application-initiated ones;
  - read reports tenant-wide without the user dimension: user filters and grouping are refused, and
    principals are shown as hidden;
  - read policies and the catalog.
- What it may not do: decide approvals, read or write budgets, write policies, export data, resolve
  identities, run evaluations or administer anything. The remaining live budget comes from FinOps' own
  read-out route.
- The owner and demo users sign in with a passcode (`AIVA_DEMO_PASSCODE`, at least 16 characters):
  constant-time comparison, rate limits and a lockout. On every deployed gateway this replaces ADR-0010's
  passwordless demo sign-in. Passwordless sign-in stays available only in the `local` and `test`
  environments, and `test` is never deployed. Startup refuses it anywhere else.

*Execution mode per run.*
- The gateway chooses each run's execution mode: `replay` (the default) or `live`.
- The request may ask for one (an additive `execution_mode` field). Public visitors default to `replay`;
  signed-in users default to the gateway's configured mode.
- Replay uses recorded exchanges (ADR-0009). The scenario set is recorded against the deployed
  configuration and ships with the gateway image.
- A replay miss, such as a free-text prompt, is answered by the simulated adapter and labelled
  `simulated`. It never makes a network call.
- The response states which mode actually ran. Replayed and simulated values are never shown as measured
  (ADR-0006).
- Budgets keep two money domains (amended in ADR-0008): live and illustrative.
  - The same budgets, caps, alerts and approvals apply in both, with separate buckets.
  - Live runs reserve and commit in the live domain.
  - Runs that cannot spend money (replay mode or simulated adapters) do the same in the illustrative
    domain, labelled `replayed` or `simulated`.
- So replay traffic can never drain live budgets or block live runs, while the replay demo still shows
  the 80% and 95% alerts, the hard-cap denial and budget approvals (LLD §23).
- A public visitor's illustrative buckets are their own, so one visitor's replays cannot fill the caps for
  others.

*Live runs for visitors.* A visitor's live run happens only when all of the following hold:
- the visitor asked for it;
- the visitor's pseudonym has used fewer than 5 live runs in the last hour (quota engine, ADR-0008);
- the `public-demo` daily budget ($10, a FinOps budget scope reserved before every billable stage like any
  other scope) has headroom;
- policy allows the request as usual. Every Appendix B rule is unchanged; for example, a confidential
  request still takes the sovereign path to Endpoint B.

When the limit or budget is exhausted, the gateway refuses with `LIVE_DEMO_UNAVAILABLE` and the reason,
and the UI offers the replay run. A live request is never silently downgraded.

*Cold endpoints.*
- Endpoints A/B scale to zero, with a cold start of about 150 to 200 s, and are not kept warm for public
  traffic.
- A request to a scaled-to-zero endpoint wakes it, and every wake bills at least the 15-minute idle tail.
  The daily cap counts only busy time. So a public visitor's live run never wakes an endpoint: the gateway
  checks the endpoint's state first and calls it only if it is already running.
- Otherwise a CIM call falls back to the full context (ADR-0009), and a sovereign call is refused with
  `LIVE_DEMO_UNAVAILABLE` (reason `endpoint_cold`); the UI offers replay.
- Endpoints wake only through the owner's warm-up or a signed-in user's live run.

*Measured value.* Measured figures come from live benchmark runs the owner approves after the spend is
quoted. They are published read-only in the Control Center with their date, configuration and provenance
`measured`.

**Consequences.**
- Anyone with the URL can spend up to the daily cap on live inference. The reservation path enforces the
  cap; only reconciliation of estimated charges can move the total, and that adjustment is bounded.
- A visitor who opens new sessions gets a fresh hourly limit. Session minting caps that at about 25 live
  runs per source address per hour, and the daily cap holds regardless.
- The demo accepts free text from the public. The UI tells visitors not to enter real or personal data,
  and DLP and policy apply as for every request.
- Recordings must be refreshed whenever prompts, the knowledge base, model pins or CIM packaging change.
  Re-recording costs cents to a few dollars and is quoted to the owner.
- This decision supersedes ADR-0010's demo sign-in for deployed gateways.
- Owners of the work:
  - Govern: public principal, passcode sign-in, proxy identity, live permission.
  - Optimize & Inference: per-run adapter selection, replay-miss fallback.
  - FinOps: `public-demo` budget scope, per-visitor live quota.
  - Control Center UI: Replay/Live toggle, mode and provenance labels, remaining budget, warm-up and
    fallback messages.
  - Deploy: proxies, recordings shipped in the image, the deploy plan.
  - Scenarios: recording the scenarios against the deployed configuration.

**Amendment (2026-10-19): session limits as settings, local and test only.** The public-session limits
(5 per source and 200 tenant-wide per hour) are now settings: `AIVA_PUBLIC_SESSIONS_PER_SOURCE` and
`AIVA_PUBLIC_SESSIONS_PER_TENANT`, with those values as defaults. The Control Center e2e suite and the replay
gates need more sessions per source than the demo allows.

- A changed value is accepted only when `AIVA_ENVIRONMENT` is `local` or `test`. A deployed gateway with
  either set refuses to start.
- Outside local and test, the limiter uses the defaults even if the start-up check is bypassed
  (`Settings.public_session_limits`). The public Space therefore keeps 5 and 200.

**Amendment (2026-10-25): visitors reach the gateway directly (ADR-0025).** The public demo's proxy is no
longer Space 1's server-side hop to a private Space 2. Space 2 is public and runs the proxy itself, an nginx
front door before the gateway on its loopback, and the UIs call it from the browser. The rules here are
unchanged: replay by default, capped live runs, the proxy proof, per-visitor limits keyed on the attested
address, and the tenant-wide caps. The browser sends the AiVa token only in `X-AiVa-Authorization`. On HF,
CORS is not a security boundary: the guarantees rest on header-only credentials, the proxy proof, per-visitor
limits and the tenant-wide caps (ADR-0025).
