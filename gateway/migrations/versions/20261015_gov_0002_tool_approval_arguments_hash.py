"""Bind tool approvals to the recorded call's arguments hash (ADR-0012 amendment).

Revision ID: gov_0002
Revises: fin_0002
Create Date: 2026-10-15 05:30:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "gov_0002"
down_revision: str | None = "fin_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("approval_request", sa.Column("arguments_sha256", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("approval_request", "arguments_sha256")
