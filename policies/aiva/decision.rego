# AI-PEL decision policy (LLD §7, ADR-0005, ADR-0012). Query: data.aiva.decision
package aiva

bundle := data.aiva_bundle

rank := {"public": 0, "internal": 1, "confidential": 2, "restricted": 3, "sovereign-only": 4}

sovereign_rank := 2

app := bundle.applications[input.identity.application_id]

effective_rank := rank[input.classification.effective]

reason(rule_id, message, remediation) := {"rule_id": rule_id, "message": message, "remediation": remediation}

# --- Identity and source -----------------------------------------------------------------------

deny contains reason("AIPEL-BUNDLE-001", "No published policy bundle is loaded.", "Publish a policy version in AiVa Govern.") if {
	not bundle.meta.version
}

deny contains reason("AIPEL-ID-001", "The application is not registered.", "Register the application in AiVa Govern.") if {
	bundle.meta.version
	not app
}

deny contains reason("AIPEL-ID-002", "The application is disabled.", "Ask an administrator to re-enable the application.") if {
	app
	not app.enabled
}

deny contains reason("AIPEL-ID-003", "Simulated identities are only accepted from testers in a demo environment.", "Sign in as a tester or call with an application credential.") if {
	input.identity.identity_mode == "simulated"
	not simulation_permitted
}

# Roles that run Test Console profiles: the tester, and the public demo's visitor (ADR-0019).
console_roles := {"tester", "public_visitor"}

simulation_permitted if {
	some role in input.identity.roles
	role in console_roles
	bundle.meta.allow_simulated_ingress == true
}

matched_networks := [id |
	some id in app.trusted_network_ids
	input.source.ip != null
	net.cidr_contains(bundle.networks[id], input.source.ip)
]

default matched_network_id := null

matched_network_id := matched_networks[0] if count(matched_networks) > 0

# An application granted internet callers accepts a call made with one of its keys from any source, the null
# source of a proxied request included (ADR-0027). Users and simulated identities still need a trusted
# network, and every other rule still applies.
internet_key_call if {
	app.internet_callers == true
	input.identity.kind == "application"
	input.identity.identity_mode == "authenticated"
}

matched_network_id := "internet" if {
	count(matched_networks) == 0
	internet_key_call
}

deny contains reason("AIPEL-NET-001", "The request source is not in a trusted network for this application.", "Call from an approved network or ask an administrator to register it.") if {
	app
	count(matched_networks) == 0
	not internet_key_call
}

# Enforced on caller- or scenario-supplied task hints. RLCD-predicted tasks are not used for
# authorization until the classifier is calibrated (P2).
deny contains reason("AIPEL-TASK-001", "The task is not permitted for this application.", "Use a task the application is approved for.") if {
	app
	input.request.task != null
	not input.request.task in app.allowed_tasks
}

# The gateway computes the most restrictive classification; re-check it was not relaxed.
deny contains reason("AIPEL-CLS-001", "The effective classification is less restrictive than a detected or declared classification.", "Resubmit; classification must not be downgraded.") if {
	some signal in ["declared", "floor", "dlp", "rlcd"]
	level := input.classification[signal]
	level != null
	rank[level] > effective_rank
}

# --- Policy documents --------------------------------------------------------------------------

matched contains p if {
	some p in bundle.policies
	every key, value in p.match {
		condition_holds(key, value)
	}
}

condition_holds("classification_any", values) if input.classification.effective in values

condition_holds("application_any", values) if input.identity.application_id in values

condition_holds("task_any", values) if input.request.task in values

condition_holds("tags_any", values) if {
	some tag in input.request.tags
	tag in values
}

condition_holds("dlp_findings_any", values) if {
	some finding in input.dlp.finding_types
	finding in values
}

condition_holds("injection_detected", value) if input.dlp.injection_detected == value

condition_holds("source_cidr_any", values) if {
	some cidr in values
	net.cidr_contains(cidr, input.source.ip)
}

condition_holds("environment_any", values) if input.environment in values

# Evidence is data, never instructions: this fact can only restrict (e.g. block tool side effects).
condition_holds("evidence_injection_detected", value) if {
	object.get(input.dlp, "evidence_injection_detected", false) == value
}

condition_holds("tool_any", values) if input.tool.tool_id in values

condition_holds("tool_access_any", values) if tool.access in values

condition_holds("tool_egress_any", values) if tool.egress in values

deny contains reason(p.policy_id, object.get(p.enforce, "reason", "Blocked by AI policy."), object.get(p.enforce, "remediation", null)) if {
	some p in matched
	p.enforce.action == "deny"
}

approvals contains reason(p.policy_id, object.get(p.enforce, "reason", "Approval required."), object.get(p.enforce, "remediation", null)) if {
	some p in matched
	p.enforce.action == "approval_required"
	not p.policy_id in approved_rule_ids
}

