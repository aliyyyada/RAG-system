"""Этап 2: источники, коллекции, реестр документов, staging, файлы, фрагменты, синхронизация.

Коллекция «Ручные загрузки» создаётся здесь же: в неё попадают документы,
загруженные через интерфейс (право documents.upload_manual).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)
JSONB = postgresql.JSONB()


def _now() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.add_column("jobs", sa.Column("dedupe_key", sa.String(128)))
    op.create_index(
        "ux_jobs_dedupe_queued",
        "jobs",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("status = 'queued' AND dedupe_key IS NOT NULL"),
    )

    op.create_table(
        "sources",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("adapter", sa.String(32), server_default=sa.text("'http_json'"), nullable=False),
        sa.Column("base_url", sa.String(500), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "sync_interval_minutes", sa.Integer(), server_default=sa.text("15"), nullable=False
        ),
        sa.Column("overlap_minutes", sa.Integer(), server_default=sa.text("60"), nullable=False),
        sa.Column("reconcile_hour", sa.Integer(), server_default=sa.text("2"), nullable=False),
        sa.Column("callback_token_hash", sa.String(64)),
        sa.Column("sync_cursor", TZ),
        sa.Column("last_sync_at", TZ),
        sa.Column("last_reconcile_at", TZ),
        sa.Column("source_total", sa.Integer()),
        sa.Column("available", sa.Boolean()),
        sa.Column("last_error", sa.Text()),
        sa.Column("last_checked_at", TZ),
        _now(),
        sa.CheckConstraint("code ~ '^[a-z][a-z0-9_-]{1,31}$'", name="ck_sources_code"),
        sa.CheckConstraint("sync_interval_minutes >= 1", name="ck_sources_interval"),
        sa.CheckConstraint("reconcile_hour BETWEEN 0 AND 23", name="ck_sources_reconcile_hour"),
    )

    op.create_table(
        "collections",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column(
            "source_id",
            sa.BigInteger(),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            unique=True,
        ),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        _now(),
        sa.CheckConstraint("kind IN ('source', 'manual', 'topic')", name="ck_collections_kind"),
    )
    op.execute(
        "INSERT INTO collections (name, kind, description) VALUES "
        "('Ручные загрузки', 'manual', 'Документы, загруженные через интерфейс')"
    )

    op.create_table(
        "documents",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("source_id", sa.BigInteger(), sa.ForeignKey("sources.id", ondelete="CASCADE")),
        sa.Column(
            "collection_id", sa.BigInteger(), sa.ForeignKey("collections.id"), nullable=False
        ),
        sa.Column("external_id", sa.String(128)),
        sa.Column("version", TZ),
        sa.Column("indexed_version", TZ),
        sa.Column("number", sa.String(100)),
        sa.Column("title", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("doc_type", sa.String(100)),
        sa.Column("topic", sa.String(200)),
        sa.Column("doc_date", sa.Date()),
        sa.Column("header", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("parts_summary", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("is_common", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_excluded", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("deleted_at", TZ),
        sa.Column("deleted_reason", sa.String(32)),
        sa.Column("status", sa.String(16), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("error_stage", sa.String(16)),
        sa.Column("error_message", sa.Text()),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("status_changed_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _now(),
        sa.Column("updated_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'ready', 'error')", name="ck_documents_status"
        ),
        sa.CheckConstraint(
            "(source_id IS NULL) = (external_id IS NULL)", name="ck_documents_source_ref"
        ),
    )
    op.create_index(
        "ux_documents_source_external",
        "documents",
        ["source_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("source_id IS NOT NULL"),
    )
    op.create_index("ix_documents_status", "documents", ["source_id", "status"])
    op.create_index("ix_documents_collection", "documents", ["collection_id"])

    op.create_table(
        "document_files",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ord", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("source_file_id", sa.String(128)),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("mime", sa.String(100), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("pages", sa.Integer()),
        sa.Column("ocr_pages", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("text", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("warnings", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
    )
    op.create_index("ix_document_files_document_id", "document_files", ["document_id"])
    op.create_index("ix_document_files_sha256", "document_files", ["sha256"])

    op.create_table(
        "chunks",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "file_id", sa.BigInteger(), sa.ForeignKey("document_files.id", ondelete="CASCADE")
        ),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("section", sa.Text()),
        sa.Column("point", sa.String(32)),
        sa.Column("page_from", sa.Integer()),
        sa.Column("page_to", sa.Integer()),
        sa.Column("token_estimate", sa.Integer(), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('russian', text)", persisted=True),
        ),
        sa.CheckConstraint("kind IN ('header', 'text', 'table')", name="ck_chunks_kind"),
    )
    op.create_index("ux_chunks_document_ord", "chunks", ["document_id", "ord"], unique=True)
    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], postgresql_using="gin")

    op.create_table(
        "document_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _now(),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("version", TZ),
        sa.Column("details", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
    )
    op.create_index("ix_document_events_document_id", "document_events", ["document_id"])

    op.create_table(
        "staging_documents",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "source_id",
            sa.BigInteger(),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("version", TZ),
        sa.Column("received_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("header", JSONB, nullable=False),
        sa.Column("parts", JSONB, nullable=False),
        sa.Column("files", JSONB, nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'received'"), nullable=False),
        sa.Column("check_errors", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
    )
    op.create_index(
        "ux_staging_source_external",
        "staging_documents",
        ["source_id", "external_id"],
        unique=True,
    )

    op.create_table(
        "sync_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "source_id",
            sa.BigInteger(),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("started_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", TZ),
        sa.Column("status", sa.String(16), server_default=sa.text("'running'"), nullable=False),
        sa.Column("stats", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("error", sa.Text()),
    )
    op.create_index("ix_sync_runs_source_id", "sync_runs", ["source_id"])


def downgrade() -> None:
    op.drop_table("sync_runs")
    op.drop_table("staging_documents")
    op.drop_table("document_events")
    op.drop_table("chunks")
    op.drop_table("document_files")
    op.drop_table("documents")
    op.drop_table("collections")
    op.drop_table("sources")
    op.drop_index("ux_jobs_dedupe_queued", table_name="jobs")
    op.drop_column("jobs", "dedupe_key")
