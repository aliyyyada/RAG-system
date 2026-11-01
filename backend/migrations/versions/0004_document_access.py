"""Этап 3: доступ к документам — кэш прав источников и доступ к ручным загрузкам.

access_cache      — когда и с каким итогом спрашивали источник о правах человека;
access_cache_ids  — id документов источника, которые он разрешил (тысячи строк на человека);
document_grants   — кому (человеку или группе) администратор открыл ручную загрузку.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "access_cache",
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "source_id",
            sa.BigInteger(),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("fetched_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", TZ, nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("id_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.Text()),
    )
    op.create_index("ix_access_cache_source_id", "access_cache", ["source_id"])
    op.create_table(
        "access_cache_ids",
        sa.Column("user_id", sa.BigInteger(), primary_key=True),
        sa.Column("source_id", sa.BigInteger(), primary_key=True),
        sa.Column("external_id", sa.String(128), primary_key=True),
        sa.ForeignKeyConstraint(
            ["user_id", "source_id"],
            ["access_cache.user_id", "access_cache.source_id"],
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "document_grants",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("group_id", sa.BigInteger(), sa.ForeignKey("groups.id", ondelete="CASCADE")),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "(user_id IS NULL) <> (group_id IS NULL)", name="ck_document_grants_target"
        ),
    )
    op.create_index(
        "ux_document_grants_user",
        "document_grants",
        ["document_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    op.create_index(
        "ux_document_grants_group",
        "document_grants",
        ["document_id", "group_id"],
        unique=True,
        postgresql_where=sa.text("group_id IS NOT NULL"),
    )
    op.create_index("ix_document_grants_user_id", "document_grants", ["user_id"])
    op.create_index("ix_document_grants_group_id", "document_grants", ["group_id"])


def downgrade() -> None:
    op.drop_table("document_grants")
    op.drop_table("access_cache_ids")
    op.drop_table("access_cache")
