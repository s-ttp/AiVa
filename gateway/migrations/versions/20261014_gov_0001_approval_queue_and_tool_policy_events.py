"""Approval Queue binding and decision columns; tool ID on policy events (ADR-0012).

Existing approval requests (P0) have no request binding, so they are backfilled as expired.

Revision ID: gov_0001
Revises: opt_0001
Create Date: 2026-10-14 09:29:58.907409+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "gov_0001"
down_revision: str | None = "opt_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())

# Backfill values for rows created before this revision; dropped once the columns exist.
_BACKFILLED = {
    "kind": (sa.String(length=16), sa.text("'policy'")),
    "reasons": (_JSONB, sa.text("'[]'::jsonb")),
    "request_hash": (sa.String(length=64), sa.text("''")),
    "principal_kind": (sa.String(length=16), sa.text("'application'")),
    "policy_input": (_JSONB, sa.text("'{}'::jsonb")),
    "bundle_hash_at_request": (sa.String(length=64), sa.text("''")),
    "expires_at": (sa.DateTime(timezone=True), sa.text("now()")),
    "approved_grants": (_JSONB, sa.text("'[]'::jsonb")),
}


def upgrade() -> None:
    for name, (type_, default) in _BACKFILLED.items():
        op.add_column("approval_request", sa.Column(name, type_, nullable=False, server_default=default))
        op.alter_column("approval_request", name, server_default=None)
    op.add_column("approval_request", sa.Column("tool_id", sa.String(length=64), nullable=True))
    op.add_column("approval_request", sa.Column("principal_pseudonym", sa.String(length=64), nullable=True))
    op.add_column("approval_request", sa.Column("decision_note", sa.Text(), nullable=True))
    op.add_column(
        "approval_request", sa.Column("policy_version_at_decision", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "approval_request", sa.Column("bundle_hash_at_decision", sa.String(length=64), nullable=True)
    )
    op.add_column("approval_request", sa.Column("consumed_by_run_id", sa.String(length=40), nullable=True))
    op.add_column("approval_request", sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("approval_request", sa.Column("approved_amount_micros", sa.BigInteger(), nullable=True))
    op.execute("UPDATE approval_request SET status = 'expired' WHERE status = 'pending'")
    op.create_index(
        "ix_approval_request_tenant_status", "approval_request", ["tenant_id", "status"], unique=False
    )
    op.add_column("policy_event", sa.Column("tool_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("policy_event", "tool_id")
    op.drop_index("ix_approval_request_tenant_status", table_name="approval_request")
    for name in (
        "approved_amount_micros",
        "consumed_at",
        "consumed_by_run_id",
        "bundle_hash_at_decision",
        "policy_version_at_decision",
        "decision_note",
        "principal_pseudonym",
        "tool_id",
        *reversed(_BACKFILLED),
    ):
        op.drop_column("approval_request", name)
