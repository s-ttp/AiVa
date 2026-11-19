"""Answer-quality comparison calls on confidential runs, on the Insights aggregates (ADR-0023, ADR-0015).

One run-line measure on the hourly and daily aggregates: the comparison calls that left a confidential run
under ADR-0023's exception for scenario 11's synthetic sample. They are reported apart from
`confidential_external_attempts`, which stays the served path's leak count; a comparison call on any other
confidential run still counts there. Existing rows get 0, which is exact: no comparison ran before this.

Revision ID: ins_0003
Revises: opt_0002
Create Date: 2026-10-21 07:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ins_0003"
down_revision: str | None = "opt_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("report_aggregate_hourly", "report_aggregate_daily")
_COLUMN = "confidential_comparison_attempts"


def upgrade() -> None:
    for table in _TABLES:
        # The default fills existing rows only; the gateway writes every measure explicitly.
        op.add_column(table, sa.Column(_COLUMN, sa.BigInteger(), nullable=False, server_default="0"))
        op.alter_column(table, _COLUMN, server_default=None)


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, _COLUMN)
