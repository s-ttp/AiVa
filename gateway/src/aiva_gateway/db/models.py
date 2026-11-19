"""Canonical schema (ADR-0003): reference data, policy versions, run records and reporting aggregates.

Money columns are integer micro-USD (`*_micros`). No prompt text is stored in these tables.
"""

from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from aiva_gateway.db.base import Base, utcnow

EMBEDDING_DIMENSIONS = 768  # BAAI/bge-base-en-v1.5

# --- Reference data (seeded from config/) ------------------------------------------------------


class TenantRow(Base):
    __tablename__ = "tenant"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class CostCenterRow(Base):
    __tablename__ = "cost_center"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(200))


class DepartmentRow(Base):
    __tablename__ = "department"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    name: Mapped[str] = mapped_column(String(200))
    business_function: Mapped[str] = mapped_column(String(32))
    cost_center_id: Mapped[str] = mapped_column(ForeignKey("cost_center.id"))


class TrustedNetworkRow(Base):
    __tablename__ = "trusted_network"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    name: Mapped[str] = mapped_column(String(200))
    cidr: Mapped[str] = mapped_column(String(64))


class ApplicationRow(Base):
    __tablename__ = "application"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    department_id: Mapped[str] = mapped_column(ForeignKey("department.id"))
    name: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    trusted_network_ids: Mapped[list[str]] = mapped_column(default=list)
    allowed_tasks: Mapped[list[str]] = mapped_column(default=list)
    allowed_models: Mapped[list[str]] = mapped_column(default=list)
    classification_floor: Mapped[str] = mapped_column(String(32), default="internal")


class ApplicationCredentialRow(Base):
    """Application credentials. Only an HMAC of the secret is stored (ADR-0007). Keys issued from the admin
    console always expire (ADR-0027); a key made with the CLI has no expiry."""

    __tablename__ = "application_credential"
    key_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("application.id"))
    secret_hmac: Mapped[str] = mapped_column(String(128))
    label: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
    # The issuing and revoking actor: a user's pseudonym, or `cli` for `aiva-admin create-credential`.
    created_by: Mapped[str] = mapped_column(String(64), default="cli")
    revoked_by: Mapped[str | None] = mapped_column(String(64), default=None)
    expires_at: Mapped[datetime | None] = mapped_column(default=None)
    # Accurate to the hour: written at most once an hour per key (auth.authenticate).
    last_used_at: Mapped[datetime | None] = mapped_column(default=None)


class PrincipalRow(Base):
    __tablename__ = "principal"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    pseudonym: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    roles: Mapped[list[str]] = mapped_column(default=list)
    department_id: Mapped[str | None] = mapped_column(ForeignKey("department.id"), default=None)
    owned_application_ids: Mapped[list[str]] = mapped_column(default=list)


class ModelRow(Base):
    __tablename__ = "model"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(32))
    provider_model_id: Mapped[str] = mapped_column(String(200))
    revision: Mapped[str] = mapped_column(String(200))
    sovereign: Mapped[bool] = mapped_column(Boolean)
    external: Mapped[bool] = mapped_column(Boolean)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    rate_card_id: Mapped[str] = mapped_column(String(64))
    spec: Mapped[dict[str, Any]] = mapped_column(default=dict)


class RateCardRow(Base):
    __tablename__ = "rate_card"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(64))
    model_id: Mapped[str] = mapped_column(String(64))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    input_micros_per_mtok: Mapped[int] = mapped_column()
    output_micros_per_mtok: Mapped[int] = mapped_column()
    cached_input_micros_per_mtok: Mapped[int] = mapped_column()
    gpu_micros_per_hour: Mapped[int | None] = mapped_column(default=None)
    effective_from: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(Text)


