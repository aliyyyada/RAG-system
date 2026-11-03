"""Проверка закупочного пакета по ЕПоЗ (этап 3): проверки, файлы, критерии, результаты.

check_runs     — одна проверка пользователя; привязана к его входу (сессии) и умирает вместе с ним;
check_files    — файлы пакета с ролью (договор, ТЗ, КП…) и данными, извлечёнными из файла;
check_criteria — критерии проверки как данные (20 строк из таблицы заказчика);
check_results  — результат по каждому критерию: статус, объяснение, цитаты.

documents.check_run_id получает внешний ключ с ON DELETE CASCADE: удалили проверку — исчезли
документы пакета, их фрагменты и файлы-записи.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "check_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", postgresql.UUID(as_uuid=True)),
        sa.Column("status", sa.String(16), server_default=sa.text("'queued'"), nullable=False),
        sa.Column(
            "params", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("epoz_edition_id", sa.BigInteger()),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", TZ),
        sa.Column("finished_at", TZ),
        sa.Column("last_activity_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column(
            "progress", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "summary", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("error", sa.Text()),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'error', 'cancelled')",
            name="ck_check_runs_status",
        ),
    )
    op.create_index("ix_check_runs_user", "check_runs", ["user_id"])
    op.create_index("ix_check_runs_session", "check_runs", ["session_id"])

    op.create_foreign_key(
        "fk_documents_check_run",
        "documents",
        "check_runs",
        ["check_run_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.create_table(
        "check_files",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "run_id",
            sa.BigInteger(),
            sa.ForeignKey("check_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_id", sa.BigInteger(), sa.ForeignKey("documents.id", ondelete="SET NULL")
        ),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("role_detected", sa.String(16)),
        sa.Column("is_winner", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "parse_status", sa.String(16), server_default=sa.text("'queued'"), nullable=False
        ),
        sa.Column("parse_error", sa.Text()),
        sa.Column("pages", sa.Integer()),
        sa.Column("ocr_pages", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "extracted", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
    )
    op.create_index("ix_check_files_run", "check_files", ["run_id", "ord"])

    op.create_table(
        "check_criteria",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("block", sa.Integer(), nullable=False),
        sa.Column("block_title", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("method", sa.String(16), server_default=sa.text("'rules'"), nullable=False),
        sa.Column(
            "epoz_refs", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.create_index("ux_check_criteria_code", "check_criteria", ["code"], unique=True)

    op.create_table(
        "check_results",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "run_id",
            sa.BigInteger(),
            sa.ForeignKey("check_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "criterion_id",
            sa.BigInteger(),
            sa.ForeignKey("check_criteria.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("summary", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("model", sa.String(100)),
        sa.Column("ms", sa.Integer()),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('ok', 'fail', 'warn', 'manual')", name="ck_check_results_status"
        ),
    )
    op.create_index(
        "ux_check_results_run_criterion", "check_results", ["run_id", "criterion_id"], unique=True
    )


def downgrade() -> None:
    op.drop_table("check_results")
    op.drop_table("check_criteria")
    op.drop_table("check_files")
    # Документы пакетов (check_run_id) удаляются каскадом вместе с проверками.
    op.execute("DELETE FROM check_runs")
    op.drop_constraint("fk_documents_check_run", "documents", type_="foreignkey")
    op.drop_table("check_runs")
