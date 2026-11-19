package aiva_test

import data.aiva

fixture_bundle := {
	"meta": {"version": "1", "bundle_hash": "test", "allow_simulated_ingress": true},
	"networks": {"net-care": "10.20.0.0/16", "net-console": "192.0.2.0/24"},
	"applications": {"support": {
		"enabled": true,
		"trusted_network_ids": ["net-care", "net-console"],
		"allowed_tasks": ["qa", "summarization"],
		"allowed_models": ["qwen", "gemini", "claude"],
		"classification_floor": "internal",
	}},
	"models": {
		"qwen": {"role": "upstream", "external": false, "sovereign": true, "enabled": true},
		"gemini": {"role": "upstream", "external": true, "sovereign": false, "enabled": true},
		"claude": {"role": "upstream", "external": true, "sovereign": false, "enabled": true},
		"cim": {"role": "cim", "external": false, "sovereign": false, "enabled": true},
	},
	"policies": [
		{
			"policy_id": "GCT-CONF-001", "priority": 1000,
			"match": {"classification_any": ["confidential", "restricted", "sovereign-only"]},
			"enforce": {
				"execution_path": "sovereign-direct", "allowed_models": ["qwen"], "cim": "bypass",
				"external_providers": "deny", "output_dlp": "required", "audit_level": "enhanced",
			},
		},
		{
			"policy_id": "GCT-SEC-002", "priority": 990,
			"match": {"dlp_findings_any": ["SECRET"]},
			"enforce": {"action": "deny", "reason": "secret", "remediation": "remove it"},
		},
		{
			"policy_id": "GCT-INJ-003", "priority": 980,
			"match": {"injection_detected": true},
			"enforce": {"action": "deny", "reason": "injection"},
		},
		{
			"policy_id": "TEST-APPROVAL", "priority": 100,
			"match": {"task_any": ["summarization"], "tags_any": ["needs-approval"]},
			"enforce": {"action": "approval_required", "reason": "approval"},
		},
	],
}

request(overrides) := object.union(
	{
		"stage": "request",
		"environment": "test",
		"tenant_id": "gct",
		"identity": {"kind": "application", "application_id": "support", "identity_mode": "authenticated", "roles": []},
		"source": {"ip": "10.20.1.15", "mode": "observed"},
		"request": {"task": "qa", "tags": [], "routing_mode": "hybrid", "quality_tier": "standard"},
		"classification": {"declared": "internal", "floor": "internal", "dlp": null, "rlcd": null, "effective": "internal"},
		"dlp": {"finding_types": [], "injection_detected": false, "injection_indicators": []},
		"egress": null,
	},
	overrides,
)

confidential := {"classification": {"declared": "confidential", "floor": "internal", "dlp": null, "rlcd": null, "effective": "confidential"}}

rule_ids(d) := {r.rule_id | some r in d.reasons}

test_internal_request_allows_all_granted_models if {
	d := aiva.decision with input as request({}) with data.aiva_bundle as fixture_bundle
	d.action == "allow"
	d.effective.allowed_models == ["claude", "gemini", "qwen"]
	d.effective.execution_path == null
	d.effective.cim == "allowed"
	d.matched_network_id == "net-care"
}

test_confidential_forces_sovereign_direct if {
	d := aiva.decision with input as request(confidential) with data.aiva_bundle as fixture_bundle
	d.action == "allow"
	d.effective.execution_path == "sovereign-direct"
	d.effective.allowed_models == ["qwen"]
	d.effective.cim == "bypass"
	d.effective.external_providers == "deny"
	d.effective.output_dlp == "required"
	d.matched_rule_ids == ["GCT-CONF-001"]
}

# LLD §16 confidentiality benchmark: a Claude-preferred tag cannot widen access.
test_confidential_with_claude_preference_still_sovereign if {
	req := request(object.union(confidential, {"request": {"task": "qa", "tags": ["claude-preferred"], "preferred_model": "claude", "routing_mode": "pinned"}}))
	d := aiva.decision with input as req with data.aiva_bundle as fixture_bundle
	d.effective.allowed_models == ["qwen"]
	{c.model_id | some c in d.denied_candidates} == {"claude", "gemini"}
}

# Appendix B invariant holds even if GCT-CONF-001 is deleted from the bundle.
test_sovereign_invariant_without_policy_document if {
	bundle := object.union(fixture_bundle, {"policies": []})
	d := aiva.decision with input as request(confidential) with data.aiva_bundle as bundle
	d.effective.allowed_models == ["qwen"]
	d.effective.cim == "bypass"
	d.effective.execution_path == "sovereign-direct"
}

test_secret_in_prompt_denied if {
	d := aiva.decision with input as request({"dlp": {"finding_types": ["SECRET"], "injection_detected": false}}) with data.aiva_bundle as fixture_bundle
	d.action == "deny"
	"GCT-SEC-002" in rule_ids(d)
}

