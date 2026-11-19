# Contracts

Generated from the gateway's Pydantic models — do not edit by hand. Regenerate with:

```bash
make contracts   # uv run aiva-admin export-contracts
```

CI fails if these files drift from the code.

| File | Contents | Used by |
|---|---|---|
| `openapi.json` | Gateway REST API (LLD §8, §21.6) | `web/packages/api-client` (typed client) |
| `schemas/event-envelope.schema.json` | Common event envelope (LLD §22.9) | Telemetry, Insights, UI |
| `schemas/cim-request.schema.json` / `cim-response.schema.json` | Generalized CIM interface (LLD §9) | CIM training data, CIM endpoint, Context Guard |
| `schemas/policy-input.schema.json` / `policy-decision.schema.json` | OPA input and decision (ADR-0005) | `policies/`, Govern |

Changing a contract changes what clients build against: make the change in a small, dedicated pull request.
