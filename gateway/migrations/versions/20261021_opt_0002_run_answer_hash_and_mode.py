"""The served answer's hash and the run's execution mode on each run record (ADR-0023).

A presenter's answer-quality comparison resends the request and the served answer; the gateway checks them
against `request_hash` and `answer_sha256` before calling anything, and compares only runs served live
(`execution_mode`). Both are written when the run is recorded; earlier runs keep nulls and cannot be compared.

Revision ID: opt_0002
Revises: fin_0005
Create Date: 2026-10-21 07:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "opt_0002"
down_revision: str | None = "fin_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("execution_run", sa.Column("answer_sha256", sa.String(length=64), nullable=True))
    op.add_column("execution_run", sa.Column("execution_mode", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("execution_run", "execution_mode")
    op.drop_column("execution_run", "answer_sha256")