test_prompt_injection_denied if {
	d := aiva.decision with input as request({"dlp": {"finding_types": [], "injection_detected": true}}) with data.aiva_bundle as fixture_bundle
	d.action == "deny"
	"GCT-INJ-003" in rule_ids(d)
}

test_untrusted_source_denied if {
	d := aiva.decision with input as request({"source": {"ip": "203.0.113.9", "mode": "observed"}}) with data.aiva_bundle as fixture_bundle
	d.action == "deny"
	"AIPEL-NET-001" in rule_ids(d)
}

test_missing_source_ip_denied if {
	d := aiva.decision with input as request({"source": {"ip": null, "mode": "observed"}}) with data.aiva_bundle as fixture_bundle
	"AIPEL-NET-001" in rule_ids(d)
}

# ADR-0027: an application granted internet callers accepts its keys from any source.
internet_bundle := json.patch(fixture_bundle, [{"op": "add", "path": "/applications/support/internet_callers", "value": true}])

test_internet_callers_accept_key_call_without_source if {
	d := aiva.decision with input as request({"source": {"ip": null, "mode": "observed"}}) with data.aiva_bundle as internet_bundle
	d.action == "allow"
	d.matched_network_id == "internet"
}

test_internet_callers_accept_key_call_from_public_address if {
	d := aiva.decision with input as request({"source": {"ip": "203.0.113.9", "mode": "observed"}}) with data.aiva_bundle as internet_bundle
	d.action == "allow"
	d.matched_network_id == "internet"
}

test_internet_callers_still_report_trusted_network if {
	d := aiva.decision with input as request({}) with data.aiva_bundle as internet_bundle
	d.matched_network_id == "net-care"
}

test_internet_callers_do_not_admit_users if {
	req := request({
		"identity": {"kind": "user", "application_id": "support", "identity_mode": "authenticated", "roles": ["finance_analyst"]},
		"source": {"ip": null, "mode": "observed"},
	})
	d := aiva.decision with input as req with data.aiva_bundle as internet_bundle
	d.action == "deny"
	"AIPEL-NET-001" in rule_ids(d)
	d.matched_network_id == null
}

test_internet_callers_do_not_admit_simulated_identities if {
	req := request({
		"identity": {"kind": "user", "application_id": "support", "identity_mode": "simulated", "roles": ["tester"]},
		"source": {"ip": "203.0.113.9", "mode": "simulated"},
	})
	d := aiva.decision with input as req with data.aiva_bundle as internet_bundle
	"AIPEL-NET-001" in rule_ids(d)
}

test_internet_callers_require_an_authenticated_key if {
	req := request({
		"identity": {"kind": "application", "application_id": "support", "identity_mode": "simulated", "roles": []},
		"source": {"ip": null, "mode": "observed"},
	})
	d := aiva.decision with input as req with data.aiva_bundle as internet_bundle
	"AIPEL-NET-001" in rule_ids(d)
}

test_internet_callers_keep_sovereign_path if {
	req := request(object.union(confidential, {"source": {"ip": null, "mode": "observed"}}))
	d := aiva.decision with input as req with data.aiva_bundle as internet_bundle
	d.action == "allow"
	d.effective.allowed_models == ["qwen"]
	d.effective.execution_path == "sovereign-direct"
	d.effective.cim == "bypass"
}

test_internet_callers_keep_other_rules if {
	req := request({"source": {"ip": null, "mode": "observed"}, "request": {"task": "coding", "tags": [], "routing_mode": "hybrid"}})
	d := aiva.decision with input as req with data.aiva_bundle as internet_bundle
	d.action == "deny"
	"AIPEL-TASK-001" in rule_ids(d)
	not "AIPEL-NET-001" in rule_ids(d)
}

test_internet_callers_flag_must_be_true if {
	bundle := json.patch(fixture_bundle, [{"op": "add", "path": "/applications/support/internet_callers", "value": "yes"}])
	d := aiva.decision with input as request({"source": {"ip": null, "mode": "observed"}}) with data.aiva_bundle as bundle
	"AIPEL-NET-001" in rule_ids(d)
}

test_unknown_application_denied if {
	req := request({"identity": {"kind": "application", "application_id": "nope", "identity_mode": "authenticated", "roles": []}})
	d := aiva.decision with input as req with data.aiva_bundle as fixture_bundle
	d.action == "deny"
	"AIPEL-ID-001" in rule_ids(d)
	d.effective.allowed_models == []
}

test_simulated_identity_requires_tester if {
	req := request({"identity": {"kind": "user", "application_id": "support", "identity_mode": "simulated", "roles": ["finance_analyst"]}})
	d := aiva.decision with input as req with data.aiva_bundle as fixture_bundle
	"AIPEL-ID-003" in rule_ids(d)
}

