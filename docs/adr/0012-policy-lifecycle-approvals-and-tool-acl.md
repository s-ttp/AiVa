# ADR-0012: Policy lifecycle, Approval Queue and tool ACL

**Context.** LLD §7 and §22.5–22.6 require versioned policy publication with simulation, review and
rollback, an approval path for `approval_required` decisions, and a tool ACL "before every tool call".
ADR-0005 fixed the engine (OPA, restrict-only policies, hashed bundles). This ADR records how the
lifecycle, approvals and tool checks work, within the LLD invariants.

**Decision — lifecycle** (`govern/lifecycle.py`, `/v1/admin/policies*`).
- A draft is a complete bundle (policies, application grants, models, tools, networks) stored with status
  `draft`, validated for cross-references (unknown models, applications, tools, invalid CIDRs, duplicate
  IDs). It is not enforced.
- Simulation evaluates any version (draft, published, archived) through `data.aiva.simulate`, which
  replaces `data.aiva_bundle` with the candidate for that query only. Cases are either a full policy input
  or a *sample* request, which the gateway classifies with the production DLP like a real request.
- Publishing loads exactly the simulated bundle, so its hash is unchanged. Rollback re-publishes an
  archived version as-is: decisions then report that version and its original hash ("re-pin a hash").
- Every publish computes the changes from the published version. A change is high-impact when it relaxes
  a constraint (policy removed, deny or approval dropped, allowlist widened, grant, network or tool
  widened, simulated ingress enabled) or blocks every request. A high-impact draft must be published by a
  different administrator than its author (`403 SEPARATION_OF_DUTIES`). An approval note is required for
  every publish and rollback.
- Lifecycle changes are serialised per tenant (Postgres advisory lock). OPA is loaded last inside the
  transaction; if OPA fails, nothing is published (`503 POLICY_ENGINE_UNAVAILABLE`).
- Draft, simulate, publish and rollback each write an audit record (version, hash, approval note, changes).

**Decision — Approval Queue** (`govern/approvals.py`, `/v1/admin/approvals*`, `/v1/approvals/{id}`).
- A request- or tool-stage decision of `approval_required` creates an approval request holding policy
  facts only: the OPA input (identity, source, hints, classification levels, DLP finding types). Prompt and
  evidence text are never stored; the gateway does not resume runs server-side.
- Approving re-evaluates the stored facts against the **current** policy version. If it now denies, the
  request is closed (`closed_by_policy`); otherwise the approval records grants `(rule_id, tool_id?)`.
  If OPA is down, nothing changes.
- The caller resumes by resubmitting the identical request with `approval_id`. The approval is verified
  (status, expiry, tenant, application, requester, request hash excluding `approval_id`) and consumed
  atomically before the run. The run evaluates policy again with the grants in `input.approvals`; grants
  satisfy approval rules only and never override a deny. A grant with a `tool_id` applies only to calls of
  that tool. If the resumed run hits a new approval rule, the new request carries the consumed grants, so
  one `approval_id` always suffices. A claimed approval is returned only when the run fails on a retryable
  dependency error.
- Approvals expire (`AIVA_APPROVAL_TTL_SECONDS`, default 24 h). Approvers are tenant/platform admins or the
  application owner; a requester cannot approve their own request. Auditors, department managers (own
  department) and owners can read the queue; requesters can read their own request. Approver identities
  are pseudonyms. Request, approve/deny/close and consume are audited.
- Budget reservations denied with `on_exhaustion: approval_required` (FinOps, ADR-0008) become approval
  requests of kind `budget` with `approved_amount_micros` = the denied worst-case envelope (estimated),
  decided by administrators or finance analysts. On resume the verified approval reaches the quota stage
  as `ReservationRequest.approval_id`. FinOps enforces the override (ADR-0014): it verifies the approval
  row and admits that run past the denying caps by at most `approved_amount_micros`, single-use, audited
  and alerted. Govern only records and decides budget approvals and claims them on resume like any other
  approval; it does not track the override. Until FinOps' follow-up lands, budget approvals are visible
  and decidable but do not unlock spend: the resumed run is denied again and raises a new budget approval.

