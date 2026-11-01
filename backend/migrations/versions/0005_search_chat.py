"""Этап 4: векторы фрагментов, история чатов, очередь к LLM.

chunks.embedding — вектор bge-m3 (1024 числа) для семантического поиска; пока фрагмент
                   не векторизован, он находится только полнотекстовым поиском;
chats, chat_messages — история диалогов вошедших пользователей (90 дней, настраивается);
llm_queue         — кто сейчас ждёт ответа модели: место в очереди показываем пользователю.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)
JSONB = postgresql.JSONB()


def upgrade() -> None:
    op.execute("ALTER TABLE chunks ADD COLUMN embedding vector(1024)")
    # HNSW — приближённый поиск ближайших векторов по косинусному расстоянию.
    op.execute(
        "CREATE INDEX ix_chunks_embedding ON chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )
    # Worker быстро находит фрагменты, которые ещё нужно векторизовать.
    op.execute(
        "CREATE INDEX ix_chunks_need_embedding ON chunks (document_id) WHERE embedding IS NULL"
    )

    op.create_table(
        "chats",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TZ, server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_chats_user_updated", "chats", ["user_id", sa.text("updated_at DESC")])
    op.create_index("ix_chats_updated", "chats", ["updated_at"])

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "chat_id",
            sa.BigInteger(),
            sa.ForeignKey("chats.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("sources", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'done'"), nullable=False),
        sa.Column("meta", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("feedback", sa.SmallInteger()),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_chat_messages_role"),
        sa.CheckConstraint(
            "status IN ('pending', 'done', 'error', 'interrupted')",
            name="ck_chat_messages_status",
        ),
    )
    op.create_index("ix_chat_messages_chat", "chat_messages", ["chat_id", "id"])

    op.create_table(
        "llm_queue",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        # 0 — вошедший пользователь, 1 — гость (гости встают после вошедших, п. 6 ТЗ).
        sa.Column("priority", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("heartbeat_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", TZ),
    )
    op.create_index("ix_llm_queue_order", "llm_queue", ["priority", "id"])


def downgrade() -> None:
    op.drop_table("llm_queue")
    op.drop_table("chat_messages")
    op.drop_table("chats")
    op.execute("DROP INDEX IF EXISTS ix_chunks_need_embedding")
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding")
    op.execute("ALTER TABLE chunks DROP COLUMN embedding")
