"""Passage counts on the Insights aggregates (Story mode: "passages kept of retrieved").

Two run-line measures on the hourly and daily aggregates: of the passages the CIM was given, how many its
package kept, on runs that sent the package (ADR-0015). Existing rows get 0 until their window is
re-aggregated (`aiva-admin aggregate --from … --to …`), which rebuilds them from the run records.

Revision ID: ins_0002
Revises: gov_0003
Create Date: 2026-10-19 09:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ins_0002"
down_revision: str | None = "gov_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("report_aggregate_hourly", "report_aggregate_daily")
_COLUMNS = ("passages_retrieved", "passages_kept")


def upgrade() -> None:
    for table in _TABLES:
        for column in _COLUMNS:
            # The default fills existing rows only; the gateway writes every measure explicitly.
            op.add_column(table, sa.Column(column, sa.BigInteger(), nullable=False, server_default="0"))
            op.alter_column(table, column, server_default=None)


def downgrade() -> None:
    for table in _TABLES:
        for column in _COLUMNS:
            op.drop_column(table, column)
