package aiva_test

import data.aiva

# Answer-quality comparison (ADR-0023) on top of fixture_bundle (decision_test.rego). `claude` is the pinned
# reference and `grader` the pinned grader.
quality_bundle := object.union(fixture_bundle, {
	"meta": {"tenant_id": "gct"},
	"models": {"grader": {"role": "grader", "external": true, "sovereign": false, "enabled": true}},
	"quality": {"reference_model_id": "claude", "grader_model_id": "grader"},
})

# A presenter's comparison of the live sovereign-routing scenario's synthetic sample: every gate holds.
s11_comparison(stage, model_id, overrides) := request(object.union(
	{
		"stage": "egress",
		"identity": {"kind": "user", "application_id": "support", "identity_mode": "authenticated", "roles": ["tester"]},
		"request": {"task": "summarization", "scenario_id": "live-sovereign-routing", "tags": [], "routing_mode": "hybrid", "quality_tier": "standard"},
		"classification": confidential.classification,
		"egress": {"model_id": model_id, "stage": stage},
		"quality": {"requested": true, "synthetic_sample": true, "data_designation": "synthetic"},
	},
	overrides,
))

reference(overrides) := d if {
	d := aiva.decision with input as s11_comparison("quality_reference", "claude", overrides)
		with data.aiva_bundle as quality_bundle
}

grading(overrides) := d if {
	d := aiva.decision with input as s11_comparison("quality_grading", "grader", overrides)
		with data.aiva_bundle as quality_bundle
}

# Both calls are refused with `rule` among the reasons.
both_denied_by(overrides, rule) if {
	ref := reference(overrides)
	ref.action == "deny"
	rule in rule_ids(ref)
	grade := grading(overrides)
	grade.action == "deny"
	rule in rule_ids(grade)
}

# --- Confidential data never leaves for a comparison ---------------------------------------------
# Even a presenter's explicit request on a committed synthetic sample is refused: no exception exists here.

test_s11_sample_comparison_denied_even_for_a_presenter if {
	both_denied_by({}, "AIPEL-INV-001")
}

test_every_presenter_role_is_denied if {
	every role in ["platform_admin", "tenant_admin", "tester"] {
		ref := reference({"identity": {"roles": [role]}})
		ref.action == "deny"
		"AIPEL-INV-001" in rule_ids(ref)
	}
}

# --- The served answer is unchanged ---------------------------------------------------------------

test_s11_request_still_takes_the_sovereign_path if {
	req := s11_comparison("quality_reference", "claude", {"stage": "request", "egress": null})
	d := aiva.decision with input as req with data.aiva_bundle as quality_bundle
	d.action == "allow"
	d.effective.execution_path == "sovereign-direct"
	d.effective.allowed_models == ["qwen"]
	d.effective.external_providers == "deny"
	d.effective.cim == "bypass"
}

# The exception is for the two comparison stages only: with the same facts, every other egress is denied.
test_s11_upstream_baseline_and_cim_calls_still_denied if {
	every stage in ["upstream", "baseline", "validation"] {
		d := aiva.decision with input as s11_comparison(stage, "claude", {}) with data.aiva_bundle as quality_bundle
		d.action == "deny"
		"AIPEL-INV-001" in rule_ids(d)
		"AIPEL-EGRESS-001" in rule_ids(d)
	}
	cim := aiva.decision with input as s11_comparison("cim", "cim", {}) with data.aiva_bundle as quality_bundle
	"AIPEL-EGRESS-002" in rule_ids(cim)
	"AIPEL-INV-001" in rule_ids(cim)
	upstream_grader := aiva.decision with input as s11_comparison("upstream", "grader", {})
		with data.aiva_bundle as quality_bundle
	"AIPEL-INV-001" in rule_ids(upstream_grader)
}

test_s11_sovereign_model_still_served if {
	d := aiva.decision with input as s11_comparison("upstream", "qwen", {}) with data.aiva_bundle as quality_bundle
	d.action == "allow"
}

# --- One missing gate each: the comparison is refused ------------------------------------------

test_no_explicit_request_denied if {
	both_denied_by({"quality": {"requested": false}}, "AIPEL-QUAL-001")
	both_denied_by({"quality": {"requested": false}}, "AIPEL-INV-001")
	both_denied_by({"quality": null}, "AIPEL-QUAL-001")
	both_denied_by({"quality": null}, "AIPEL-INV-001")
}

test_public_visitor_denied if {
	both_denied_by({"identity": {"roles": ["public_visitor"]}}, "AIPEL-QUAL-001")
	both_denied_by({"identity": {"roles": ["public_visitor"]}}, "AIPEL-INV-001")
	both_denied_by({"identity": {"roles": ["tester", "public_visitor"]}}, "AIPEL-QUAL-001")
}

test_non_presenter_user_denied if {
	both_denied_by({"identity": {"roles": ["security_auditor"]}}, "AIPEL-QUAL-001")
	both_denied_by({"identity": {"roles": []}}, "AIPEL-INV-001")
}

test_application_credential_denied if {
	app_caller := {"identity": {"kind": "application", "roles": ["tester"]}}
	both_denied_by(app_caller, "AIPEL-QUAL-001")
	both_denied_by(app_caller, "AIPEL-INV-001")
}

