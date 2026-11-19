# ADR-0005: Policy engine — OPA, restrict-only policies, two-phase evaluation

**Context.** LLD §7 makes AI-PEL the non-bypassable authority, with deny-overrides, sovereign-only
overriding preferences, intersecting mandatory constraints and conflicts resolving to deny.

**Decision.**
- OPA (Rego v1) runs as a sidecar on localhost; the gateway queries `data.aiva.decision`. Any OPA error
  fails closed (`POLICY_ENGINE_UNAVAILABLE`).
- Grants come only from the authenticated application registry (allowed tasks, CIDRs, models).
  Policies can only restrict: matched policies intersect allowed models, add `cim: bypass`, deny external
  providers, force `sovereign-direct`, require approval, or deny. Because policies only restrict, matching
  on user-supplied tags (hints) can never widen access.
- Effective classification = most restrictive of declared, DLP-derived and RLCD-predicted; the gateway
  computes it and the policy re-checks it is not below the declared or DLP value.
- Stages: `request` (after ingress), `egress` (before every CIM/upstream call), `tool` (before every tool
  call, P3).
- Published bundles (policies + applications + models) are stored in `policy_version` with a SHA-256
  hash; every decision records version and hash. Rollback re-publishes an earlier version.

**Consequences.** RLCD can only choose within the allowed set and is re-checked at egress, so a faulty
router cannot route outside policy.