class BudgetRow(Base):
    __tablename__ = "budget"
    __table_args__ = (UniqueConstraint("tenant_id", "scope_type", "scope_id", "period"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    scope_type: Mapped[str] = mapped_column(String(32))
    scope_id: Mapped[str] = mapped_column(String(64))
    period: Mapped[str] = mapped_column(String(16))
    cap_micros: Mapped[int] = mapped_column()
    alert_thresholds: Mapped[list[Any]] = mapped_column(default=list)
    on_exhaustion: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(64), default=None)


class TaxonomyVersionRow(Base):
    __tablename__ = "taxonomy_version"
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    definition: Mapped[dict[str, Any]] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class PolicyVersionRow(Base):
    """Versioned AI-PEL bundle: policies + application grants + model registry (ADR-0005)."""

    __tablename__ = "policy_version"
    __table_args__ = (UniqueConstraint("tenant_id", "version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    bundle: Mapped[dict[str, Any]] = mapped_column()
    bundle_hash: Mapped[str] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text, default=None)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    published_by: Mapped[str | None] = mapped_column(String(64), default=None)
    published_at: Mapped[datetime | None] = mapped_column(default=None)


# --- Run records (LLD §13, §21.5) -------------------------------------------------------------


class ExecutionRunRow(Base):
    __tablename__ = "execution_run"
    __table_args__ = (
        Index("ix_execution_run_tenant_created", "tenant_id", "created_at"),
        Index("ix_execution_run_app_created", "application_id", "created_at"),
    )
    run_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(32), unique=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    application_id: Mapped[str] = mapped_column(String(64))
    department_id: Mapped[str | None] = mapped_column(String(64))
    cost_center_id: Mapped[str | None] = mapped_column(String(64))
    principal_kind: Mapped[str] = mapped_column(String(16))
    principal_pseudonym: Mapped[str | None] = mapped_column(String(64))
    identity_mode: Mapped[str] = mapped_column(String(16))
    source_mode: Mapped[str] = mapped_column(String(16))
    ingress_source_policy: Mapped[str | None] = mapped_column(String(64))
    scenario_id: Mapped[str | None] = mapped_column(String(64))
    routing_mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[str] = mapped_column(String(32))
    execution_path: Mapped[str] = mapped_column(String(32))
    selected_model: Mapped[str | None] = mapped_column(String(64))
    cim_state: Mapped[str] = mapped_column(String(32))
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    policy_version: Mapped[str | None] = mapped_column(String(32))
    effective_classification: Mapped[str] = mapped_column(String(32))
    environment: Mapped[str] = mapped_column(String(32))
    data_designation: Mapped[str] = mapped_column(String(16))
    provenance: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    # SHA-256 of the answer the run served (never the text): binds a presenter's answer-quality comparison to
    # the served answer (ADR-0023). Null when the run served no answer.
    answer_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    # The mode the run executed in (ADR-0019): `live`, `replay`, `record` or `simulated`. Only a live run can
    # be compared (ADR-0023). Null on runs recorded before the column existed.
    execution_mode: Mapped[str | None] = mapped_column(String(16), default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(default=None)
    latency_ms: Mapped[float | None] = mapped_column(Float, default=None)


class ClassificationEventRow(Base):
    __tablename__ = "classification_event"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("execution_run.run_id"), index=True)
    taxonomy_version: Mapped[str] = mapped_column(String(64))
    classifier_version: Mapped[str] = mapped_column(String(64))
    business_function: Mapped[str | None] = mapped_column(String(32))
    task_category: Mapped[str | None] = mapped_column(String(32))
    intent: Mapped[str | None] = mapped_column(String(64))
    complexity: Mapped[str | None] = mapped_column(String(32))
    sensitivity: Mapped[str] = mapped_column(String(32))
    language: Mapped[str] = mapped_column(String(16))
    confidences: Mapped[dict[str, Any]] = mapped_column(default=dict)
    review_needed: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewed_label: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class PolicyEventRow(Base):
    __tablename__ = "policy_event"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("execution_run.run_id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    stage: Mapped[str] = mapped_column(String(16))
    egress_model_id: Mapped[str | None] = mapped_column(String(64))
    tool_id: Mapped[str | None] = mapped_column(String(64), default=None)
    action: Mapped[str] = mapped_column(String(32))
    policy_version: Mapped[str] = mapped_column(String(32))
    bundle_hash: Mapped[str] = mapped_column(String(64))
    matched_rule_ids: Mapped[list[str]] = mapped_column(default=list)
    reasons: Mapped[list[Any]] = mapped_column(default=list)
    effective: Mapped[dict[str, Any]] = mapped_column(default=dict)
    declared_classification: Mapped[str | None] = mapped_column(String(32))
    dlp_classification: Mapped[str | None] = mapped_column(String(32))
    effective_classification: Mapped[str] = mapped_column(String(32))
    matched_network_id: Mapped[str | None] = mapped_column(String(64))
    reservation_id: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ModelAttemptRow(Base):
    __tablename__ = "model_attempt"
    attempt_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("execution_run.run_id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    stage: Mapped[str] = mapped_column(String(32))
    model_id: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32))
    provider_model_version: Mapped[str] = mapped_column(String(200))
    retry_of: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(32))
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cached_input_tokens: Mapped[int] = mapped_column(default=0)
    reasoning_tokens: Mapped[int] = mapped_column(default=0)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    ttft_ms: Mapped[float | None] = mapped_column(Float)
    provenance: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column()
    completed_at: Mapped[datetime | None] = mapped_column()


class BudgetReservationRow(Base):
    """Durable reservation journal (LLD §12, §13 budget_ledger), written before the first billable call.

    Redis holds the live counters; this table is the system of record they are rebuilt from. `allocations`
    maps each bucket key to {"reserved", "committed"} micro-USD. There is no foreign key to execution_run:
    the reservation is journaled before the run record exists, and an orphan is what reconciliation finds.
    """

    __tablename__ = "budget_reservation"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key"),
        Index("ix_budget_reservation_tenant_status_created", "tenant_id", "status", "created_at"),
        Index("ix_budget_reservation_allocations", "allocations", postgresql_using="gin"),
    )
    reservation_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(128), default=None)
    # The budget approval this reservation consumed; unique, so an approval funds one reservation (ADR-0014).
    approval_id: Mapped[str | None] = mapped_column(String(40), default=None, unique=True)
    scope_chain: Mapped[list[Any]] = mapped_column(default=list)
    lines: Mapped[list[Any]] = mapped_column(default=list)
    allocations: Mapped[dict[str, Any]] = mapped_column(default=dict)
    estimate_micros: Mapped[int] = mapped_column()
    actual_micros: Mapped[int] = mapped_column(default=0)
    released_micros: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(16))
    pricing_version: Mapped[str] = mapped_column(String(64))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    provenance: Mapped[str] = mapped_column(String(16))
    charge_provenance: Mapped[str | None] = mapped_column(String(16), default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    settled_at: Mapped[datetime | None] = mapped_column(default=None)
    reconciled_at: Mapped[datetime | None] = mapped_column(default=None)


class BudgetAdjustmentRow(Base):
    """Append-only corrections to a reservation's committed spend in one bucket, found by reconciliation."""

    __tablename__ = "budget_adjustment"
    __table_args__ = (Index("ix_budget_adjustment_tenant_bucket", "tenant_id", "bucket"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    reservation_id: Mapped[str] = mapped_column(String(40), index=True)
    bucket: Mapped[str] = mapped_column(String(256))
    delta_micros: Mapped[int] = mapped_column()
    reason: Mapped[str] = mapped_column(String(64))
    reconciliation_id: Mapped[str | None] = mapped_column(String(40), default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class BudgetAlertRow(Base):
    """FinOps alerts for the Admin Dashboard (LLD §21.9): thresholds, hard caps and reconciliation."""

    __tablename__ = "budget_alert"
    __table_args__ = (Index("ix_budget_alert_tenant_created", "tenant_id", "created_at"),)
    alert_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    budget_id: Mapped[str | None] = mapped_column(String(200), default=None)
    scope_type: Mapped[str | None] = mapped_column(String(32), default=None)
    scope_id: Mapped[str | None] = mapped_column(String(200), default=None)
    # The application the alert concerns, when it concerns one: row-level scope for owners and department
    # managers (LLD §21.8). Set from application, workflow and request scopes, and by Insights for a user's
    # anomaly within an application (the user stays a pseudonym).
    application_id: Mapped[str | None] = mapped_column(String(64), default=None)
    # Money domain of a budget alert (ADR-0008 amendment): `live`, `illustrative` or `visitor:<pseudonym>`;
    # null for alerts that are not about budget counters (e.g. Insights anomalies).
    domain: Mapped[str | None] = mapped_column(String(64), default=None)
    period: Mapped[str | None] = mapped_column(String(16), default=None)
    period_key: Mapped[str | None] = mapped_column(String(16), default=None)
    threshold_bps: Mapped[int | None] = mapped_column(Integer, default=None)
    cap_micros: Mapped[int | None] = mapped_column(default=None)
    spent_micros: Mapped[int | None] = mapped_column(default=None)
    amount_micros: Mapped[int | None] = mapped_column(default=None)
    reservation_id: Mapped[str | None] = mapped_column(String(40), default=None)
    run_id: Mapped[str | None] = mapped_column(String(40), default=None)
    message: Mapped[str] = mapped_column(Text)
    provenance: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ReconciliationRunRow(Base):
    """One run of the reservation <-> usage ledger reconciliation job."""

    __tablename__ = "reconciliation_run"
    __table_args__ = (Index("ix_reconciliation_run_tenant_started", "tenant_id", "started_at"),)
    reconciliation_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    triggered_by: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    reservations_checked: Mapped[int] = mapped_column(default=0)
    settled: Mapped[int] = mapped_column(default=0)
    expired: Mapped[int] = mapped_column(default=0)
    adjustments: Mapped[int] = mapped_column(default=0)
    counters_corrected: Mapped[int] = mapped_column(default=0)
    findings: Mapped[list[Any]] = mapped_column(default=list)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(default=None)


class UsageLedgerRow(Base):
    """Append-only charges (database trigger rejects UPDATE/DELETE). Corrections are new rows."""

    __tablename__ = "usage_ledger"
    __table_args__ = (Index("ix_usage_ledger_tenant_created", "tenant_id", "created_at"),)
    ledger_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("execution_run.run_id"), index=True)
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey("model_attempt.attempt_id"))
    tenant_id: Mapped[str] = mapped_column(String(64))
    application_id: Mapped[str] = mapped_column(String(64))
    rate_card_id: Mapped[str | None] = mapped_column(String(64))
    rate_card_version: Mapped[str] = mapped_column(String(64))
    charge_type: Mapped[str] = mapped_column(String(32))
    measured_or_estimated: Mapped[str] = mapped_column(String(16))
    amount_micros: Mapped[int] = mapped_column()
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    reservation_id: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    reconciled_at: Mapped[datetime | None] = mapped_column(default=None)


class CacheEventRow(Base):
    __tablename__ = "cache_event"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("execution_run.run_id"), index=True)
    cache_type: Mapped[str] = mapped_column(String(32))
    scope: Mapped[str] = mapped_column(String(200))
    hit: Mapped[bool] = mapped_column(Boolean)
    key_hash: Mapped[str] = mapped_column(String(64))
    source_run_id: Mapped[str | None] = mapped_column(String(40))
    invalidation_reason: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class OptimizationEventRow(Base):
    __tablename__ = "optimization_event"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("execution_run.run_id"), index=True)
    cache_type: Mapped[str | None] = mapped_column(String(32))
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    context_strategy: Mapped[str | None] = mapped_column(String(32))
    context_tokens_before: Mapped[int] = mapped_column(default=0)
    context_tokens_after: Mapped[int] = mapped_column(default=0)
    removed_passages_count: Mapped[int] = mapped_column(default=0)
    preservation_result: Mapped[str | None] = mapped_column(String(32))
    avoided_call: Mapped[bool] = mapped_column(Boolean, default=False)
    modeled_avoided_tokens: Mapped[int] = mapped_column(default=0)
    modeled_avoided_micros: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ContextPackageRow(Base):
    __tablename__ = "context_package"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("execution_run.run_id"), index=True)
    cim_revision: Mapped[str] = mapped_column(String(200))
    context_profile: Mapped[str | None] = mapped_column(String(64))
    evidence_refs: Mapped[list[str]] = mapped_column(default=list)
    retained_ids: Mapped[list[str]] = mapped_column(default=list)
    removed_ids: Mapped[list[str]] = mapped_column(default=list)
    missing_fields: Mapped[list[str]] = mapped_column(default=list)
    validation: Mapped[str] = mapped_column(String(32))
    guard_details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    token_counts: Mapped[dict[str, Any]] = mapped_column(default=dict)
    provenance: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AssuranceResultRow(Base):
    __tablename__ = "assurance_result"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("execution_run.run_id"), index=True)
    passed: Mapped[bool] = mapped_column(Boolean)
    output_dlp: Mapped[str] = mapped_column(String(16))
    checks: Mapped[list[Any]] = mapped_column(default=list)
    evidence_coverage: Mapped[float | None] = mapped_column(Float)
    escalation: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ArtifactRow(Base):
    """A generated document (LLD §13 artifacts). Owner-only: tenant and owner are checked on every read."""

    __tablename__ = "artifact"
    __table_args__ = (Index("ix_artifact_tenant_owner", "tenant_id", "owner_kind", "owner_id"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("execution_run.run_id"), index=True)
    object_ref: Mapped[str] = mapped_column(Text)
    document_type: Mapped[str] = mapped_column(String(16))
    schema_validation: Mapped[str] = mapped_column(String(32))
    figure_validation: Mapped[str] = mapped_column(String(32))
    access_policy: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    tenant_id: Mapped[str] = mapped_column(String(64), default="")
    application_id: Mapped[str] = mapped_column(String(64), default="")
    owner_kind: Mapped[str] = mapped_column(String(16), default="")
    owner_id: Mapped[str] = mapped_column(String(64), default="")
    file_name: Mapped[str] = mapped_column(String(128), default="")
    media_type: Mapped[str] = mapped_column(String(128), default="")
    size_bytes: Mapped[int] = mapped_column(default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="")
    classification: Mapped[str] = mapped_column(String(16), default="")
    provenance: Mapped[str] = mapped_column(String(16), default="")
    generator_version: Mapped[str] = mapped_column(String(32), default="")
    checks: Mapped[dict[str, Any]] = mapped_column(default=dict)


class ArtifactBlobRow(Base):
    """The document bytes. Space filesystems are ephemeral, so content lives in Postgres (POC)."""

    __tablename__ = "artifact_blob"
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifact.id"), primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary)


class AgentWorkflowRow(Base):
    """An agent workflow (LLD §15 #6). Its model steps are `execution_run` rows sharing `workflow_id`."""

    __tablename__ = "agent_workflow"
    __table_args__ = (Index("ix_agent_workflow_tenant_created", "tenant_id", "created_at"),)
    workflow_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    application_id: Mapped[str] = mapped_column(String(64))
    department_id: Mapped[str | None] = mapped_column(String(64))
    principal_kind: Mapped[str] = mapped_column(String(16))
    principal_pseudonym: Mapped[str | None] = mapped_column(String(64))
    agent: Mapped[str] = mapped_column(String(32))
    definition_version: Mapped[str] = mapped_column(String(32))
    incident_id: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32))
    provenance: Mapped[str] = mapped_column(String(16))
    data_designation: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column()


