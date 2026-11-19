# ADR-0027: Application keys and model switches from the admin console

**Status.** Accepted. The owner chose both features on 2026-10-26, with Optimize's OpenAI-compatible API
(ADR-0026). The options read:
- "Platform or tenant admins can create and revoke application keys (aiva_…) on the Applications page. The
  key is shown once, only a hash is stored, and every create or revoke is written to the audit log. Today
  keys can only be made with the CLI inside the gateway container, so the live gateway has none."
- "Turn a model off or back on per application from the admin console, saved as a policy version through
  the existing draft → test → publish flow. Adding new models and setting prices stay in config/ with a pull
  request, because each pin and price needs its source and date."

**Owners.** Govern: this ADR, the key routes and permissions, the switch drafts and their checks. Control
Center UI: the Applications page's keys and the Model Allowlist's switches.

## 1. Application keys

**Context.** An application authenticates with `aiva_<key_id>.<secret>`, and only an HMAC of the secret is
stored (ADR-0007). Until now a key could only be made with `aiva-admin create-credential` inside the
container. A key can spend real money in live mode, and the public demo's admin sign-in sits behind a shared
passcode, so issuing keys from the console needs guardrails.

**Decision.**
- **Routes.**
  - `POST /v1/admin/applications/{application_id}/credentials` with `{label, expires_in_days}` issues a key.
    The response carries the full key **once**, with `Cache-Control: no-store`. AiVa keeps only the HMAC.
  - `GET /v1/admin/applications/{application_id}/credentials` lists metadata: key id, label, issuer, created,
    expires, last used and revoked, and whether the key is active. It never returns a secret.
  - `POST /v1/admin/applications/{application_id}/credentials/{key_id}/revoke` revokes a key. It stops
    authenticating at once. Revoking a revoked key changes nothing and writes no second audit row.
  - An unknown application, or a key of another application, is 404.
- **Who.** The owner's option names platform and tenant administrators.
  - `credentials:manage` (issue, revoke): `platform_admin` and `tenant_admin` only.
  - `credentials:read`: administrators and `security_auditor` for every application, and an
    `application_owner` for its own applications.
  - Never the tester or the public visitor, and never an application credential (`granted` needs a user).
  - Authorization comes before the body: 401, then 403, then 422 (ADR-0007).
  - The key list carries `viewer_can_issue` (the caller manages keys and the application is below its
    limit) and `viewer_can_revoke`, so the console disables what the gateway would refuse.
  - `GET /v1/admin/applications` lists key metadata only to callers who may read that application's keys.
    Before, anyone with `policies:read` saw key ids and labels, the public visitor included.
- **Guardrails** (code constants, changed only through review):
  - **At most 5 active keys per application.** Active means neither revoked nor expired. Issuing locks the
    application's row first, so concurrent issues cannot pass the limit. A sixth is refused with 409
    `CREDENTIAL_LIMIT`.
  - **Every console key expires:** after 30 days by default, at most 90. A key made with the CLI keeps no
    expiry, as before.
  - `authenticate` refuses a revoked or expired key with the same answer as an unknown one.
  - The key's application, budgets and policy apply as for any application call. A reservation precedes any
    billable stage (Appendix B).
