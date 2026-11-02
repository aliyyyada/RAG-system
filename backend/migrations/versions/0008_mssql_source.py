"""Источник документов в MS SQL Server (адаптер `mssql`).

sources.config — настройки адаптера (вызов функции, имена колонок, режим прав).
                 Пароль сюда не попадает: он только в .env.
source_items   — последний снимок источника, который отдаёт всё одним вызовом функции:
                 шапка документа, список его файлов (по SHA-256 в хранилище) и «отпечаток»
                 содержимого. По отпечатку видно, что документ изменился, когда у источника
                 нет даты изменения.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column(
            "config", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
    )
    op.create_table(
        "source_items",
        sa.Column(
            "source_id",
            sa.BigInteger(),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("external_id", sa.String(128), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        # Версия документа: дата изменения из источника, а если её нет — момент, когда мы
        # впервые увидели это содержимое (отпечаток).
        sa.Column("version", TZ, nullable=False),
        sa.Column("deleted_at", TZ),
        sa.Column("header", postgresql.JSONB(), nullable=False),
        sa.Column(
            "files", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("seen_at", TZ, server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("source_items")
    op.drop_column("sources", "config")
