# ADR-0007: Identity, simulated test identities and trusted ingress

**Decision.**
- Applications authenticate with credentials `aiva_<key_id>.<secret>`; only an HMAC-SHA256 of the secret
  (with a server-side pepper) is stored.
- Users authenticate with JWTs (HS256 issuer for the POC; OIDC/JWKS configurable later). Roles: platform
  admin, tenant admin, finance analyst, department manager, security auditor, application owner, tester.
- The Test Console runs requests as a signed-in **tester** who selects an application profile and,
  optionally, a simulated source IP. Such runs are recorded with `identity_mode=simulated` /
  `source_mode=simulated`; simulation is refused for any other role or when disabled by configuration.
- The client IP is the TCP peer unless the peer is a configured trusted proxy, in which case
  `X-Forwarded-For` is walked right-to-left skipping trusted proxies. Untrusted `X-Forwarded-For` is ignored.
- User identifiers are pseudonymised (HMAC) in run and reporting tables.

**Consequences.** On HF Spaces (no trusted proxy configured) CIDR policy demonstrations use simulated
ingress and are labelled as such, as LLD §5 requires.

**Amendment (2026-10-15): platform-proxy ingress (ADR-0019).** On HF Spaces the gateway (Space 2) is
private, and the public Control Center's proxy calls it server-side. It adds to the decision above; direct
deployments are unchanged.
- **Ingress mode.** `AIVA_INGRESS=direct` (the default) keeps the rules above. `AIVA_INGRESS=platform-proxy`
  is set in the Space 2 image and needs `AIVA_PROXY_SECRET` (at least 32 characters), shared with the
  proxies.
- **Credential channel.** Behind the platform proxy, `Authorization` carries the platform's token (the HF
  token). The gateway never reads that header there, so the token is never parsed, logged, stored or
  echoed. The AiVa credential travels in `X-AiVa-Authorization: Bearer …` (an application credential or a
  user JWT). In direct mode `X-AiVa-Authorization` wins when present, and `Authorization` otherwise works as
  before. A test sends a canary token through every path and finds it in no response, log, table or Redis
  key (`tests/invariants/test_platform_token_is_never_kept.py`).
- **Proxy proof.** A request is trusted as proxied only if `X-AiVa-Proxy-Secret` matches the secret. The
  comparison is constant-time, on digests.
- **Proof required.** In `platform-proxy` mode, a request without a valid proof is refused with 401
  `PROXY_PROOF_REQUIRED` before routing, authenticated or not (`api/proxy_guard.py`). So an HF token alone,
  which reaches the private Space, does not reach the API.
  - The container's own probes are exempt: `/health/live` (the image's HEALTHCHECK) and `/health/ready`
    (the watchdog and the smoke test).
  - A direct call to the gateway, such as the owner's scripts, the replay recording run or the deployed
    smoke tests, sends `X-AiVa-Proxy-Secret` too, or goes through a Space 1/3 proxy.
- **Attested address.** The proxy sets `X-AiVa-Client-IP` from the hop HF's edge appended: the rightmost
  `X-Forwarded-For` entry, which Deploy verified on a public Space. It is used only with a valid proof, and
  only if it is one public address outside every tenant network. A private, reserved or documentation
  address, or one inside a tenant network, attests nothing, so spoofing a campus address gains nothing.
  `X-Real-IP` is never read: HF passes a client-sent value through unchanged.
- **What the run sees.** Behind the platform proxy the socket peer is HF's own address and is never a
  client address. `X-Forwarded-For` is not walked there.
  - The run's observed source is null: a visitor is on the internet, which no trusted network contains, so
    network-gated applications are denied (AIPEL-NET-001).
  - CIDR demonstrations keep using the Test Console's simulated source IP, labelled `simulated`, as above.
  - The attested address only keys the sign-in and public-session rate limits, through an HMAC client
    reference (`cli_…`, with the pseudonym key). The raw address is never recorded.

**Amendment (2026-10-18): run routes authorize before they validate the body.** A user without `chat:test`
used to get a 422 for an invalid body on `/v1/ai/chat`, because the check ran inside the handler, after
FastAPI had validated the body. With a valid body the same user got a 403. The routes that start a run or
dry-run one now take `RunCallerDep`, which applies `authorize_run_caller` before the body is read:
`/v1/ai/chat`, `POST /v1/artifacts`, `POST /v1/agents/workflows`, the workflow resume route and
`POST /v1/policy/evaluate`.

- The order is 401 without credentials, then 403 for a user who may not run, and only then 422.
- `authorize_run_caller` is the same predicate `resolve_run_identity` applies, so nothing that could run
  before is refused now. An application credential passes, and so does a user granted `chat:test`: testers,
  public visitors (ADR-0019) and administrators.
- Malformed JSON is still a 422 before authentication on every route, because FastAPI parses the body
  before it resolves dependencies. Permission checks inside other handlers (`require(...)`) still follow
  body validation.

**Amendment (2026-10-25): the proxy is Space 2's own front door (ADR-0025).** The proxy described in the
2026-10-15 amendment now runs in front of the gateway in the same container: nginx on Space 2's port, the
gateway on its loopback, and Space 2 public. Spaces 1 and 3 no longer proxy, and no HF token travels in
`Authorization` to the gateway. The trust rules of the 2026-10-15 amendment are unchanged:
- `platform-proxy` ingress;
- the proxy proof, which now means "came through Space 2's nginx";
- the rightmost `X-Forwarded-For` entry as the only attested address, re-verified on a public Space on
  2026-10-22;
- a null policy source for proxied requests;
- the attested address used only for the HMAC rate-limit reference.
