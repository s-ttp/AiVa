# ADR-0025: Visitors reach the gateway directly; no Space-to-Space hop

**Status.** Accepted. The owner approved option B on 2026-10-22, and confirmed it on 2026-10-25. Govern
reviewed the ingress and security conditions.

**Owners.** Deploy & Hardening: `deploy/spaces/gateway/` (nginx front door, supervisord),
`deploy/spaces/shared/gateway-proxy.sh`, `deploy/scripts/push_spaces.py`, the smoke tests, the runbook.
Control Center UI and Context Lab: the browser client (credential header, "starting" handling). Govern:
the trust rules, which this ADR keeps unchanged.

**Context.**
- **Today's path is a Space-to-Space hop.** Space 2 (the gateway) is private. Space 1's nginx proxies
  `/gateway/` to it server-side over HF's edge, with one HF token for every visitor (ADR-0019, ADR-0007's
  2026-10-15 amendment). Space 3 does the same.
- **HF's edge blocked that hop three times**, each time for about 15 to 25 minutes, and the public demo was
  unreachable while it lasted:
  - 2026-10-21. HF's 429 page carried `RateLimit: "pages";r=0;t=212`.
  - 2026-10-22 at 09:11. A restart of Space 1 cleared it.
  - 2026-10-22 at 10:38. A brand-new Space 1 container was blocked on its first call. The 429 carried no
    `RateLimit` headers, so the documented quota could not be read (deploy PR #211 logs them).
- **The block is not the documented account quota.** Space 1's token belongs to the s-ttp PRO account. Direct
  calls to Space 2 with a token of the same account got 200 throughout. The evidence points to a limit keyed
  on Space 1's egress address, which Spaces share with other tenants. That is neither measurable nor
  controllable from our side.
- **HF has no private network between Spaces.** Every route from Space 1 to Space 2 crosses the same edge.

**Options.**
- **A. Merge.** Space 2 serves the Control Center too (one origin), and Space 1 becomes a redirect. About
  1.5 days; Space 3 would still need B's path.
- **B. A front door on Space 2.** Space 2 becomes public with its own nginx in front of the gateway. The
  Space 1 and Space 3 UIs call it from the browser. About 1 day.
- **C. Keep the hop.** Back off and wait out each block (the UIs' back-off, #213 and #217).

**Decision: B.**
1. **nginx owns Space 2's port.** A supervisord program (`front-door.sh`) writes the `/gateway/` location with
   `gateway-proxy.sh` in its front-door mode, then runs nginx. The gateway (uvicorn) listens on
   `127.0.0.1:8000` only, so nothing outside the container reaches it except through nginx. Everything
   outside `/gateway/` answers 404, and `/gateway/docs`, `/gateway/redoc` and `/gateway/openapi.json` are not
   served (the contract is in `contracts/`).
2. **The ingress rules are unchanged.** Space 2 keeps `AIVA_INGRESS=platform-proxy`, not `direct` with a
   loopback trusted proxy, so the policy source of a visitor's run stays null and NET-001 still denies
   network-gated applications from the internet. nginx sets `X-AiVa-Proxy-Secret`, which now proves "came
   through Space 2's nginx" (defence in depth). It sets `X-AiVa-Client-IP` from the rightmost
   `X-Forwarded-For` entry, which only keys the HMAC rate-limit reference. The header whitelist
   (`proxy_pass_request_headers off`) and the `hf_` credential filter move over unchanged. The front door
   sends no HF token upstream: the loopback needs none.
3. **The per-visitor limits stay at the front door:** 5 requests/s with a burst of 20, and 10 concurrent,
   keyed on the visitor's address. The proxy's own 429 is the JSON envelope `RATE_LIMITED` with
   `Retry-After` (deploy PR #215).
4. **CORS is answered in one place:** nginx, for exactly the two UI origins (`AIVA_PROXY_CORS_ORIGINS`). The
   gateway's own `AIVA_CORS_ORIGINS` stays `[]`, and upstream `Access-Control-*` headers are hidden, so a
   response carries exactly one `Access-Control-Allow-Origin`.
   - Allowed request headers: `X-AiVa-Authorization`, `Content-Type`, `Idempotency-Key`. Not
     `Authorization`: the UIs send the AiVa token only in `X-AiVa-Authorization`, so HF's edge never sees an
     AiVa JWT in `Authorization` on a public Space (ADR-0019's rule).
   - Exposed: `Retry-After`, `RateLimit`, `RateLimit-Policy`, `Idempotent-Replayed`, so the UIs' back-off can
     read HF's reset cross-origin.
   - `Access-Control-Allow-Credentials` is never sent, and `Set-Cookie` stays hidden.
5. **The UIs call `https://<owner>-gct-rlcd-gateway.hf.space/gateway` directly** (`AIVA_GATEWAY_URL`). A waking or
   paused Space, or a network error, is shown as "starting" or "paused", never as an error (the Control
   Center's and the Context Lab's clients, merged 2026-10-22).
6. **Space 1 needs no secrets.** Space 3 keeps `HF_TOKEN` for its Hub registry and drops
   `AIVA_PROXY_SECRET`. The hop stays in both images, unconfigured (503), so a rollback is configuration only.

**CORS is not a security boundary on HF** (Govern's wording). A probe Space verified this on 2026-10-22,
with curl and with Chromium on the real Space 1 page and on example.com:
- HF's edge answers every preflight itself (200) and allows any origin and any requested header. The app
  never sees an `OPTIONS` request.
- HF adds `Access-Control-Allow-Origin: <origin>` to a response whenever the app sends none, and replaces the
  exposed headers with `*`. A page on any site can therefore read a public Space's answers.
- HF never adds `Access-Control-Allow-Credentials`.

AiVa's guarantees rest instead on:
- credentials sent only in request headers, never in cookies or URLs, so a third-party page can make only
  anonymous calls;
- the proxy proof;
- a null policy source for proxied requests;
- per-visitor limits, with the tenant-wide public caps (`public_sessions_per_tenant` and the `public-demo`
  daily budget, ADR-0019) as the hard bound on spend.

nginx's allowlist still sets the right headers for the two origins, and would matter if HF stopped
reflecting origins.

**Verified on HF (probe Space, 2026-10-22).**
- `X-Forwarded-For` on a public Space keeps a client-sent value and appends exactly one hop, the client, on
  the right. A plain request arrives with one entry. No internal load-balancer hop appears, so the rightmost
  entry is the visitor, as on Space 1 on 2026-10-15.
- **A restart has no gap.** HF keeps the old container serving until the new one runs. While the gateway
  boots behind nginx, the front door answers `GATEWAY_STARTING` (503, `Retry-After: 30`).
- **A paused Space answers HF's own 503 page** (`text/html`), with `Access-Control-Allow-Origin` added by HF,
  so the browser reads it rather than seeing an opaque error. A sleeping Space could not be forced on
  cpu-basic; the UIs treat an opaque network error as "starting" too.

**Tested invariants** (`deploy/spaces/gateway/smoke_test.sh` in CI, and `tests/e2e` against the deployed
Spaces):
- the gateway listens on 127.0.0.1 only; another host reaches the front door but never the gateway's port;
- a request that reaches the gateway without the proxy proof gets 401 `PROXY_PROOF_REQUIRED`;
- client-sent `X-Forwarded-For`, `X-AiVa-Client-IP` and `X-AiVa-Proxy-Secret` are overwritten. The rightmost
  entry keys a public session as an attested `cli_` reference, a spoofed client IP changes nothing, and two
  visitors get two references;
- an AiVa token in a cookie or in the query string is no credential (401); only the header counts;
- exactly one `Access-Control-Allow-Origin`, also on the proxy's own 429; an unlisted origin gets none from
  nginx;
- `/docs`, `/redoc` and `/openapi.json` are not served.

**Consequences.**
- No visitor request crosses a Space-to-Space hop. Each visitor counts against HF's limits from their own
  address, and nothing is shared across visitors.
- **Space 2's repository becomes public, and the GitHub repository is private.** The public Space now publishes
  what its image is built from: the gateway source, the policies, `config/`, the synthetic knowledge base and the
  replay recordings (`push_spaces.py`'s gateway `include`). Space 1 already publishes the Control Center's source
  the same way. The tree is staged from git-tracked files only and passes the secret check (no key or env
  files, no token-shaped strings), and all of it is synthetic (LLD §17).
- Space 1 loses `HF_TOKEN` and `AIVA_PROXY_SECRET`; Space 3 loses `AIVA_PROXY_SECRET`. The owner deletes them
  only after the cutover is verified (runbook §7).
- nginx starts before the gateway, so callers get a retryable answer during boot and restore instead of a
  connection error.
- ADR-0007's 2026-10-15 amendment and ADR-0019 get one-paragraph amendments pointing here. Their trust
  rules are unchanged.
