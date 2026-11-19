# AI-PEL policies (OPA, Rego v1)

`aiva/decision.rego` evaluates `data.aiva.decision` for the gateway at the `request`, `egress` and
`tool` stages (ADR-0005, ADR-0012). The policy logic is generic; the tenant's policy documents,
application grants, trusted networks, model registry and tool registry are loaded as data under
`data.aiva_bundle` by the gateway when a policy version is published (`aiva-admin seed` /
`POST /v1/admin/policies/publish`). `data.aiva.simulate` evaluates a candidate bundle passed in the input
without loading it (policy simulator, `POST /v1/admin/policies/test`).

Rules:
- Grants come only from the application registry. Policy documents can only restrict.
- Deny overrides approval; approval overrides allow. Conflicting mandatory paths deny.
- Built-in invariants (LLD Appendix B), independent of policy documents: confidential, restricted and
  sovereign-only payloads never reach CIM, external providers or external tools, and take the sovereign
  direct path.
- Tool stage: the tool must be registered, enabled and granted to the application; the registry decides
  its access (read/write) and egress (internal/external).
- Approvals: `input.approvals` carries grants the gateway verified; they satisfy approval rules only (a
  tool grant only for its tool) and never override a deny.

Run the tests: `opa test policies/ -v` (OPA v1.20.0). Tool and approval tests are in
`aiva/tools_approvals_test.rego`.
