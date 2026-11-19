"""AiVa Insights: hourly and daily aggregates at line grain, the refresh log and CSV export jobs (ADR-0015).

report_aggregate_daily held no data (nothing wrote it before the Insights worker) and is derived from the
run records, so it is dropped and recreated at the new grain; the worker rebuilds it from the ledger.

Revision ID: ins_0001
Revises: scn_0001
Create Date: 2026-10-14 13:28:50.460483+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ins_0001"
down_revision: str | None = "scn_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIMENSIONS = (
    ("application_id", 64),
    ("department_id", 64),
    ("cost_center_id", 64),
    ("principal_kind", 16),
    ("principal", 80),
    ("business_function", 32),
    ("task_category", 32),
    ("intent", 64),
    ("complexity", 32),
    ("sensitivity", 32),
    ("language", 16),
    ("classification_status", 16),
    ("taxonomy_version", 64),
    ("classifier_version", 64),
    ("execution_path", 32),
    ("context_strategy", 32),
    ("cim_state", 32),
    ("outcome", 32),
    ("source_policy", 64),
    ("policy_version", 32),
    ("deny_rule_id", 200),
    ("environment", 32),
    ("data_designation", 16),
    ("line_kind", 16),
    ("model_id", 64),
    ("provider", 32),
    ("stage", 16),
    ("provenance", 16),
    ("rate_card_version", 64),
)
_RUN_MEASURES = (
    "request_count",
    "cache_hit_count",
    "avoided_call_count",
    "avoided_tokens",
    "avoided_micros",
    "context_tokens_before",
    "context_tokens_after",
    "removed_passages",
    "quality_checked_count",
    "quality_passed_count",
    "reserved_micros",
    "committed_micros",
    "run_latency_ms_sum",
    "approval_exception_count",
    "confidential_external_attempts",
    "confidential_cim_attempts",
    "frontier_baseline_micros",
    "savings_cache_micros",
    "savings_context_micros",
    "savings_routing_micros",
    "savings_prompt_cache_micros",
    "baseline_runs",
    "baseline_micros",
    "baseline_actual_micros",
    "baseline_passed_runs",
    "baseline_passed_savings_micros",
)
_ATTEMPT_MEASURES = (
    "attempt_count",
    "retry_count",
    "failed_attempt_count",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
    "retry_input_tokens",
    "retry_output_tokens",
    "api_micros",
    "retry_micros",
    "gpu_estimated_micros",
    "attempt_latency_ms_sum",
    "attempt_latency_count",
)


def _line_columns() -> list[sa.Column]:
    columns: list[sa.Column] = [
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("line_key", sa.String(length=32), nullable=False),
    ]
    columns += [sa.Column(name, sa.String(length=length), nullable=False) for name, length in _DIMENSIONS]
    for name in _RUN_MEASURES + _ATTEMPT_MEASURES:
        kind = sa.Float() if name.endswith("_ms_sum") else sa.BigInteger()
        columns.append(sa.Column(name, kind, nullable=False))
    columns.append(sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=False))
    return columns


def upgrade() -> None:
    op.drop_table("report_aggregate_daily")
    op.create_table(
        "report_aggregate_hourly",
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        *_line_columns(),
        sa.PrimaryKeyConstraint(
            "tenant_id", "bucket_start", "line_key", name=op.f("pk_report_aggregate_hourly")
        ),
    )
    op.create_table(
        "report_aggregate_daily",
        sa.Column("day", sa.Date(), nullable=False),
        *_line_columns(),
        sa.PrimaryKeyConstraint("tenant_id", "day", "line_key", name=op.f("pk_report_aggregate_daily")),
    )
    op.create_table(
        "report_refresh",
        sa.Column("refresh_id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("triggered_by", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hours_refreshed", sa.BigInteger(), nullable=False),
        sa.Column("days_refreshed", sa.BigInteger(), nullable=False),
        sa.Column("runs_counted", sa.BigInteger(), nullable=False),
        sa.Column("lines_written", sa.BigInteger(), nullable=False),
        sa.Column("alerts_raised", sa.BigInteger(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("refresh_id", name=op.f("pk_report_refresh")),
    )
    op.create_index(
        "ix_report_refresh_tenant_started", "report_refresh", ["tenant_id", "started_at"], unique=False
    )
    op.create_table(
        "report_export",
        sa.Column("export_id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("requester_pseudonym", sa.String(length=64), nullable=False),
        sa.Column("report", sa.String(length=32), nullable=False),
        sa.Column("group_by", sa.String(length=32), nullable=False),
        sa.Column("window_from", sa.Date(), nullable=False),
        sa.Column("window_to", sa.Date(), nullable=False),
        sa.Column("filters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("scope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("export_id", name=op.f("pk_report_export")),
    )
    op.create_index(
        "ix_report_export_tenant_requester",
        "report_export",
        ["tenant_id", "requester_pseudonym"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_report_export_tenant_requester", table_name="report_export")
    op.drop_table("report_export")
    op.drop_index("ix_report_refresh_tenant_started", table_name="report_refresh")
    op.drop_table("report_refresh")
    op.drop_table("report_aggregate_daily")
    op.drop_table("report_aggregate_hourly")
    op.create_table(
        "report_aggregate_daily",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("application_id", sa.String(length=64), nullable=False),
        sa.Column("department_id", sa.String(length=64), nullable=False),
        sa.Column("task_category", sa.String(length=32), nullable=False),
        sa.Column("model_id", sa.String(length=64), nullable=False),
        sa.Column("execution_path", sa.String(length=32), nullable=False),
        sa.Column("data_designation", sa.String(length=16), nullable=False),
        sa.Column("request_count", sa.BigInteger(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cim_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cached_tokens", sa.BigInteger(), nullable=False),
        sa.Column("api_spend_micros", sa.BigInteger(), nullable=False),
        sa.Column("gpu_estimated_micros", sa.BigInteger(), nullable=False),
        sa.Column("avoided_micros", sa.BigInteger(), nullable=False),
        sa.Column("cache_hits", sa.BigInteger(), nullable=False),
        sa.Column("denied_count", sa.BigInteger(), nullable=False),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "day",
            "tenant_id",
            "application_id",
            "department_id",
            "task_category",
            "model_id",
            "execution_path",
            "data_designation",
            name=op.f("pk_report_aggregate_daily"),
        ),
    )
