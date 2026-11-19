"""Alert money domain: the budget domain an alert is about (ADR-0008 amendment, ADR-0019).

`live` counts provider spend, `illustrative` replayed and simulated runs, and `visitor:<pseudonym>` a public
visitor's own illustrative runs. Existing budget alerts were all raised by simulated runs, so they are
backfilled as `illustrative`; alerts that are not about a budget (Insights anomalies) stay NULL.

Revision ID: fin_0004
Revises: fin_0003
Create Date: 2026-10-15 18:40:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fin_0004"
down_revision: str | None = "fin_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("budget_alert", sa.Column("domain", sa.String(length=64), nullable=True))
    op.execute(
        "UPDATE budget_alert SET domain = 'illustrative' WHERE kind IN ("
        "'threshold', 'hard_cap', 'reservation_overrun', 'reservation_expired', "
        "'reconciliation_adjustment', 'counter_corrected', 'approved_exception')"
    )


def downgrade() -> None:
    op.drop_column("budget_alert", "domain")
