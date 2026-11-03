"""Справочник ОКПД2 (проверка ЕПоЗ, этап 2).

okpd2_editions — редакции справочника (одна действующая, прежние для истории);
okpd2          — коды и названия; поиск по началу кода и по словам названия (русская морфология).

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "okpd2_editions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("effective_date", sa.Date()),
        sa.Column("records", sa.Integer(), nullable=False),
        sa.Column("declared_total", sa.Integer()),
        sa.Column("imported_by", sa.String(64)),
        sa.Column("imported_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_index(
        "ux_okpd2_editions_active",
        "okpd2_editions",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_table(
        "okpd2",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "edition_id",
            sa.BigInteger(),
            sa.ForeignKey("okpd2_editions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("parent_code", sa.String(16)),
        sa.Column(
            "name_tsv",
            sa.dialects.postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('russian', name)", persisted=True),
        ),
    )
    op.create_index("ux_okpd2_edition_code", "okpd2", ["edition_id", "code"], unique=True)
    op.create_index("ix_okpd2_name_tsv", "okpd2", ["name_tsv"], postgresql_using="gin")
    op.create_index(
        "ix_okpd2_code_prefix",
        "okpd2",
        ["edition_id", "code"],
        postgresql_ops={"code": "varchar_pattern_ops"},
    )


def downgrade() -> None:
    op.drop_table("okpd2")
    op.drop_table("okpd2_editions")