class AgentStepRow(Base):
    """One workflow step. Tool output and model text are never stored, only their hashes."""

    __tablename__ = "agent_step"
    __table_args__ = (UniqueConstraint("workflow_id", "step_index"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("agent_workflow.workflow_id"), index=True)
    step_index: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(8))
    name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(String(255))
    run_id: Mapped[str | None] = mapped_column(String(40), index=True)
    tool_id: Mapped[str | None] = mapped_column(String(64))
    tool_access: Mapped[str | None] = mapped_column(String(8))
    tool_egress: Mapped[str | None] = mapped_column(String(16))
    arguments: Mapped[dict[str, Any]] = mapped_column(default=dict)
    policy_action: Mapped[str | None] = mapped_column(String(32))
    rule_ids: Mapped[list[str]] = mapped_column(default=list)
    approval_id: Mapped[str | None] = mapped_column(String(40))
    executed: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence_id: Mapped[str | None] = mapped_column(String(200))
    output_sha256: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column()
    completed_at: Mapped[datetime] = mapped_column()


class TraceSpanRow(Base):
    """Redacted spans for the Trace Explorer. Attributes are allowlisted; no prompt text."""

    __tablename__ = "trace_span"
    span_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(32), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("execution_run.run_id"), index=True)
    parent_span_id: Mapped[str | None] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column()
    duration_ms: Mapped[float] = mapped_column(Float)
    attributes: Mapped[dict[str, Any]] = mapped_column(default=dict)


