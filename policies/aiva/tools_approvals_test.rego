package aiva_test

import data.aiva

# Tool registry, grants and tool policies on top of fixture_bundle (decision_test.rego).
tool_bundle := object.union(fixture_bundle, {
	"applications": {"support": {"allowed_tools": ["ticket.read", "notice.send", "notice.draft", "web.search"]}},
	"tools": {
		"ticket.read": {"access": "read", "egress": "internal", "enabled": true},
		"notice.draft": {"access": "write", "egress": "internal", "enabled": true},
		"notice.send": {"access": "write", "egress": "external", "enabled": true},
		"web.search": {"access": "read", "egress": "external", "enabled": true},
		"line.suspend": {"access": "write", "egress": "internal", "enabled": true},
		"old.tool": {"access": "read", "egress": "internal", "enabled": false},
	},
	"policies": array.concat(fixture_bundle.policies, [
		{
			"policy_id": "GCT-TOOL-005", "priority": 700,
			"match": {"tool_access_any": ["write"], "tool_egress_any": ["external"]},
			"enforce": {"action": "approval_required", "reason": "approve external side effects"},
		},
		{
			"policy_id": "GCT-TOOL-006", "priority": 970,
			"match": {"evidence_injection_detected": true, "tool_access_any": ["write"]},
			"enforce": {"action": "deny", "reason": "evidence injection"},
		},
	]),
})

tool_call(tool_id, overrides) := request(object.union({"stage": "tool", "tool": {"tool_id": tool_id}}, overrides))

tool_decision(tool_id, overrides) := d if {
	d := aiva.decision with input as tool_call(tool_id, overrides) with data.aiva_bundle as tool_bundle
}

test_granted_read_tool_allowed if {
	d := tool_decision("ticket.read", {})
	d.action == "allow"
}

test_internal_write_tool_allowed if {
	tool_decision("notice.draft", {}).action == "allow"
}

test_tool_call_without_tool_denied if {
	d := aiva.decision with input as request({"stage": "tool"}) with data.aiva_bundle as tool_bundle
	"AIPEL-TOOL-001" in rule_ids(d)
}

test_unregistered_tool_denied if {
	"AIPEL-TOOL-002" in rule_ids(tool_decision("rm.rf", {}))
}

test_disabled_tool_denied if {
	"AIPEL-TOOL-003" in rule_ids(tool_decision("old.tool", {}))
}

test_tool_not_granted_denied if {
	d := tool_decision("line.suspend", {})
	d.action == "deny"
	"AIPEL-TOOL-004" in rule_ids(d)
}

test_external_write_tool_requires_approval if {
	d := tool_decision("notice.send", {})
	d.action == "approval_required"
	"GCT-TOOL-005" in rule_ids(d)
}

# Appendix B: confidential data never reaches an external tool, even with no policy documents.
test_external_tool_denied_for_confidential_without_policies if {
	bundle := object.union(tool_bundle, {"policies": []})
	d := aiva.decision with input as tool_call("web.search", confidential) with data.aiva_bundle as bundle
	d.action == "deny"
	"AIPEL-INV-002" in rule_ids(d)
}

test_internal_tool_allowed_for_confidential if {
	tool_decision("ticket.read", confidential).action == "allow"
}

test_local_only_denies_external_tools if {
	d := tool_decision("web.search", {"request": {"task": "qa", "tags": [], "routing_mode": "local-only"}})
	"AIPEL-TOOL-005" in rule_ids(d)
}

test_evidence_injection_blocks_write_tools if {
	dlp := {"dlp": {"finding_types": [], "injection_detected": false, "evidence_injection_detected": true}}
	d := tool_decision("notice.draft", dlp)
	d.action == "deny"
	"GCT-TOOL-006" in rule_ids(d)
}

# --- Live web search (ADR-0022) ---------------------------------------------------------------

person_in_question := {"dlp": {"finding_types": ["PERSON"], "injection_detected": false, "injection_indicators": []}}

visitor := {"identity": {"kind": "user", "application_id": "support", "identity_mode": "simulated", "roles": ["public_visitor"]}}

presenter := {"identity": {"kind": "user", "application_id": "support", "identity_mode": "simulated", "roles": ["tester"]}}

test_web_search_allowed_for_a_clean_internal_question if {
	tool_decision("web.search", {}).action == "allow"
}

test_web_search_denied_at_confidential_and_above if {
	every level in ["confidential", "restricted", "sovereign-only"] {
		classification := {"declared": level, "floor": "internal", "dlp": null, "rlcd": null, "effective": level}
		d := tool_decision("web.search", {"classification": classification})
		d.action == "deny"
		"AIPEL-INV-002" in rule_ids(d)
	}
}

test_a_question_with_a_dlp_finding_is_never_searched if {
	# Even where the finding left the classification internal, the question is not sent (and never rewritten).
	d := tool_decision("web.search", person_in_question)
	d.action == "deny"
	"AIPEL-TOOL-006" in rule_ids(d)
}

test_dlp_findings_do_not_block_internal_or_write_tools if {
	tool_decision("ticket.read", person_in_question).action == "allow"
	not "AIPEL-TOOL-006" in rule_ids(tool_decision("notice.send", person_in_question))
}

