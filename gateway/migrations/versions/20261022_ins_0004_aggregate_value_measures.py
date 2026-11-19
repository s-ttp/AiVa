"""The Story mode's hero figures on the Insights aggregates (ADR-0015).

Six run-line measures on the hourly and daily aggregates, one run's part of the /live "This session"
counters (`reporting.lines.value_measures`): the requests priced both ways, AiVa's model, CIM and tool-call
spend on them and their direct-frontier estimate, and the tokens the answering models would have read and
written, with those caching and context optimization spared. Existing rows get 0 until their window is
re-aggregated (`aiva-admin aggregate --from … --to …`), which rebuilds them from the run records.

Revision ID: ins_0004
Revises: ins_0003
Create Date: 2026-10-22 06:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ins_0004"
down_revision: str | None = "ins_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("report_aggregate_hourly", "report_aggregate_daily")
_COLUMNS = (
    "priced_request_count",
    "priced_api_micros",
    "priced_frontier_micros",
    "optimizable_tokens",
    "cache_tokens_saved",
    "context_tokens_saved",
)


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
