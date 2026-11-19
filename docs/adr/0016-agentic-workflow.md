# ADR-0016: Agentic workflow — governed steps, per-tool policy, one workflow budget

**Context.** LLD §15 #6 asks for a multi-step service incident with per-tool policy and cumulative
budgets. §7 requires a tool ACL "before every tool call", §12 a workflow envelope that multi-step agents
must respect, and §17 that retrieved text is untrusted evidence and that dependencies fail closed. Govern
built the `tool` policy stage (`Orchestrator.authorize_tool_call`, ADR-0012) and FinOps the `workflow`
budget scope (ADR-0008). The Scenarios & Artifacts brief also rules out autonomous punitive action.

**Decision.**
- **Runbook-driven agent** (`agents/service_incident.py`, definition `service-incident-v1`). The workflow
  definition, not model text, chooses which tool runs and with which arguments. A model's output can
  therefore never trigger a tool, which removes the "injected text calls a tool" path by construction.
  The models plan, correlate and draft. Steps: `plan` → `incident.lookup`, `network.site_status`,
  `kb.search` → `analyse` → `ticket.create` (and `subscriber.suspend` for human review when a line is
  implicated) → `draft-notice` → `notification.draft` → `notification.send`.
- **Every model step is a governed run.** Each step goes through `Orchestrator.run`, so policy, cache,
  RLCD, reservation, egress checks, assurance and metering all apply. Each step carries the workflow ID,
  so every reservation includes the `workflow` scope `<application_id>/<workflow_id>`. The workflow
  budget therefore bounds the sum of all steps (cumulative; default template in `tenant.yaml`).
- **Every tool call is authorized first.** `authorize_tool_call` runs on the run of the model step that
  precedes the call, and the tool runs only on `allow`. Every write tool is authorized by a step whose
  evidence includes all earlier tool output. Injected tool output therefore reaches policy as
  `evidence_injection_detected`, and GCT-TOOL-006 denies the side effects. The decision is stored as a
  `policy_event` with `tool_id`, and approval requests (kind `tool`) are created as for any run.
- **Tool output is evidence.** It reaches later steps only as `evidence` items with stable IDs
  (`<incident>/<tool>`). Each item is wrapped by `evidence_block` and DLP- and injection-scanned. DLP
  findings in tool output raise the step's classification: an MSISDN in an incident record sends the
  following steps down the sovereign direct path, and external tools are then denied (AIPEL-INV-002).
- **No autonomous punitive action.** `PUNITIVE_TOOLS` (`subscriber.suspend`) are policy-checked and
  recorded as `human_review`, and are never executed. This holds even if policy allowed them: the
  synthetic system refuses them as a second line of defence. External sends with side effects stop at
  `approval_required` (GCT-TOOL-005).
- **Stop on the first incomplete step.** A blocked, failed or over-budget model step ends the workflow
  (`blocked`, `failed`, `budget_exhausted`). So does a tool stage that cannot decide (OPA down). Nothing
  after it runs. The sovereign path never falls back (LLD §17).
- **Evidence is never trimmed.** Every later step carries all earlier tool output. If it exceeds the
  request limits, the workflow stops explicitly: `EVIDENCE_LIMIT_EXCEEDED` (an evidence item over the
  contract size) or `DLP_SCAN_LIMIT_EXCEEDED` (more text than DLP scans), both 413, before any step runs
  on partial evidence.
- **Each tool runs at most once per workflow.** Tool output is kept as evidence under
  `<incident>/<tool_id>`, and `Workflow.executed` records tool IDs, so a definition lists each tool once
  (asserted by a test). A definition that called a tool twice would first need per-call evidence IDs, or
  the later output would replace the earlier one.
- **A failed tool call is never retried.** If the synthetic system fails on an allowed call, the step is
  recorded as `failed` with `executed: false`, and the workflow ends `failed` with
  `502 TOOL_EXECUTION_FAILED` (`retryable: false`). The side effect may have happened, so AiVa does not
  try again.
