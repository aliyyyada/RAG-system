"""ЕПоЗ: редакции, части, пункты и защита документов от удаления (проверка ЕПоЗ, этап 1).

documents.is_protected / epoz_part — защищённый документ ЕПоЗ и его часть комплекта;
documents.check_run_id             — документ принадлежит проверке пакета (таблица — в 0011);
epoz_editions / epoz_parts         — редакции ЕПоЗ (черновик, действующая, архив) и их файлы;
epoz_clauses                       — пункты с восстановленными номерами («10.3.4»);
триггер epoz_protect               — защищённый документ нельзя удалить, исключить из поиска
                                     или пометить удалённым даже запросом из консоли БД.

downgrade убирает всё, в том числе сами документы ЕПоЗ (они без этих полей были бы обычными
ручными загрузками, видимыми группе): откат возвращает базу к состоянию до функционала.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)

PROTECT_FN = """
CREATE OR REPLACE FUNCTION epoz_protect_documents() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.is_protected THEN
            RAISE EXCEPTION 'Документ ЕПоЗ защищён от удаления (documents.id=%)', OLD.id
                USING ERRCODE = '42501';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.is_protected AND (
        NEW.is_protected IS DISTINCT FROM TRUE
        OR NEW.deleted_at IS NOT NULL
        OR NEW.is_excluded
    ) THEN
        RAISE EXCEPTION 'Документ ЕПоЗ нельзя исключить, удалить или снять с него защиту (documents.id=%)', OLD.id
            USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("is_protected", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("documents", sa.Column("epoz_part", sa.String(16)))
    op.add_column("documents", sa.Column("check_run_id", sa.BigInteger()))
    op.create_index(
        "ix_documents_check_run",
        "documents",
        ["check_run_id"],
        postgresql_where=sa.text("check_run_id IS NOT NULL"),
    )

    op.create_table(
        "epoz_editions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("title", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("created_by", sa.String(64)),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("activated_at", TZ),
        sa.Column("activated_by", sa.String(64)),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'archived')", name="ck_epoz_editions_status"
        ),
    )
    # Не больше одной действующей и одного черновика одновременно.
    op.create_index(
        "ux_epoz_editions_active",
        "epoz_editions",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ux_epoz_editions_draft",
        "epoz_editions",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'draft'"),
    )

    op.create_table(
        "epoz_parts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "edition_id",
            sa.BigInteger(),
            sa.ForeignKey("epoz_editions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("part_key", sa.String(16), nullable=False),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("documents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("file_name", sa.String(500), server_default=sa.text("''"), nullable=False),
        sa.Column("uploaded_by", sa.String(64)),
        sa.Column("uploaded_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column(
            "report", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("inherited", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_index(
        "ux_epoz_parts_edition_key", "epoz_parts", ["edition_id", "part_key"], unique=True
    )
    op.create_index("ix_epoz_parts_document", "epoz_parts", ["document_id"])

    op.create_table(
        "epoz_clauses",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(64), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("is_heading", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
    )
    op.create_index("ux_epoz_clauses_doc_ord", "epoz_clauses", ["document_id", "ord"], unique=True)
    op.create_index("ix_epoz_clauses_doc_label", "epoz_clauses", ["document_id", "label"])

    op.execute(PROTECT_FN)
    op.execute(
        "CREATE TRIGGER epoz_protect BEFORE UPDATE OR DELETE ON documents "
        "FOR EACH ROW EXECUTE FUNCTION epoz_protect_documents()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS epoz_protect ON documents")
    op.execute("DROP FUNCTION IF EXISTS epoz_protect_documents()")
    op.drop_table("epoz_clauses")
    op.drop_table("epoz_parts")
    op.drop_table("epoz_editions")
    # Документы ЕПоЗ были созданы этим функционалом: убираем их вместе с ним
    # (фрагменты, файлы, права, события удаляются каскадом).
    op.execute("DELETE FROM documents WHERE epoz_part IS NOT NULL")
    op.drop_index("ix_documents_check_run", table_name="documents")
    op.drop_column("documents", "check_run_id")
    op.drop_column("documents", "epoz_part")
    op.drop_column("documents", "is_protected")
