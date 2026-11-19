# ADR-0010: Control Center sign-in and role-aware navigation

**Context.** The Control Center needs signed-in users with roles (LLD §21.8, §22.5) before single sign-on
exists. ADR-0007 defines user JWTs (HS256 for the POC, OIDC/JWKS later), but the browser had no way to
obtain one or to learn the caller's roles.

**Decision.**
- `GET /v1/auth/me` returns the authenticated caller: kind, tenant, display name, roles and the
  permissions those roles grant (computed by the gateway from `auth/rbac.py`). The UI builds navigation
  from `permissions` only, never from token claims, so swapping the token issuer for OIDC changes nothing
  in the UI beyond how the token is obtained.
- Demo sign-in: `GET /v1/auth/dev/users` lists the seeded demo users and `POST /v1/auth/dev/token` mints a
  one-hour user JWT for one of them, without a password. Both return `404 DEV_SIGN_IN_DISABLED` unless the
  gateway runs with `AIVA_DEV_SIGN_IN=true`; the default is off. Enable it only for local and private
  synthetic demo gateways, never on a public Space. The gateway refuses to start with it when
  `AIVA_ENVIRONMENT` is `prod` or `production`, and the endpoints re-check that at request time.
- The Control Center keeps the token in `sessionStorage` (per tab, cleared when the tab closes), signs out
  on `401`, and hides navigation entries the caller lacks the permission for. Hidden entries are a
  convenience: the gateway authorizes every request and the UI shows an explicit "no access" state when a
  view is opened directly.

**Consequences.** A gateway with dev sign-in enabled lets anyone who can reach it act as any demo user,
administrators included, which is acceptable only for synthetic data on a non-public gateway. Production
deployments leave it off and add an OIDC token source in the Control Center
(`web/apps/control-center/src/auth/`).

**Amendment (2026-10-15): deployed gateways (ADR-0019).** The Control Center is a public Space. This
amendment replaces the demo sign-in above on every deployed gateway.
- **Dev sign-in, local only.** Password-less dev sign-in (`/v1/auth/dev/*`) is allowed only when
  `AIVA_ENVIRONMENT` is `local` or `test` (`test` is the test suite's environment and is never deployed).
  The gateway refuses to start with `AIVA_DEV_SIGN_IN=true` anywhere else, and the endpoints check again at
  request time. This closes the earlier gap where a `demo` environment allowed it.
- **Passcode sign-in.** The owner and demo users sign in with `POST /v1/auth/demo/token {user_id,
  passcode}`, which mints the same one-hour JWT.
  - It is enabled by `AIVA_DEMO_PASSCODE` (at least 16 characters, or the gateway refuses to start).
  - The passcode is compared in constant time, on digests.
  - Five failures from one source within 15 minutes lock that source out for 15 minutes. At most 30
    attempts a minute are verified tenant-wide. Both answer 429 with `Retry-After`.
  - Without Redis no attempt is verified (503).
  - Each verified attempt is audited as `auth.demo_sign_in` or `auth.demo_sign_in_refused`, by pseudonym
    and client reference, never with the passcode.
  - `GET /v1/auth/demo/users` lists the users it offers.
- **Public demo sessions.** A browser without a sign-in calls `POST /v1/auth/public/session`. It gets a
  two-hour token for the seeded `u-public-visitor` (role `public_visitor`, department `public-demo`).
  - The token carries a random session id, and the visitor's pseudonym is an HMAC over that id. So each
    browser session is its own visitor: its runs, workflows, artifacts and approvals are its own.
  - Sessions are issued only to a request with a valid proxy proof (ADR-0007 amendment), or in the local
    and test environments.
  - Issuance is limited to 5 sessions per source an hour and 200 an hour tenant-wide (429), fails closed
    without Redis (503), and is audited as `auth.public_session`.
  - Only this endpoint issues visitor tokens: a visitor JWT without a session id, or any other user's JWT
    with one, is refused. The visitor is never listed for either sign-in.
- **The visitor's permissions.** The role grants `chat:test` (console chat, agent workflow and artifact
  runs under a simulated identity; policy rule AIPEL-ID-003 accepts it like the tester), the new
  `chat:live`, `runs:read`, `reports:read`, `policies:read` and `catalog:read`.
  - Run reads cover its own runs and application-initiated ones.
  - Reports are tenant-wide but without the user dimension: no user filter or grouping, and principals
    are shown as `hidden`.
  - It has no approval, policy, budget, export, identity-resolution, evaluation or admin permission. A
    test walks every non-GET operation in the contract.
  - `chat:live` only allows a request to ask for a live run (`auth.run_identity.authorize_execution_mode`).
    FinOps' per-visitor quota and the `public-demo` budget still decide whether it runs.
- **Discovery.** `GET /v1/auth/sign-in-options` tells the UI which of public, passcode and dev this
  request may use.
- **Configuration errors.** Settings validation errors never print their input, because it holds every
  secret read from the environment (`hide_input_in_errors`).
