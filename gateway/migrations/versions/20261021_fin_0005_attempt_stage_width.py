"""Attempt stage width for the answer-quality comparison (ADR-0023).

The comparison's model attempts have stages `quality_reference` and `quality_grading`, which are longer than
the 16 characters `model_attempt.stage` allowed. The Insights aggregates copy an attempt's stage into their
`stage` dimension, so it widens with it. Widening keeps every existing value and every aggregate `line_key`.
A downgrade fails while a longer stage is stored, rather than truncating it.

Revision ID: fin_0005
Revises: ins_0002
Create Date: 2026-10-21 06:30:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fin_0005"
down_revision: str | None = "ins_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("model_attempt", "report_aggregate_hourly", "report_aggregate_daily")


def upgrade() -> None:
    for table in _TABLES:
        op.alter_column(table, "stage", type_=sa.String(length=32), existing_type=sa.String(length=16))


def downgrade() -> None:
    for table in _TABLES:
        op.alter_column(table, "stage", type_=sa.String(length=16), existing_type=sa.String(length=32))
