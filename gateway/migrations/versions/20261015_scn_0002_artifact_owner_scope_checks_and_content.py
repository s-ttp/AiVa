"""Artifacts (LLD §15 #10): tenant and owner scope, file metadata, document checks, and the content blob.

Existing rows (none are written before this revision) get empty defaults.

Revision ID: scn_0002
Revises: ins_0001
Create Date: 2026-10-15 05:21:56.398887+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "scn_0002"
down_revision: str | None = "ins_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artifact_blob",
        sa.Column("artifact_id", sa.String(length=40), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(
            ["artifact_id"], ["artifact.id"], name=op.f("fk_artifact_blob_artifact_id_artifact")
        ),
        sa.PrimaryKeyConstraint("artifact_id", name=op.f("pk_artifact_blob")),
    )
    op.add_column("artifact", sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default=""))
    op.add_column(
        "artifact", sa.Column("application_id", sa.String(length=64), nullable=False, server_default="")
    )
    op.add_column(
        "artifact", sa.Column("owner_kind", sa.String(length=16), nullable=False, server_default="")
    )
    op.add_column("artifact", sa.Column("owner_id", sa.String(length=64), nullable=False, server_default=""))
    op.add_column(
        "artifact", sa.Column("file_name", sa.String(length=128), nullable=False, server_default="")
    )
    op.add_column(
        "artifact", sa.Column("media_type", sa.String(length=128), nullable=False, server_default="")
    )
    op.add_column("artifact", sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("artifact", sa.Column("sha256", sa.String(length=64), nullable=False, server_default=""))
    op.add_column("artifact", sa.Column("status", sa.String(length=16), nullable=False, server_default=""))
    op.add_column(
        "artifact", sa.Column("classification", sa.String(length=16), nullable=False, server_default="")
    )
    op.add_column(
        "artifact", sa.Column("provenance", sa.String(length=16), nullable=False, server_default="")
    )
    op.add_column(
        "artifact", sa.Column("generator_version", sa.String(length=32), nullable=False, server_default="")
    )
    op.add_column(
        "artifact",
        sa.Column("checks", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
    )
    op.create_index(
        "ix_artifact_tenant_owner", "artifact", ["tenant_id", "owner_kind", "owner_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_artifact_tenant_owner", table_name="artifact")
    op.drop_column("artifact", "checks")
    op.drop_column("artifact", "generator_version")
    op.drop_column("artifact", "provenance")
    op.drop_column("artifact", "classification")
    op.drop_column("artifact", "status")
    op.drop_column("artifact", "sha256")
    op.drop_column("artifact", "size_bytes")
    op.drop_column("artifact", "media_type")
    op.drop_column("artifact", "file_name")
    op.drop_column("artifact", "owner_id")
    op.drop_column("artifact", "owner_kind")
    op.drop_column("artifact", "application_id")
    op.drop_column("artifact", "tenant_id")
    op.drop_table("artifact_blob")
