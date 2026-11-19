"""Alert application scope: the application a budget alert concerns, for row-level visibility (LLD §21.8).

Existing application, request and workflow alerts are backfilled from their scope; other scopes stay NULL
(visible to tenant-wide roles, as before).

Revision ID: fin_0003
Revises: scn_0002
Create Date: 2026-10-15 06:20:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fin_0003"
down_revision: str | None = "scn_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("budget_alert", sa.Column("application_id", sa.String(length=64), nullable=True))
    op.execute(
        "UPDATE budget_alert SET application_id = scope_id WHERE scope_type IN ('application', 'request')"
    )
    op.execute(
        "UPDATE budget_alert SET application_id = split_part(scope_id, '/', 1) "
        "WHERE scope_type = 'workflow' AND position('/' in scope_id) > 0"
    )


def downgrade() -> None:
    op.drop_column("budget_alert", "application_id")