- Approval responses carry per-caller flags computed by the gateway with the same checks `decide`
  enforces: `requested_by_viewer` and `viewer_can_decide` (allowed for this kind and application, not
  the requester, still pending). Policy version detail carries `authored_by_viewer` for the
  separation-of-duties rule. The UI uses them to disable actions; the gateway still enforces every rule.

**Decision — tool ACL** (`tool` stage, `Orchestrator.authorize_tool_call`).
- `config/tools.yaml` is the tool registry: `access` (`read`/`write` = side effects) and `egress`
  (`internal`/`external`). Callers only name the tool; the registry decides its risk. Applications are
  granted tools in `tenant.yaml` (`allowed_tools`); policies can only restrict, matching on `tool_any`,
  `tool_access_any`, `tool_egress_any` and `evidence_injection_detected`.
- Built-in rules: unregistered (`AIPEL-TOOL-002`), disabled (`-003`) or ungranted (`-004`) tools are
  denied; external tools are denied when external providers are (`-005`); confidential, restricted and
  sovereign-only runs never reach an external tool (`AIPEL-INV-002`, independent of policy documents).
  Request-stage checks (identity, network, task, classification) apply at the tool stage too.
- Policies: GCT-TOOL-005 requires approval for external tools with side effects; GCT-TOOL-006 denies tool
  side effects when retrieved evidence carried injection indicators (LLD §17).
- Every tool decision is a policy event with its `tool_id`. The agentic workflow (Scenarios & Artifacts)
  must call `authorize_tool_call` before each tool call and proceed only on `allow`.

**Consequences.** Policy editors can test drafts with realistic requests before they take effect, and
roll back by hash. Approvals add no stored prompt text. Tool safety rests on the registry being accurate:
registering a write tool as `read` would bypass GCT-TOOL-005, so registry changes are high-impact in the
publish review. OPA is a per-instance sidecar: with several gateway replicas, a publish reaches the
others at startup or on their readiness check (`ensure_opa_bundle`), a known POC limitation for P5.

**Amendment (2026-10-15): recorded tool calls.** Scenario 6 resumed a held tool call (`notification.send`)
by re-running the model step that authorized it, because a tool approval was bound only to that run's
request hash. FinOps found that this re-run can meet an exhausted workflow budget and consume the
approval without reaching the tool, and in live mode the re-run can produce different output from what
the approver saw. Tool approvals can now be bound to the exact call:
- `authorize_tool_call(ctx, tool_id, arguments=…)` puts `arguments_sha256` (SHA-256 of the canonical JSON
  arguments) into the tool-stage `PolicyTool`. Policies do not match on it. The approval request stores
  it (`approval_request.arguments_sha256`, migration `gov_0002`) and shows it in the Approval Queue.
- `govern.approvals.authorize_recorded_tool_call` executes nothing itself. It requires an approved,
  unexpired tool approval for the same tenant, application and principal (otherwise 404
  `APPROVAL_NOT_FOUND`, as for resubmissions) and for the same tool and arguments hash (otherwise 409
  `APPROVAL_REQUEST_MISMATCH`). An approval recorded without arguments never matches.
- It evaluates the recorded tool-stage facts (classification levels, DLP and evidence-injection facts,
  the tool) against the current published policy, with the approval's grants and with the current
  caller's identity and source. A newer, stricter policy or an untrusted network therefore refuses the
  call. There is no model run and no budget reservation.
- The decision is stored as the next `tool` policy event of the run the call was recorded on, and
  audited (`approval.consume`, or `approval.recheck_refused`). The approval is consumed only on `allow`,
  under a row lock, so exactly one concurrent caller wins. If OPA is unavailable it raises 503 before any
  state change.
- Releasing a claimed approval after a retryable failure is now audited (`approval.release`).