class AuditLogRow(Base):
    """Append-only audit trail (database trigger rejects UPDATE/DELETE)."""

    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    actor_kind: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class IdempotencyRecordRow(Base):
    """Durable key-to-run mapping for audit and double-charge prevention; replays live in Redis (24h TTL)."""

    __tablename__ = "idempotency_record"
    __table_args__ = (UniqueConstraint("tenant_id", "application_id", "idempotency_key"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    application_id: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ApprovalRequestRow(Base):
    """Approval Queue (ADR-0012). Holds policy facts for re-evaluation, never prompt or evidence text.

    Approval is bound to the original request hash, application and requester; it is single-use.
    """

    __tablename__ = "approval_request"
    __table_args__ = (Index("ix_approval_request_tenant_status", "tenant_id", "status"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("execution_run.run_id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    application_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), default="policy")
    tool_id: Mapped[str | None] = mapped_column(String(64), default=None)
    # Tool approvals: the recorded call (canonical JSON arguments hash) the approval may execute.
    arguments_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    rule_ids: Mapped[list[str]] = mapped_column(default=list)
    reasons: Mapped[list[Any]] = mapped_column(default=list)
    status: Mapped[str] = mapped_column(String(16))
    request_hash: Mapped[str] = mapped_column(String(64), default="")
    principal_kind: Mapped[str] = mapped_column(String(16), default="application")
    principal_pseudonym: Mapped[str | None] = mapped_column(String(64), default=None)
    policy_input: Mapped[dict[str, Any]] = mapped_column(default=dict)
    policy_version_at_request: Mapped[str] = mapped_column(String(32))
    bundle_hash_at_request: Mapped[str] = mapped_column(String(64), default="")
    requested_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(default=None)
    decided_by: Mapped[str | None] = mapped_column(String(64), default=None)
    decision_note: Mapped[str | None] = mapped_column(Text, default=None)
    approved_grants: Mapped[list[Any]] = mapped_column(default=list)
    approved_amount_micros: Mapped[int | None] = mapped_column(default=None)
    policy_version_at_decision: Mapped[str | None] = mapped_column(String(32), default=None)
    bundle_hash_at_decision: Mapped[str | None] = mapped_column(String(64), default=None)
    consumed_by_run_id: Mapped[str | None] = mapped_column(String(40), default=None)
    consumed_at: Mapped[datetime | None] = mapped_column(default=None)


# --- AiVa Insights aggregates (LLD §21.2, §21.5; ADR-0015) ------------------------------------------


class ReportLineColumns:
    """Dimensions and measures shared by the hourly and daily aggregates (ADR-0015).

    A run contributes one `run` line (request counts and run-level outcomes), one `attempt` line per model
    attempt (tokens, ledger charges, latency) and one `adjustment` line per ledger correction. Measures of
    different line kinds never overlap, so any grouping sums exactly to the attempt-level ledger. Usage is
    attributed to the UTC hour the run started. `line_key` is a hash of every dimension column. No prompt
    text, raw identity or subscriber data is stored: users appear only as pseudonyms.
    """

    tenant_id: Mapped[str] = mapped_column(String(64))
    line_key: Mapped[str] = mapped_column(String(32))
    # Run dimensions
    application_id: Mapped[str] = mapped_column(String(64))
    department_id: Mapped[str] = mapped_column(String(64))
    cost_center_id: Mapped[str] = mapped_column(String(64))
    principal_kind: Mapped[str] = mapped_column(String(16))
    principal: Mapped[str] = mapped_column(String(80))
    business_function: Mapped[str] = mapped_column(String(32))
    task_category: Mapped[str] = mapped_column(String(32))
    intent: Mapped[str] = mapped_column(String(64))
    complexity: Mapped[str] = mapped_column(String(32))
    sensitivity: Mapped[str] = mapped_column(String(32))
    language: Mapped[str] = mapped_column(String(16))
    classification_status: Mapped[str] = mapped_column(String(16))
    taxonomy_version: Mapped[str] = mapped_column(String(64))
    classifier_version: Mapped[str] = mapped_column(String(64))
    execution_path: Mapped[str] = mapped_column(String(32))
    context_strategy: Mapped[str] = mapped_column(String(32))
    cim_state: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[str] = mapped_column(String(32))
    source_policy: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(32))
    deny_rule_id: Mapped[str] = mapped_column(String(200))
    environment: Mapped[str] = mapped_column(String(32))
    data_designation: Mapped[str] = mapped_column(String(16))
    # Line dimensions
    line_kind: Mapped[str] = mapped_column(String(16))
    model_id: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32))
    stage: Mapped[str] = mapped_column(String(32))
    provenance: Mapped[str] = mapped_column(String(16))
    rate_card_version: Mapped[str] = mapped_column(String(64))
    # Run-line measures
    request_count: Mapped[int] = mapped_column(default=0)
    cache_hit_count: Mapped[int] = mapped_column(default=0)
    avoided_call_count: Mapped[int] = mapped_column(default=0)
    avoided_tokens: Mapped[int] = mapped_column(default=0)
    avoided_micros: Mapped[int] = mapped_column(default=0)
    context_tokens_before: Mapped[int] = mapped_column(default=0)
    context_tokens_after: Mapped[int] = mapped_column(default=0)
    removed_passages: Mapped[int] = mapped_column(default=0)
    passages_retrieved: Mapped[int] = mapped_column(default=0)
    passages_kept: Mapped[int] = mapped_column(default=0)
    quality_checked_count: Mapped[int] = mapped_column(default=0)
    quality_passed_count: Mapped[int] = mapped_column(default=0)
    reserved_micros: Mapped[int] = mapped_column(default=0)
    committed_micros: Mapped[int] = mapped_column(default=0)
    run_latency_ms_sum: Mapped[float] = mapped_column(Float, default=0.0)
    approval_exception_count: Mapped[int] = mapped_column(default=0)
    confidential_external_attempts: Mapped[int] = mapped_column(default=0)
    confidential_cim_attempts: Mapped[int] = mapped_column(default=0)
    confidential_comparison_attempts: Mapped[int] = mapped_column(default=0)
    frontier_baseline_micros: Mapped[int] = mapped_column(default=0)
    savings_cache_micros: Mapped[int] = mapped_column(default=0)
    savings_context_micros: Mapped[int] = mapped_column(default=0)
    savings_routing_micros: Mapped[int] = mapped_column(default=0)
    savings_prompt_cache_micros: Mapped[int] = mapped_column(default=0)
    baseline_runs: Mapped[int] = mapped_column(default=0)
    baseline_micros: Mapped[int] = mapped_column(default=0)
    baseline_actual_micros: Mapped[int] = mapped_column(default=0)
    baseline_passed_runs: Mapped[int] = mapped_column(default=0)
    baseline_passed_savings_micros: Mapped[int] = mapped_column(default=0)
    priced_request_count: Mapped[int] = mapped_column(default=0)
    priced_api_micros: Mapped[int] = mapped_column(default=0)
    priced_frontier_micros: Mapped[int] = mapped_column(default=0)
    optimizable_tokens: Mapped[int] = mapped_column(default=0)
    cache_tokens_saved: Mapped[int] = mapped_column(default=0)
    context_tokens_saved: Mapped[int] = mapped_column(default=0)
    # Attempt- and adjustment-line measures
    attempt_count: Mapped[int] = mapped_column(default=0)
    retry_count: Mapped[int] = mapped_column(default=0)
    failed_attempt_count: Mapped[int] = mapped_column(default=0)
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cached_input_tokens: Mapped[int] = mapped_column(default=0)
    reasoning_tokens: Mapped[int] = mapped_column(default=0)
    retry_input_tokens: Mapped[int] = mapped_column(default=0)
    retry_output_tokens: Mapped[int] = mapped_column(default=0)
    api_micros: Mapped[int] = mapped_column(default=0)
    retry_micros: Mapped[int] = mapped_column(default=0)
    gpu_estimated_micros: Mapped[int] = mapped_column(default=0)
    attempt_latency_ms_sum: Mapped[float] = mapped_column(Float, default=0.0)
    attempt_latency_count: Mapped[int] = mapped_column(default=0)
    refreshed_at: Mapped[datetime] = mapped_column(default=utcnow)


class ReportAggregateHourlyRow(ReportLineColumns, Base):
    __tablename__ = "report_aggregate_hourly"
    __table_args__ = (PrimaryKeyConstraint("tenant_id", "bucket_start", "line_key"),)
    bucket_start: Mapped[datetime] = mapped_column()


class ReportAggregateDailyRow(ReportLineColumns, Base):
    __tablename__ = "report_aggregate_daily"
    __table_args__ = (PrimaryKeyConstraint("tenant_id", "day", "line_key"),)
    day: Mapped[date] = mapped_column(Date)


class ReportRefreshRow(Base):
    """One run of the Insights aggregation worker: which hours and days it rebuilt (ADR-0015)."""

    __tablename__ = "report_refresh"
    __table_args__ = (Index("ix_report_refresh_tenant_started", "tenant_id", "started_at"),)
    refresh_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    triggered_by: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    window_start: Mapped[datetime | None] = mapped_column(default=None)
    window_end: Mapped[datetime | None] = mapped_column(default=None)
    hours_refreshed: Mapped[int] = mapped_column(default=0)
    days_refreshed: Mapped[int] = mapped_column(default=0)
    runs_counted: Mapped[int] = mapped_column(default=0)
    lines_written: Mapped[int] = mapped_column(default=0)
    alerts_raised: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(default=None)


class ReportExportRow(Base):
    """An asynchronous CSV export (LLD §21.6, §22.6). The file expires; it never holds prompt text or PII."""

    __tablename__ = "report_export"
    __table_args__ = (Index("ix_report_export_tenant_requester", "tenant_id", "requester_pseudonym"),)
    export_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    requester_pseudonym: Mapped[str] = mapped_column(String(64))
    report: Mapped[str] = mapped_column(String(32))
    group_by: Mapped[str] = mapped_column(String(32))
    window_from: Mapped[date] = mapped_column(Date)
    window_to: Mapped[date] = mapped_column(Date)
    filters: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # The requester's row-level scope when the export was requested (roles, department, owned applications).
    scope: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(16))
    row_count: Mapped[int | None] = mapped_column(default=None)
    content: Mapped[str | None] = mapped_column(Text, default=None)
    content_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(default=None)
    expires_at: Mapped[datetime] = mapped_column()


# --- Knowledge base for retrieval (LLD §3, §11; Optimize & Inference) ---------------------------


class KnowledgeDocumentRow(Base):
    """A synthetic knowledge-base document. Retrieval only returns public/internal, DLP-clean content."""

    __tablename__ = "kb_document"
    __table_args__ = (UniqueConstraint("tenant_id", "document_id"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))
    document_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(300))
    classification: Mapped[str] = mapped_column(String(32))
    # Applications allowed to retrieve the document; empty = every application of the tenant.
    application_ids: Mapped[list[str]] = mapped_column(default=list)
    version: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    effective_from: Mapped[date | None] = mapped_column(Date, default=None)
    expires_on: Mapped[date | None] = mapped_column(Date, default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class KnowledgeChunkRow(Base):
    __tablename__ = "kb_chunk"
    __table_args__ = (
        UniqueConstraint("tenant_id", "evidence_id"),
        Index(
            "ix_kb_chunk_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    document_pk: Mapped[int] = mapped_column(ForeignKey("kb_document.id", ondelete="CASCADE"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    evidence_id: Mapped[str] = mapped_column(String(160))
    ordinal: Mapped[int] = mapped_column(Integer)
    section: Mapped[str] = mapped_column(String(300))
    text: Mapped[str] = mapped_column(Text)
    token_estimate: Mapped[int] = mapped_column(default=0)
    dlp_finding_types: Mapped[list[str]] = mapped_column(default=list)
    # Prompt-injection indicator names from the ingest scan, and the detector version that produced them.
    # NULL, or another detector version than the running one, means not screened: never retrievable.
    injection_indicators: Mapped[list[str] | None] = mapped_column(default=None)
    injection_detector: Mapped[str | None] = mapped_column(String(64), default=None)
    embedding_model: Mapped[str] = mapped_column(String(200))
    embedding: Mapped[Any] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
