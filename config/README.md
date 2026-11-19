# Runtime configuration (versioned)

Everything the LLD §19 requires to be pinned and versioned lives here and is loaded by the gateway
(`aiva_gateway.catalog`). The seed command (`aiva-admin seed`) upserts it into Postgres and publishes the
policy bundle to OPA. All identities, networks and data are synthetic.

| File | Contents | Owner |
|---|---|---|
| `tenant.yaml` | GCT tenant, departments, cost centers, applications, demo users, trusted networks, budgets | Govern / FinOps |
| `models.yaml` | Model registry and pinned revisions | Optimize & Inference |
| `rate_cards.yaml` | Pinned prices with source and date | FinOps |
| `policies/*.yaml` | AI-PEL policy documents | Govern |
| `policies/dlp/tenant_vocabulary.yaml` | Tenant organisation, plan and bundle names that DLP never treats as people (ADR-0011) | Govern |
| `tools.yaml` | Tool registry for agents: access (read/write) and egress (internal/external), ADR-0012 | Govern |
| `scenarios.yaml` | The ten locked synthetic scenarios (LLD §15) | Scenarios |
| `taxonomy.yaml` | Classification taxonomy (LLD §21.3) | Insights |
| `context_profiles.yaml` | CIM context profiles (configuration, not model weights) | Optimize & Inference |
