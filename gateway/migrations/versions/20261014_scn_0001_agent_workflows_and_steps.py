"""Agent workflows and steps (LLD §15 #6): workflow and step records, hashes only, never tool text.

Revision ID: scn_0001
Revises: gov_0002
Create Date: 2026-10-14 13:31:00.065360+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "scn_0001"
down_revision: str | None = "gov_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_workflow",
        sa.Column("workflow_id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("application_id", sa.String(length=64), nullable=False),
        sa.Column("department_id", sa.String(length=64), nullable=True),
        sa.Column("principal_kind", sa.String(length=16), nullable=False),
        sa.Column("principal_pseudonym", sa.String(length=64), nullable=True),
        sa.Column("agent", sa.String(length=32), nullable=False),
        sa.Column("definition_version", sa.String(length=32), nullable=False),
        sa.Column("incident_id", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provenance", sa.String(length=16), nullable=False),
        sa.Column("data_designation", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("workflow_id", name=op.f("pk_agent_workflow")),
    )
    op.create_index(
        "ix_agent_workflow_tenant_created", "agent_workflow", ["tenant_id", "created_at"], unique=False
    )
    op.create_table(
        "agent_step",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("workflow_id", sa.String(length=40), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("message", sa.String(length=255), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=True),
        sa.Column("tool_id", sa.String(length=64), nullable=True),
        sa.Column("tool_access", sa.String(length=8), nullable=True),
        sa.Column("tool_egress", sa.String(length=16), nullable=True),
        sa.Column("arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("policy_action", sa.String(length=32), nullable=True),
        sa.Column("rule_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("approval_id", sa.String(length=40), nullable=True),
        sa.Column("executed", sa.Boolean(), nullable=False),
        sa.Column("evidence_id", sa.String(length=200), nullable=True),
        sa.Column("output_sha256", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["agent_workflow.workflow_id"],
            name=op.f("fk_agent_step_workflow_id_agent_workflow"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_step")),
        sa.UniqueConstraint("workflow_id", "step_index", name=op.f("uq_agent_step_workflow_id")),
    )
    op.create_index(op.f("ix_agent_step_run_id"), "agent_step", ["run_id"], unique=False)
    op.create_index(op.f("ix_agent_step_workflow_id"), "agent_step", ["workflow_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_agent_step_workflow_id"), table_name="agent_step")
    op.drop_index(op.f("ix_agent_step_run_id"), table_name="agent_step")
    op.drop_table("agent_step")
    op.drop_index("ix_agent_workflow_tenant_created", table_name="agent_workflow")
    op.drop_table("agent_workflow")
