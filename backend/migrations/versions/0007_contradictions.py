"""Этап 6: противоречия между документами.

contradiction_checks — кэш проверок пар текстов моделью (ключ — хэши текстов двух
                       фрагментов) и решение специалиста «Подтверждено / Отклонено»;
contradictions       — текущие пары фрагментов разных документов с противоречием
                       (каскадно удаляются вместе с фрагментами при переиндексации);
documents.contra_*   — для какой версии документа уже искали противоречия.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)


def _fk(table: str, ondelete: str = "CASCADE") -> sa.ForeignKey:
    return sa.ForeignKey(f"{table}.id", ondelete=ondelete)


def upgrade() -> None:
    op.add_column("documents", sa.Column("contra_version", TZ))
    op.add_column("documents", sa.Column("contra_pairs", sa.Integer()))

    op.create_table(
        "contradiction_checks",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("key_a", sa.String(64), nullable=False),
        sa.Column("key_b", sa.String(64), nullable=False),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("topic", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("explanation", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("quote_a", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("quote_b", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("model", sa.String(100)),
        sa.Column("ms", sa.Integer()),
        sa.Column("error", sa.String(200)),
        sa.Column("checked_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("review_status", sa.String(16), server_default=sa.text("'new'"), nullable=False),
        sa.Column("reviewed_by", sa.BigInteger(), _fk("users", "SET NULL")),
        sa.Column("reviewed_at", TZ),
        sa.Column("review_note", sa.Text()),
        sa.CheckConstraint(
            "verdict IN ('contradiction', 'possible', 'none', 'error')",
            name="ck_contradiction_checks_verdict",
        ),
        sa.CheckConstraint(
            "review_status IN ('new', 'confirmed', 'rejected')",
            name="ck_contradiction_checks_review",
        ),
        sa.CheckConstraint("key_a < key_b", name="ck_contradiction_checks_order"),
    )
    op.create_index(
        "ux_contradiction_checks_keys", "contradiction_checks", ["key_a", "key_b"], unique=True
    )
    op.create_index("ix_contradiction_checks_verdict", "contradiction_checks", ["verdict"])

    op.create_table(
        "contradictions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("check_id", sa.BigInteger(), _fk("contradiction_checks"), nullable=False),
        sa.Column("chunk_a_id", sa.BigInteger(), _fk("chunks"), nullable=False),
        sa.Column("chunk_b_id", sa.BigInteger(), _fk("chunks"), nullable=False),
        sa.Column("document_a_id", sa.BigInteger(), _fk("documents"), nullable=False),
        sa.Column("document_b_id", sa.BigInteger(), _fk("documents"), nullable=False),
        sa.Column("origin", sa.String(8), nullable=False),
        sa.Column("similarity", sa.Float()),
        sa.Column("found_at", TZ, server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ux_contradictions_chunks", "contradictions", ["chunk_a_id", "chunk_b_id"], unique=True
    )
    op.create_index("ix_contradictions_check", "contradictions", ["check_id"])
    op.create_index("ix_contradictions_doc_a", "contradictions", ["document_a_id"])
    op.create_index("ix_contradictions_doc_b", "contradictions", ["document_b_id"])
    op.create_index("ix_contradictions_chunk_b", "contradictions", ["chunk_b_id"])


def downgrade() -> None:
    op.drop_table("contradictions")
    op.drop_table("contradiction_checks")
    op.drop_column("documents", "contra_pairs")
    op.drop_column("documents", "contra_version")
