"""Этап 5: граф знаний.

document_links   — связи «документ → документ» по правилам: отменяет / изменяет /
                   во исполнение / ссылается на (фрагмент-доказательство chunk_id);
entities, entity_mentions, relations — сущности и связи, извлечённые моделью; каждое
                   упоминание и каждая связь ссылаются на фрагмент, поэтому граф
                   фильтруется по правам так же, как фрагменты;
documents.legal_status — статус «Отменён / Действует, изменён», вычисленный по связям;
chunks.graph_*   — прогресс извлечения моделью (возобновляется после перезапуска);
app_flags        — переключатели админки (извлечение моделью включает администратор).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)


def _fk(table: str, ondelete: str = "CASCADE") -> sa.ForeignKey:
    return sa.ForeignKey(f"{table}.id", ondelete=ondelete)


def upgrade() -> None:
    op.add_column("documents", sa.Column("legal_status", sa.String(64)))
    op.add_column("documents", sa.Column("links_version", TZ))
    # До первого прогона правил статус — из шапки источника, как было на этапе 4.
    op.execute(
        "UPDATE documents SET legal_status = NULLIF(btrim(header->>'status'), '') "
        "WHERE header ? 'status'"
    )
    op.add_column("chunks", sa.Column("graph_state", sa.String(8)))
    op.add_column("chunks", sa.Column("graph_ms", sa.Integer()))
    op.add_column("chunks", sa.Column("graph_note", sa.String(200)))
    op.execute(
        "CREATE INDEX ix_chunks_graph_pending ON chunks (document_id) WHERE graph_state IS NULL"
    )

    op.create_table(
        "document_links",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("from_document_id", sa.BigInteger(), _fk("documents"), nullable=False),
        sa.Column("chunk_id", sa.BigInteger(), _fk("chunks"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("target_document_id", sa.BigInteger(), _fk("documents", "SET NULL")),
        sa.Column("target_type", sa.String(64)),
        sa.Column("target_number", sa.String(100), nullable=False),
        sa.Column("target_number_norm", sa.String(100), nullable=False),
        sa.Column("target_date", sa.Date()),
        sa.Column("target_label", sa.Text(), nullable=False),
        sa.Column("quote", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('cancels', 'amends', 'executes', 'refers')", name="ck_document_links_kind"
        ),
    )
    op.create_index("ix_document_links_from", "document_links", ["from_document_id"])
    op.create_index("ix_document_links_target", "document_links", ["target_document_id"])
    op.create_index("ix_document_links_chunk", "document_links", ["chunk_id"])
    op.create_index(
        "ix_document_links_unresolved",
        "document_links",
        ["target_number_norm"],
        postgresql_where=sa.text("target_document_id IS NULL"),
    )

    op.create_table(
        "entities",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("norm_name", sa.String(300), nullable=False),
        sa.Column(
            "name_tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('russian', name)", persisted=True),
        ),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ux_entities_type_norm", "entities", ["type", "norm_name"], unique=True)
    op.create_index("ix_entities_name_tsv", "entities", ["name_tsv"], postgresql_using="gin")

    op.create_table(
        "entity_mentions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("entity_id", sa.BigInteger(), _fk("entities"), nullable=False),
        sa.Column("chunk_id", sa.BigInteger(), _fk("chunks"), nullable=False),
        sa.Column("document_id", sa.BigInteger(), _fk("documents"), nullable=False),
        sa.Column("surface", sa.String(300)),
    )
    op.create_index("ux_entity_mentions", "entity_mentions", ["entity_id", "chunk_id"], unique=True)
    op.create_index("ix_entity_mentions_chunk", "entity_mentions", ["chunk_id"])
    op.create_index("ix_entity_mentions_document", "entity_mentions", ["document_id"])

    op.create_table(
        "relations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("subject_id", sa.BigInteger(), _fk("entities"), nullable=False),
        sa.Column("predicate", sa.String(32), nullable=False),
        sa.Column("object_id", sa.BigInteger(), _fk("entities"), nullable=False),
        sa.Column("chunk_id", sa.BigInteger(), _fk("chunks"), nullable=False),
        sa.Column("document_id", sa.BigInteger(), _fk("documents"), nullable=False),
        sa.Column("evidence", sa.Text(), server_default=sa.text("''"), nullable=False),
    )
    op.create_index(
        "ux_relations",
        "relations",
        ["subject_id", "predicate", "object_id", "chunk_id"],
        unique=True,
    )
    op.create_index("ix_relations_subject", "relations", ["subject_id"])
    op.create_index("ix_relations_object", "relations", ["object_id"])
    op.create_index("ix_relations_chunk", "relations", ["chunk_id"])

    op.create_table(
        "app_flags",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", postgresql.JSONB()),
        sa.Column("updated_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_by", sa.String(64)),
    )


def downgrade() -> None:
    op.drop_table("app_flags")
    op.drop_table("relations")
    op.drop_table("entity_mentions")
    op.drop_table("entities")
    op.drop_table("document_links")
    op.execute("DROP INDEX IF EXISTS ix_chunks_graph_pending")
    op.drop_column("chunks", "graph_note")
    op.drop_column("chunks", "graph_ms")
    op.drop_column("chunks", "graph_state")
    op.drop_column("documents", "links_version")
    op.drop_column("documents", "legal_status")