test_a_public_visitor_never_searches_the_web if {
	d := tool_decision("web.search", visitor)
	d.action == "deny"
	"AIPEL-TOOL-007" in rule_ids(d)
}

test_a_presenter_may_search_the_web if {
	not "AIPEL-TOOL-007" in rule_ids(tool_decision("web.search", presenter))
}

test_the_visitor_rule_leaves_other_tools_alone if {
	not "AIPEL-TOOL-007" in rule_ids(tool_decision("ticket.read", visitor))
	not "AIPEL-TOOL-007" in rule_ids(tool_decision("notice.send", visitor))
}

test_evidence_injection_keeps_read_tools if {
	dlp := {"dlp": {"finding_types": [], "injection_detected": false, "evidence_injection_detected": true}}
	tool_decision("ticket.read", dlp).action == "allow"
}

# Evidence is data: injection text in evidence alone never denies the request itself.
test_evidence_injection_does_not_deny_request if {
	dlp := {"dlp": {"finding_types": [], "injection_detected": false, "evidence_injection_detected": true}}
	d := aiva.decision with input as request(dlp) with data.aiva_bundle as tool_bundle
	d.action == "allow"
}

test_tool_stage_still_enforces_trusted_network if {
	d := tool_decision("ticket.read", {"source": {"ip": "203.0.113.9", "mode": "observed"}})
	"AIPEL-NET-001" in rule_ids(d)
}

# --- Approvals ---------------------------------------------------------------------------------

needs_approval := {"request": {"task": "summarization", "tags": ["needs-approval"], "routing_mode": "hybrid"}}

test_request_approval_satisfies_rule if {
	req := request(object.union(needs_approval, {"approvals": [{"approval_id": "apr_1", "grants": [{"rule_id": "TEST-APPROVAL", "tool_id": null}]}]}))
	d := aiva.decision with input as req with data.aiva_bundle as fixture_bundle
	d.action == "allow"
	d.satisfied_approval_rule_ids == ["TEST-APPROVAL"]
}

test_approval_for_other_rule_does_not_satisfy if {
	req := request(object.union(needs_approval, {"approvals": [{"approval_id": "apr_1", "grants": [{"rule_id": "OTHER", "tool_id": null}]}]}))
	d := aiva.decision with input as req with data.aiva_bundle as fixture_bundle
	d.action == "approval_required"
	d.satisfied_approval_rule_ids == []
}

test_approval_never_overrides_deny if {
	req := request(object.union(needs_approval, {
		"dlp": {"finding_types": ["SECRET"], "injection_detected": false},
		"approvals": [{"approval_id": "apr_1", "grants": [{"rule_id": "TEST-APPROVAL"}, {"rule_id": "GCT-SEC-002"}]}],
	}))
	d := aiva.decision with input as req with data.aiva_bundle as fixture_bundle
	d.action == "deny"
	"GCT-SEC-002" in rule_ids(d)
}

test_request_approval_applies_at_egress if {
	req := request(object.union(needs_approval, {
		"stage": "egress", "egress": {"model_id": "gemini", "stage": "upstream"},
		"approvals": [{"approval_id": "apr_1", "grants": [{"rule_id": "TEST-APPROVAL", "tool_id": null}]}],
	}))
	d := aiva.decision with input as req with data.aiva_bundle as fixture_bundle
	d.action == "allow"
}

test_tool_approval_applies_to_its_tool if {
	approval := {"approvals": [{"approval_id": "apr_2", "grants": [{"rule_id": "GCT-TOOL-005", "tool_id": "notice.send"}]}]}
	d := tool_decision("notice.send", approval)
	d.action == "allow"
	d.satisfied_approval_rule_ids == ["GCT-TOOL-005"]
}

test_tool_approval_does_not_cover_other_tools if {
	bundle := object.union(tool_bundle, {"tools": {"sms.send": {"access": "write", "egress": "external", "enabled": true}}, "applications": {"support": {"allowed_tools": ["notice.send", "sms.send"]}}})
	approval := {"approvals": [{"approval_id": "apr_2", "grants": [{"rule_id": "GCT-TOOL-005", "tool_id": "notice.send"}]}]}
	d := aiva.decision with input as tool_call("sms.send", approval) with data.aiva_bundle as bundle
	d.action == "approval_required"
}

# --- Policy simulator --------------------------------------------------------------------------

test_simulate_evaluates_candidate_bundle_not_loaded_one if {
	draft := object.union(fixture_bundle, {
		"meta": {"version": "2", "bundle_hash": "draft"},
		"policies": array.concat(fixture_bundle.policies, [{"policy_id": "DRAFT-DENY-ALL", "priority": 1, "match": {}, "enforce": {"action": "deny"}}]),
	})
	simulated := aiva.simulate with input as {"bundle": draft, "case": request({})} with data.aiva_bundle as fixture_bundle
	simulated.action == "deny"
	simulated.policy_version == "2"
	"DRAFT-DENY-ALL" in rule_ids(simulated)
	live := aiva.decision with input as request({}) with data.aiva_bundle as fixture_bundle
	live.action == "allow"
}
