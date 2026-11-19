"""Budget approval single use: the approval a reservation consumed, unique per reservation (ADR-0014).

Revision ID: fin_0002
Revises: gov_0001
Create Date: 2026-10-14 11:41:29.858971+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fin_0002"
down_revision: str | None = "gov_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("budget_reservation", sa.Column("approval_id", sa.String(length=40), nullable=True))
    op.create_unique_constraint(
        op.f("uq_budget_reservation_approval_id"), "budget_reservation", ["approval_id"]
    )


def downgrade() -> None:
    op.drop_constraint(op.f("uq_budget_reservation_approval_id"), "budget_reservation", type_="unique")
    op.drop_column("budget_reservation", "approval_id")