# --- Verified approvals ------------------------------------------------------------------------
# The gateway adds an approval to the input only after checking its status, expiry and binding to
# this request. Approvals satisfy approval rules only; they never override a deny.

approved_rule_ids contains grant.rule_id if {
	some approval in input.approvals
	some grant in approval.grants
	grant_applies(grant)
}

# Request grants apply at every stage; tool grants only to calls of the approved tool.
grant_applies(grant) if object.get(grant, "tool_id", null) == null

grant_applies(grant) if {
	input.stage == "tool"
	grant.tool_id == input.tool.tool_id
}

satisfied_approval_rule_ids := sort([p.policy_id |
	some p in matched
	p.enforce.action == "approval_required"
	p.policy_id in approved_rule_ids
])

# --- Effective constraints ---------------------------------------------------------------------

sovereign_path if effective_rank >= sovereign_rank

sovereign_path if {
	some p in matched
	p.enforce.execution_path == "sovereign-direct"
}

external_denied if effective_rank >= sovereign_rank

external_denied if {
	some p in matched
	p.enforce.external_providers == "deny"
}

external_denied if input.request.routing_mode == "local-only"

cim_bypass if effective_rank >= sovereign_rank

cim_bypass if {
	some p in matched
	p.enforce.cim == "bypass"
}

policy_model_sets := [{m | some m in p.enforce.allowed_models} |
	some p in matched
	p.enforce.allowed_models
]

candidate_models := {m |
	some m in app.allowed_models
	bundle.models[m].role == "upstream"
}

allowed_models := {m |
	some m in candidate_models
	not exclusion(m)
}

exclusion(m) := "Model is disabled." if {
	not bundle.models[m].enabled
} else := "External providers are denied for this request." if {
	external_denied
	bundle.models[m].external
} else := "Sovereign direct route required." if {
	sovereign_path
	not bundle.models[m].sovereign
} else := "Not in the model allowlist of a matched policy." if {
	some allowed in policy_model_sets
	not m in allowed
}

denied_candidates := [{"model_id": m, "reason": exclusion(m)} |
	some m in sort(candidate_models)
	exclusion(m)
]

deny contains reason("AIPEL-ROUTE-001", "No model satisfies every applicable policy.", "Adjust the request classification or contact the policy owner.") if {
	app
	count(allowed_models) == 0
}

# --- Egress checks (before every CIM, upstream, baseline or comparison call) -------------------

egress_model := bundle.models[input.egress.model_id]

# The grader is not a routing target, so it is never in the allowed set: AIPEL-QUAL-002 and -003 check it.
deny contains reason("AIPEL-EGRESS-001", "The selected model is outside the allowed set.", null) if {
	input.stage == "egress"
	not input.egress.stage in {"cim", "quality_grading"}
	not input.egress.model_id in allowed_models
}

deny contains reason("AIPEL-EGRESS-002", "Context intelligence is not permitted for this request.", null) if {
	input.stage == "egress"
	input.egress.stage == "cim"
	cim_bypass
}

deny contains reason("AIPEL-EGRESS-003", "The CIM call must target the registered CIM model.", null) if {
	input.stage == "egress"
	input.egress.stage == "cim"
	not egress_model.role == "cim"
}

deny contains reason("AIPEL-INV-001", "Confidential data cannot be sent to an external provider.", null) if {
	input.stage == "egress"
	effective_rank >= sovereign_rank
	not egress_model.sovereign
}

# --- Answer-quality comparison (ADR-0023) ------------------------------------------------------
# After a live answer, a presenter may compare it with the pinned frontier reference
# (`quality_reference`), graded by the pinned grader (`quality_grading`). Each is an egress call, checked
# here before it is made.

quality_stages := {"quality_reference", "quality_grading"}

quality_stage if {
	input.stage == "egress"
	input.egress.stage in quality_stages
}

presenter_roles := {"platform_admin", "tenant_admin", "tester"}

# Only the quality-check endpoint sets `quality.requested`: a signed-in presenter asked for this comparison.
quality_requested if {
	input.quality.requested == true
	input.identity.kind == "user"
	some role in input.identity.roles
	role in presenter_roles
	not "public_visitor" in input.identity.roles
}

deny contains reason("AIPEL-QUAL-001", "Answer-quality comparison needs a presenter's explicit request.", "Sign in as a presenter and ask for the comparison on the live demo page.") if {
	quality_stage
	not quality_requested
}

pinned_quality_model := object.get(bundle, ["quality", "reference_model_id"], null) if {
	input.egress.stage == "quality_reference"
}

pinned_quality_model := object.get(bundle, ["quality", "grader_model_id"], null) if {
	input.egress.stage == "quality_grading"
}