test_simulated_identity_allowed_for_public_visitor if {
	req := request({
		"identity": {"kind": "user", "application_id": "support", "identity_mode": "simulated", "roles": ["public_visitor"]},
		"source": {"ip": "192.0.2.10", "mode": "simulated"},
	})
	d := aiva.decision with input as req with data.aiva_bundle as fixture_bundle
	d.action == "allow"
}

test_public_visitor_without_a_source_is_denied_by_network if {
	req := request({
		"identity": {"kind": "user", "application_id": "support", "identity_mode": "simulated", "roles": ["public_visitor"]},
		"source": {"ip": null, "mode": "observed"},
	})
	d := aiva.decision with input as req with data.aiva_bundle as fixture_bundle
	d.action == "deny"
	"AIPEL-NET-001" in rule_ids(d)
	not "AIPEL-ID-003" in rule_ids(d)
}

test_simulated_identity_allowed_for_tester if {
	req := request({
		"identity": {"kind": "user", "application_id": "support", "identity_mode": "simulated", "roles": ["tester"]},
		"source": {"ip": "192.0.2.10", "mode": "simulated"},
	})
	d := aiva.decision with input as req with data.aiva_bundle as fixture_bundle
	d.action == "allow"
	d.matched_network_id == "net-console"
}

test_task_not_granted_denied if {
	d := aiva.decision with input as request({"request": {"task": "coding", "tags": [], "routing_mode": "hybrid"}}) with data.aiva_bundle as fixture_bundle
	"AIPEL-TASK-001" in rule_ids(d)
}

test_classification_downgrade_denied if {
	req := request({"classification": {"declared": "internal", "floor": "internal", "dlp": "confidential", "rlcd": null, "effective": "internal"}})
	d := aiva.decision with input as req with data.aiva_bundle as fixture_bundle
	"AIPEL-CLS-001" in rule_ids(d)
}

test_approval_required if {
	req := request({"request": {"task": "summarization", "tags": ["needs-approval"], "routing_mode": "hybrid"}})
	d := aiva.decision with input as req with data.aiva_bundle as fixture_bundle
	d.action == "approval_required"
	"TEST-APPROVAL" in rule_ids(d)
}

test_deny_overrides_approval if {
	req := request({
		"request": {"task": "summarization", "tags": ["needs-approval"], "routing_mode": "hybrid"},
		"dlp": {"finding_types": ["SECRET"], "injection_detected": false},
	})
	d := aiva.decision with input as req with data.aiva_bundle as fixture_bundle
	d.action == "deny"
}

test_local_only_mode_excludes_external if {
	d := aiva.decision with input as request({"request": {"task": "qa", "tags": [], "routing_mode": "local-only"}}) with data.aiva_bundle as fixture_bundle
	d.effective.allowed_models == ["qwen"]
}

test_no_bundle_denied if {
	d := aiva.decision with input as request({}) with data.aiva_bundle as {}
	d.action == "deny"
	"AIPEL-BUNDLE-001" in rule_ids(d)
}

test_egress_to_allowed_model if {
	d := aiva.decision with input as request({"stage": "egress", "egress": {"model_id": "gemini", "stage": "upstream"}}) with data.aiva_bundle as fixture_bundle
	d.action == "allow"
}

test_egress_outside_allowed_set_denied if {
	req := request(object.union(confidential, {"stage": "egress", "egress": {"model_id": "claude", "stage": "upstream"}}))
	d := aiva.decision with input as req with data.aiva_bundle as fixture_bundle
	d.action == "deny"
	"AIPEL-EGRESS-001" in rule_ids(d)
	"AIPEL-INV-001" in rule_ids(d)
}

test_cim_egress_denied_for_confidential if {
	req := request(object.union(confidential, {"stage": "egress", "egress": {"model_id": "cim", "stage": "cim"}}))
	d := aiva.decision with input as req with data.aiva_bundle as fixture_bundle
	d.action == "deny"
	"AIPEL-EGRESS-002" in rule_ids(d)
	"AIPEL-INV-001" in rule_ids(d)
}

test_cim_egress_allowed_for_internal if {
	d := aiva.decision with input as request({"stage": "egress", "egress": {"model_id": "cim", "stage": "cim"}}) with data.aiva_bundle as fixture_bundle
	d.action == "allow"
}

test_cim_egress_must_target_cim_model if {
	d := aiva.decision with input as request({"stage": "egress", "egress": {"model_id": "gemini", "stage": "cim"}}) with data.aiva_bundle as fixture_bundle
	"AIPEL-EGRESS-003" in rule_ids(d)
}

test_decision_reports_bundle_identity if {
	d := aiva.decision with input as request({}) with data.aiva_bundle as fixture_bundle
	d.policy_version == "1"
	d.bundle_hash == "test"
}

test_absent_task_hint_is_not_denied if {
	d := aiva.decision with input as request({"request": {"task": null, "tags": [], "routing_mode": "hybrid"}}) with data.aiva_bundle as fixture_bundle
	d.action == "allow"
}