- **Records.**
  - `audit_log` gets `credential.create` and `credential.revoke`. The actor is the administrator's
    pseudonym; the details are the application, the label and the expiry. Never any part of the secret.
  - The key's row records its issuer (`created_by`, or `cli`), its revoker and its last use. Last use is
    written at most once an hour per key, so authenticating stays a read, and a used key does not change
    the database every minute (ADR-0024's backup uploads only on change).
  - No secret reaches a log, a span or any response but the one that issues it.
- **Restarts.** Keys live in `application_credential`, which ADR-0024's whole-database dump includes and its
  change check counts. A restored key authenticates only while `AIVA_CREDENTIAL_PEPPER` is the same Space
  secret; rotating the pepper invalidates every key.

**Using a key on the deployed gateway.** On Space 2 every request arrives through the front door, so an
application call's policy source is null (ADR-0019, ADR-0025). Every application's trusted networks are
private or test-console ranges, so `AIPEL-NET-001` denies the call. Section 3 records which applications
the owner let take calls from the internet; every other application's key authenticates on Space 2 but its
runs are blocked by AI policy.

## 2. Model switches per application

**Context.** An application's models are granted in `config/tenant.yaml`; policies can only narrow them
(ADR-0005). Turning a model off for an application should not need a pull request, but it must be a policy
change: versioned, tested and published like any other.

**Decision.**
- **A switch is a policy document.** Each application with a model switched off has one document,
  `GCT-SWITCH-<application_id>`. It matches that application and sets `allowed_models` to its granted models
  minus the ones switched off. The policy engine already intersects every matched document's allowlist, so
  the document can only narrow.
- **Switching back on** writes a new version: the document without that model, or without the document when
  nothing is off. The allowlist is built from the registry grants, so it never widens past them, and a
  model the application is not granted is refused.
- **The flow is unchanged.** A helper, `POST /v1/admin/policies/model-switch`
  (`{application_id, model_id, enabled, notes}`, `policies:write`), creates the draft from the published
  version. It then goes through the existing test and publish routes:
  - Switching a model off only narrows, so it is not high-impact.
  - Switching one back on relaxes a policy, so it is high-impact: a different administrator publishes it,
    with an approval note (separation of duties).
- **The sovereign rule.** When the sovereign model is off for an application, that application's
  confidential, restricted and sovereign-only requests have no eligible model. `GCT-CONF-001` allows only the
  sovereign model, and the switch removes it, so they are **blocked by AI policy (`AIPEL-ROUTE-001`), never
  rerouted** to another model (Appendix B). The helper's response simulates the draft for every
  classification on that application and says so explicitly, so the UI can warn before it is published.
- **The API** (built 2026-10-26):
  - **Who.** `POST /v1/admin/policies/model-switch` needs `policies:write` (administrators). It is authorized
    before the body is validated.
  - **Response.** 201 with:
    - the draft, as `PolicyVersionDetail` with `changes_from_published`;
    - `base_version`, `switched_off_models` and `high_impact`;
    - `checks`: one per classification from the application's floor up, with the allowed models before and
      after, `sovereign_path` and `blocked`;
    - `sovereign_blocked`, `sovereign_blocked_before` and ready-made `warnings`, so the console never
      composes rule text.
  - **Refusals.**
    - A model the application is not granted: 422 `MODEL_NOT_GRANTED`.
    - An unknown application: 404.
    - A switch to the state already published: 409 `MODEL_SWITCH_UNCHANGED`.
  - **Audit.** Each switch writes `policy.model_switch`, with the application, the model, the base version
    and the sovereign flags, beside the draft's `policy.draft`.
  - **Reads.** `GET /v1/admin/applications` gives each application's `switched_off_models`, and
    `GET /v1/admin/models` gives each model's `switched_off_application_ids`. Both are read from the published
    version. Both lists carry `viewer_can_switch`.
  - **Drafts are full versions.** A switch is built from the published version, like every draft. Publishing
    an older draft after a newer one replaces the newer one's changes. The console builds each switch on the
    version it shows.
- **Out of scope.** Adding a model, pinning a revision or changing a price stays in `config/` through a pull
  request, because each pin and price needs its source and date.

## 3. Internet callers (amendment, 2026-10-26)

**Context.** Section 1's keys are blocked on Space 2 by `AIPEL-NET-001` (above). The owner was asked: "Which
applications should accept key calls from the internet? All other policy still applies: classification,
sovereignty, DLP, model allowlist and budgets." The owner chose **"Sandbox + coding & knowledge apps"**:
"Same sandbox app, and also let gct-coding-assistant and gct-knowledge-portal accept key calls from the
internet. More realistic, but those apps' bigger budgets would be reachable with a key." The sandbox is "one
application just for SDK and API use, e.g. gct-api-sandbox: internal classification floor, its own model
grants, and a hard budget cap of $5 a month, so a leaked or demo key can't run up spend."

**Decision.**
- **A grant flag, not a network.** An application in the registry may set `internet_callers: true`
  (`config/tenant.yaml`, carried in the policy bundle). Then a call made with one of its keys passes
  `AIPEL-NET-001` from any source, the null source behind the front door included, and reports
  `matched_network_id: "internet"`.
  - It is not a `0.0.0.0/0` trusted network. A tenant network that contains every address would contain every
    visitor's address too, so the front door's client attestation (ADR-0025), which refuses an address inside
    a tenant network, would treat every visitor as unattested.
  - Trusted networks stay private (RFC 1918) and documentation (RFC 5737) ranges only.
- **In the API.** `GET /v1/admin/applications` carries it as `internet_callers` on each application, so the
  console reads it and never hard-codes application ids or infers it from a CIDR. `trusted_networks` never
  contains an internet entry. On a run, `matched_network_id: "internet"` is a documented value, not a network
  id; a caller inside one of the application's networks is reported with that network's id instead.
- **Only keys.** The flag admits an application principal with an authenticated key. A user, and any
  simulated identity (the Test Console, the public visitor), still needs a source in one of the application's
  trusted networks.
- **Nothing else relaxes.** Classification, the sovereign path (`GCT-CONF-001`, Appendix B), DLP, task and
  model grants, tool grants, approvals and budgets apply to an internet call exactly as to any other.
- **The three applications.**
  - `gct-api-sandbox` (new, IT & Engineering): internal floor; tasks qa, summarization, rag, generation,
    analysis and coding; the four upstream models; no tools; `net-test-console` for Test Console runs; and an
    application budget of $5.00 a month that blocks on exhaustion.
  - `gct-coding-assistant` and `gct-knowledge-portal` keep their grants and budgets, as the owner accepted.
  - Every other application keeps `AIPEL-NET-001` as before.
- **Changing it is high-impact.** Setting the flag on an application widens access, so a policy version that
  does it needs a second administrator to publish (`govern/impact.py`). The invariant tests pin the set of
  internet applications to the owner's three and the sandbox's cap; changing either needs an owner decision.
- **The in-house GPU endpoints.** The sandbox's cap bounds API spend only. So internet key calls never wake a
  sleeping in-house endpoint, and their estimated GPU cost draws on its own $5 monthly `internet_gpu` budget
  (owner decision 2026-10-26; ADR-0008 amendment of that date).

**Consequences.**
- The deployed gateway can issue its own keys, and an administrator can retire one at once.
- A leaked console key is bounded: by its expiry, by its application's budgets and policy, and by the five
  active keys per application. Only the three internet applications' keys work from outside a trusted
  network; the sandbox's are bounded by $5 a month, the coding and knowledge apps' by their own budgets.
- Model availability per application is visible and reversible in the policy history, with the same audit
  and separation of duties as every other policy change.