# No grader registered means no grading call.
deny contains reason("AIPEL-QUAL-002", "The comparison must use the pinned reference or grader model.", null) if {
	quality_stage
	input.egress.model_id != pinned_quality_model
}

# The grader's own external-provider check; the reference has AIPEL-EGRESS-001.
deny contains reason("AIPEL-QUAL-003", "External providers are denied for this request.", null) if {
	input.stage == "egress"
	input.egress.stage == "quality_grading"
	external_denied
	egress_model.external
}

# --- Tool checks (before every tool call) ------------------------------------------------------
# The tool registry is authoritative for access and egress; callers only name the tool.

tool := bundle.tools[input.tool.tool_id]

deny contains reason("AIPEL-TOOL-001", "The tool call does not name a tool.", null) if {
	input.stage == "tool"
	not input.tool.tool_id
}

deny contains reason("AIPEL-TOOL-002", "The tool is not registered.", "Register the tool in AiVa Govern.") if {
	input.stage == "tool"
	input.tool.tool_id
	not tool
}

deny contains reason("AIPEL-TOOL-003", "The tool is disabled.", "Ask an administrator to re-enable the tool.") if {
	input.stage == "tool"
	tool
	not tool.enabled
}

deny contains reason("AIPEL-TOOL-004", "The application is not permitted to use this tool.", "Ask an administrator to grant the tool to the application.") if {
	input.stage == "tool"
	app
	tool
	not input.tool.tool_id in object.get(app, "allowed_tools", [])
}

deny contains reason("AIPEL-TOOL-005", "External tools are denied for this request.", null) if {
	input.stage == "tool"
	external_denied
	tool
	tool.egress != "internal"
}

# An external read tool sends the request's own question outside AiVa (`web.search`, ADR-0022).
external_read_tool if {
	tool
	tool.access == "read"
	tool.egress == "external"
}

# A question with any DLP finding is never searched, and never searched after rewriting it.
deny contains reason("AIPEL-TOOL-006", "The search question contains data that must not leave AiVa.", "Remove personal data and secrets from the question. AiVa never searches a rewritten question.") if {
	input.stage == "tool"
	external_read_tool
	count(input.dlp.finding_types) > 0
}

# Live web search is for signed-in presenters (ADR-0022). The public visitor never reaches it, so real web
# content and per-search cost stay off the public demo.
deny contains reason("AIPEL-TOOL-007", "Public demo visitors cannot search the web.", "Sign in as a presenter to use live web search.") if {
	input.stage == "tool"
	external_read_tool
	"public_visitor" in input.identity.roles
}

# Built-in invariant, independent of policy documents (LLD Appendix B).
deny contains reason("AIPEL-INV-002", "Confidential data cannot be sent to an external tool.", null) if {
	input.stage == "tool"
	effective_rank >= sovereign_rank
	tool
	tool.egress != "internal"
}

# --- Decision ----------------------------------------------------------------------------------

default execution_path := null

execution_path := "sovereign-direct" if sovereign_path

default cim := "allowed"

cim := "bypass" if cim_bypass

default external_providers := "allow"

external_providers := "deny" if external_denied

default action := "allow"

action := "deny" if count(deny) > 0

action := "approval_required" if {
	count(deny) == 0
	count(approvals) > 0
}

matched_rule_ids := sort([p.policy_id | some p in matched])

audit_level := "enhanced" if {
	some p in matched
	p.enforce.audit_level == "enhanced"
} else := "standard"

output_dlp := "required" if {
	some p in matched
	p.enforce.output_dlp == "required"
} else := "required" if {
	effective_rank >= sovereign_rank
} else := "optional"

decision := {
	"action": action,
	"reasons": array.concat([r | some r in deny], [r | some r in approvals]),
	"matched_rule_ids": matched_rule_ids,
	"matched_network_id": matched_network_id,
	"effective": {
		"execution_path": execution_path,
		"allowed_models": sort(allowed_models),
		"cim": cim,
		"external_providers": external_providers,
		"output_dlp": output_dlp,
		"audit_level": audit_level,
	},
	"denied_candidates": denied_candidates,
	"satisfied_approval_rule_ids": satisfied_approval_rule_ids,
	"policy_version": object.get(bundle, ["meta", "version"], ""),
	"bundle_hash": object.get(bundle, ["meta", "bundle_hash"], ""),
}

# --- Policy simulator --------------------------------------------------------------------------
# Evaluate a candidate bundle (draft or archived version) against a case without loading it.
# Query: data.aiva.simulate with input {"bundle": <bundle>, "case": <policy input>}.

simulate := result if {
	result := decision with data.aiva_bundle as input.bundle with input as input.case
}
