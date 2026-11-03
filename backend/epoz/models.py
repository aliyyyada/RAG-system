"""Таблицы проверки ЕПоЗ: редакции ЕПоЗ, их части и пункты (этап 1).

Справочник ОКПД2 и таблицы самой проверки добавляются в этот же файл на следующих этапах.
Внешние ключи на `documents` объявлены только в миграции, а не в ORM: так порядок импорта
модулей не влияет на настройку мапперов.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class EpozEdition(Base):
    """Редакция ЕПоЗ: полный набор частей (Положение и Приложения 1-14).

    draft — собирается администратором, в поиске не участвует; active — действующая (одна);
    archived — прежняя, остаётся для истории проверок. Черновик наследует все части
    действующей редакции, администратор заменяет только нужные.
    """

    __tablename__ = "epoz_editions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(Text, server_default=text("''"))
    status: Mapped[str] = mapped_column(String(16), server_default=text("'draft'"))
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_by: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'active', 'archived')", name="ck_epoz_editions_status"
        ),
        Index(
            "ux_epoz_editions_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "ux_epoz_editions_draft",
            "status",
            unique=True,
            postgresql_where=text("status = 'draft'"),
        ),
    )


class EpozPart(Base):
    """Часть редакции: файл Положения или одного из приложений, разобранный в документ."""

    __tablename__ = "epoz_parts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    edition_id: Mapped[int] = mapped_column(ForeignKey("epoz_editions.id", ondelete="CASCADE"))
    part_key: Mapped[str] = mapped_column(String(16))  # main, app1 … app14
    document_id: Mapped[int] = mapped_column(BigInteger)
    file_name: Mapped[str] = mapped_column(String(500), server_default=text("''"))
    uploaded_by: Mapped[str | None] = mapped_column(String(64))
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Отчёт разбора: способ нумерации, сопоставление с оглавлением, доля найденных ссылок.
    report: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # Часть унаследована от прежней редакции (не загружалась заново).
    inherited: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))

    __table_args__ = (
        Index("ux_epoz_parts_edition_key", "edition_id", "part_key", unique=True),
        Index("ix_epoz_parts_document", "document_id"),
    )


class EpozClause(Base):
    """Пункт документа ЕПоЗ с восстановленным номером: «10.3.4», «6.6(2)».

    По этой таблице критерии проверки берут пункты **по номеру**, а не по смыслу:
    `epoz.clauses.get_clause(session, "main", "10.3")` вернёт заголовок п. 10.3 и весь его текст.
    """

    __tablename__ = "epoz_clauses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(BigInteger)
    ord: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(64))
    level: Mapped[int] = mapped_column(Integer)
    is_heading: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    text: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        Index("ux_epoz_clauses_doc_ord", "document_id", "ord", unique=True),
        Index("ix_epoz_clauses_doc_label", "document_id", "label"),
    )


class Okpd2Edition(Base):
    """Редакция справочника ОКПД2 (ОК 034-2014 с изменениями): загружается из Excel в админке.

    Действующая одна; прежние остаются для истории проверок (их записи не удаляются)."""

    __tablename__ = "okpd2_editions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    note: Mapped[str] = mapped_column(Text, server_default=text("''"))
    effective_date: Mapped[Any | None] = mapped_column(Date)
    records: Mapped[int] = mapped_column(Integer)
    declared_total: Mapped[int | None] = mapped_column(Integer)
    imported_by: Mapped[str | None] = mapped_column(String(64))
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))

    __table_args__ = (
        Index(
            "ux_okpd2_editions_active", "is_active", unique=True, postgresql_where=text("is_active")
        ),
    )


class Okpd2(Base):
    """Код ОКПД2: «26.20.11.110» — «Ноутбуки…». Уровень: 1 класс (2 знака) … 6 полный код (9 знаков)."""

    __tablename__ = "okpd2"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    edition_id: Mapped[int] = mapped_column(ForeignKey("okpd2_editions.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(Text)
    level: Mapped[int] = mapped_column(Integer)
    parent_code: Mapped[str | None] = mapped_column(String(16))
    name_tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('russian', name)", persisted=True)
    )

    __table_args__ = (
        Index("ux_okpd2_edition_code", "edition_id", "code", unique=True),
        Index("ix_okpd2_name_tsv", "name_tsv", postgresql_using="gin"),
        Index(
            "ix_okpd2_code_prefix",
            "edition_id",
            "code",
            postgresql_ops={"code": "varchar_pattern_ops"},
        ),
    )


# ---------------------------------------------------------------------------
# Проверка закупочного пакета (этап 3)
# ---------------------------------------------------------------------------


class CheckRun(Base):
    """Одна проверка пакета. Живёт, пока жива сессия входа пользователя (и не дольше простоя)."""

    __tablename__ = "check_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    session_id: Mapped[Any | None] = mapped_column(UUID(as_uuid=True))
    # queued → running → done | error | cancelled
    status: Mapped[str] = mapped_column(String(16), server_default=text("'queued'"))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    epoz_edition_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    progress: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    error: Mapped[str | None] = mapped_column(Text)


class CheckFile(Base):
    """Файл пакета: роль (договор, ТЗ, КП…) выбрана пользователем, `role_detected` — по содержимому."""

    __tablename__ = "check_files"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("check_runs.id", ondelete="CASCADE"))
    document_id: Mapped[int | None] = mapped_column(BigInteger)
    ord: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(500))
    path: Mapped[str] = mapped_column(Text)
    size: Mapped[int] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(String(16))
    role_detected: Mapped[str | None] = mapped_column(String(16))
    is_winner: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    parse_status: Mapped[str] = mapped_column(String(16), server_default=text("'queued'"))
    parse_error: Mapped[str | None] = mapped_column(Text)
    pages: Mapped[int | None] = mapped_column(Integer)
    ocr_pages: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    extracted: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class CheckCriterion(Base):
    """Критерий проверки. Список — данные, а не код: добавить критерий можно без правки кода,
    если для него уже есть правило (epoz/checks/rules.py)."""

    __tablename__ = "check_criteria"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(16))
    block: Mapped[int] = mapped_column(Integer)
    block_title: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))
    method: Mapped[str] = mapped_column(String(16), server_default=text("'rules'"))
    epoz_refs: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    ord: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class CheckResult(Base):
    __tablename__ = "check_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("check_runs.id", ondelete="CASCADE"))
    criterion_id: Mapped[int] = mapped_column(ForeignKey("check_criteria.id", ondelete="CASCADE"))
    # ok — соответствует; fail — не соответствует; warn — требует внимания; manual — нужна ручная проверка
    status: Mapped[str] = mapped_column(String(16))
    summary: Mapped[str] = mapped_column(Text, server_default=text("''"))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    model: Mapped[str | None] = mapped_column(String(100))
    ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