test_other_scenario_denied if {
	both_denied_by({"request": {"scenario_id": "fraud-management"}}, "AIPEL-INV-001")
	both_denied_by({"request": {"scenario_id": null}}, "AIPEL-INV-001")
	"AIPEL-EGRESS-001" in rule_ids(reference({"request": {"scenario_id": "fraud-management"}}))
	"AIPEL-QUAL-003" in rule_ids(grading({"request": {"scenario_id": "fraud-management"}}))
}

# A presenter's own text on the S11 button is not the committed synthetic sample.
test_text_other_than_the_synthetic_sample_denied if {
	both_denied_by({"quality": {"synthetic_sample": false}}, "AIPEL-INV-001")
}

test_non_synthetic_deployment_denied if {
	both_denied_by({"quality": {"data_designation": "customer"}}, "AIPEL-INV-001")
	both_denied_by({"quality": {"data_designation": null}}, "AIPEL-INV-001")
}

test_other_tenant_denied if {
	both_denied_by({"tenant_id": "acme"}, "AIPEL-INV-001")
	other := object.union(quality_bundle, {"meta": {"tenant_id": "acme"}})
	d := aiva.decision with input as s11_comparison("quality_reference", "claude", {})
		with data.aiva_bundle as other
	"AIPEL-INV-001" in rule_ids(d)
}

# Only confidential: restricted and sovereign-only data never leave, even on S11.
test_restricted_and_sovereign_only_denied if {
	every level in ["restricted", "sovereign-only"] {
		both_denied_by({"classification": {"dlp": level, "effective": level}}, "AIPEL-INV-001")
	}
}

test_local_only_routing_denied if {
	both_denied_by({"request": {"routing_mode": "local-only"}}, "AIPEL-INV-001")
}

test_unpinned_reference_denied if {
	d := aiva.decision with input as s11_comparison("quality_reference", "gemini", {})
		with data.aiva_bundle as quality_bundle
	d.action == "deny"
	"AIPEL-QUAL-002" in rule_ids(d)
	"AIPEL-INV-001" in rule_ids(d)
}

test_unpinned_grader_denied if {
	d := aiva.decision with input as s11_comparison("quality_grading", "claude", {})
		with data.aiva_bundle as quality_bundle
	d.action == "deny"
	"AIPEL-QUAL-002" in rule_ids(d)
	"AIPEL-INV-001" in rule_ids(d)
}

# No grader registered, or no comparison section in the bundle: nothing is called.
test_unregistered_grader_or_reference_denied if {
	no_grader := object.union(quality_bundle, {"quality": {"grader_model_id": null}})
	d := aiva.decision with input as s11_comparison("quality_grading", "grader", {})
		with data.aiva_bundle as no_grader
	"AIPEL-QUAL-002" in rule_ids(d)
	"AIPEL-INV-001" in rule_ids(d)
	no_section := object.remove(quality_bundle, ["quality"])
	ref := aiva.decision with input as s11_comparison("quality_reference", "claude", {})
		with data.aiva_bundle as no_section
	"AIPEL-QUAL-002" in rule_ids(ref)
	"AIPEL-INV-001" in rule_ids(ref)
}

# A policy document's deny applies too, alongside the sovereignty invariant.
test_deny_policies_also_apply if {
	secret := {"dlp": {"finding_types": ["SECRET"], "injection_detected": false}}
	both_denied_by(secret, "GCT-SEC-002")
	both_denied_by(secret, "AIPEL-INV-001")
}

# --- Use cases that are not sovereign: normal egress policy decides -----------------------------

internal_comparison(stage, model_id, overrides) := s11_comparison(stage, model_id, object.union(
	{
		"request": {"task": "qa", "scenario_id": null},
		"classification": request({}).classification,
		"quality": {"synthetic_sample": false},
	},
	overrides,
))

internal_decision(stage, model_id, overrides) := d if {
	d := aiva.decision with input as internal_comparison(stage, model_id, overrides)
		with data.aiva_bundle as quality_bundle
}

# Presenter free text included: no exception is needed, and none applies.
test_internal_comparison_allowed_without_the_exception if {
	internal_decision("quality_reference", "claude", {}).action == "allow"
	internal_decision("quality_grading", "grader", {}).action == "allow"
}

test_internal_comparison_still_needs_a_presenter_request if {
	"AIPEL-QUAL-001" in rule_ids(internal_decision("quality_reference", "claude", {"quality": {"requested": false}}))
	"AIPEL-QUAL-001" in rule_ids(internal_decision("quality_grading", "grader", {"identity": {"roles": ["public_visitor"]}}))
}

test_internal_comparison_respects_local_only_routing if {
	local := {"request": {"routing_mode": "local-only"}}
	"AIPEL-EGRESS-001" in rule_ids(internal_decision("quality_reference", "claude", local))
	"AIPEL-QUAL-003" in rule_ids(internal_decision("quality_grading", "grader", local))
}

test_reference_outside_the_application_allowlist_denied if {
	narrow := object.union(quality_bundle, {"applications": {"support": {"allowed_models": ["qwen", "gemini"]}}})
	d := aiva.decision with input as internal_comparison("quality_reference", "claude", {})
		with data.aiva_bundle as narrow
	"AIPEL-EGRESS-001" in rule_ids(d)
}
