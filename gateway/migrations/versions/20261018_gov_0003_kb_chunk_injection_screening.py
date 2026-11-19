"""Prompt-injection screening of knowledge-base chunks (LLD §7: scan retrieved text).

Each chunk keeps its indicator names and the injection detector version that produced them. Existing
chunks get NULL (never screened) and are not retrievable until their document is re-ingested; the gateway
Space re-ingests its boot knowledge base on every start.

Revision ID: gov_0003
Revises: fin_0004
Create Date: 2026-10-18 15:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "gov_0003"
down_revision: str | None = "fin_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "kb_chunk",
        sa.Column("injection_indicators", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("kb_chunk", sa.Column("injection_detector", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("kb_chunk", "injection_detector")
    op.drop_column("kb_chunk", "injection_indicators")
