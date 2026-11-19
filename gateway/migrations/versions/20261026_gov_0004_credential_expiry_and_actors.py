"""Application keys from the admin console (ADR-0027): expiry, issuing and revoking actor, last use.

Existing keys (made with `aiva-admin create-credential`) keep no expiry and are attributed to `cli`.

Revision ID: gov_0004
Revises: ins_0004
Create Date: 2026-10-26 06:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "gov_0004"
down_revision: str | None = "ins_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "application_credential",
        sa.Column("created_by", sa.String(length=64), nullable=False, server_default="cli"),
    )
    op.alter_column("application_credential", "created_by", server_default=None)
    op.add_column("application_credential", sa.Column("revoked_by", sa.String(length=64), nullable=True))
    op.add_column(
        "application_credential", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "application_credential", sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("application_credential", "last_used_at")
    op.drop_column("application_credential", "expires_at")
    op.drop_column("application_credential", "revoked_by")
    op.drop_column("application_credential", "created_by")