- **Pause and resume on approval.** A step held for approval pauses the workflow at `approval_required`
  with one pending approval. That covers a model step held by request policy or by a budget with
  `on_exhaustion: approval_required`, and a tool call held by GCT-TOOL-005.
  - **Resubmission.** `POST /v1/agents/workflows/{id}/resume` resubmits the paused phase's model step
    byte-identical with that approval: same prompt, same evidence, same workflow ID, so the request hash
    the approval is bound to matches.
  - **Claim.** Govern's `claim_approval` consumes the approval for that step's new run before it runs
    (`ctx.approval`, so the reservation carries the approval ID). Policy is evaluated again, and an
    approval never overrides a deny. A budget approval lifts the cap for that one reservation only
    (ADR-0014). The paused tool and the rest of the definition then continue, and any later model step
    meets the cap again.
  - **Tool approvals** are the exception: they execute the recorded call (see "Recorded tool calls").
  - **Where the paused evidence lives.** Between pause and resume, the paused step's evidence stays in
    Redis until the approval expires. That evidence is synthetic tool output and earlier model text, the
    same exposure as the 24-hour idempotency replay store. It is never kept in Postgres.
  - **Integrity check.** On resume every item is checked against the output hash stored in Postgres when
    it was produced; a mismatch is `409 WORKFLOW_PAUSE_INVALID` and runs nothing.
  - **Resume errors**, all 409, all running no step:
    - Redis lost the pause: `WORKFLOW_NOT_PAUSED`.
    - Another workflow's approval: `APPROVAL_REQUEST_MISMATCH`.
    - An approval that is pending, expired, used, or claimed by a concurrent resume:
      `APPROVAL_NOT_USABLE`. Exactly one concurrent resume wins.
    - FinOps' single-use backstop: `BUDGET_APPROVAL_USED`.
  - **Retries and access.** `Idempotency-Key` on resume replays the first result. Only the principal that
    ran the workflow can resume it.
- **Budget view.** `WorkflowBudgetOut` is FinOps' own readout of the workflow bucket
  (`finops.workflows.workflow_budget`): `remaining = max(0, cap - committed)`, since an approved exception
  can take committed past the cap. Its provenance is the least trustworthy label among the bucket and
  the ledger charges of every reservation that drew on it: `simulated` in simulated mode.
- **Storage.** `agent_workflow` and `agent_step` (migration `scn_0001`) hold IDs, statuses, policy
  actions, rule IDs, approval IDs and output hashes, never tool or model text. Text appears only in the
  POST response. Every tool decision and every workflow is written to the append-only audit log. Read
  scope matches run drilldown (LLD §21.8); unreadable workflows return 404.
- **Synthetic systems.** The ITSM, NOC, runbook and CRM data live in `agents/data/service_incident.yaml`.
  It is deterministic, so prompts built from tool output are reproducible and replayable. Tool calls are
  internal and unpriced. Model spend is what the workflow budget meters.

- **Tool output inside a phase is screened before a write.** Each tool is authorized on the run of its
  phase's model step, which never saw output from earlier tools of the same phase. Before a write tool,
  any evidence the authorizing run did not see is DLP-scanned. If it carries injection indicators or would
  raise the classification, the workflow stops as `blocked` (`TOOL_EVIDENCE_NOT_SCREENED`) without
  calling the tool.
- **Unused approvals are returned.** A resumed step can stop before it holds a reservation or calls a
  model, for example at the quota stage. The approval it claimed is then released back to `approved`,
  as on a retryable failure.

**Recorded tool calls (follow-up "D2").** A tool-approval resume does not re-run the model step. The
pause keeps the approval-held call's arguments, and Govern binds the approval to their hash
(`arguments_sha256`, #31). On resume, `authorize_recorded_tool_call` re-evaluates the recorded tool-stage
facts against the current policy, with the caller's current identity and source, and checks the
arguments hash. It consumes the approval only on `allow`, and its transaction commits before the call. The
gateway then executes exactly that call, with no model re-run and no reservation (no billable stage).
If the recorded tool was the last in its phase, the workflow continues with the next phase. Otherwise a
fresh model step for the same phase runs on the evidence that now holds the recorded call's output, and
the phase continues from the next tool, so every later write tool is authorized on a run that saw all
earlier tool output. That step is a new decision, reserved and billed as usual. If it pauses for a
budget approval, the pause records the phase's first remaining tool, so tools that already ran are not
decided again. Under a stricter policy the call is denied and recorded, the approval
stays unconsumed, and the workflow is `blocked`. Budget approval followed by tool approval therefore
completes: the earlier livelock and the over-cap 409 are gone.

An approved call that then fails in the synthetic system is not retried:
- The approval stays `consumed`. It was spent on the attempt, and Govern's commit precedes the call.
- The step is `failed` and the workflow ends `failed` (502). The pause is cleared, so a second resume
  returns `409 WORKFLOW_NOT_PAUSED`.
- To try again, the operator checks the synthetic system and starts a new workflow, which asks for a new
  approval.

**Consequences.**
- The agent's tools are fixed per definition. A model-chosen tool loop would add a second guard: a check
  that the proposed tool is in the definition's allowlist, run before `authorize_tool_call`. That is
  deferred.
- Workflow budgets are cooperative: the default template (`tenant.yaml`, $1.00 per workflow, approval on
  exhaustion) sits under the hard request cap and the application budgets. Each budget approval funds one
  model step; any later model step meets the cap again. Tool approvals reserve nothing.
- With the P0 RLCD, the zero-API-price GCT-Qwen is usually chosen, so a workflow's API spend is often $0.
  GPU estimates are metered separately. Routing hints (`routing.preferred_model`) move steps to
  API-priced models inside the eligible set; the cumulative-budget tests use one.
