"""Knowledge base for retrieval: documents and embedded chunks (pgvector, HNSW cosine index).

Revision ID: opt_0001
Revises: fin_0001
Create Date: 2026-10-14 09:37:58.270264+00:00
"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "opt_0001"
down_revision: str | None = "fin_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "kb_document",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("classification", sa.String(length=32), nullable=False),
        sa.Column("application_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("expires_on", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], name=op.f("fk_kb_document_tenant_id_tenant")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_document")),
        sa.UniqueConstraint("tenant_id", "document_id", name=op.f("uq_kb_document_tenant_id")),
    )
    op.create_table(
        "kb_chunk",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("document_pk", sa.BigInteger(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("evidence_id", sa.String(length=160), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("section", sa.String(length=300), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_estimate", sa.BigInteger(), nullable=False),
        sa.Column("dlp_finding_types", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("embedding_model", sa.String(length=200), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.vector.VECTOR(dim=768), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_pk"],
            ["kb_document.id"],
            name=op.f("fk_kb_chunk_document_pk_kb_document"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_chunk")),
        sa.UniqueConstraint("tenant_id", "evidence_id", name=op.f("uq_kb_chunk_tenant_id")),
    )
    op.create_index(op.f("ix_kb_chunk_document_pk"), "kb_chunk", ["document_pk"], unique=False)
    op.create_index(
        "ix_kb_chunk_embedding_hnsw",
        "kb_chunk",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_kb_chunk_embedding_hnsw", table_name="kb_chunk")
    op.drop_index(op.f("ix_kb_chunk_document_pk"), table_name="kb_chunk")
    op.drop_table("kb_chunk")
    op.drop_table("kb_document")
