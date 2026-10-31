"""ORM-модели. На этапе 0 здесь только очередь фоновых задач."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Job(Base):
    """Фоновая задача worker'а (синхронизация, парсинг, векторизация, граф, противоречия).

    Очередь живёт прямо в PostgreSQL: отдельный Redis/RabbitMQ не нужен.
    Worker забирает задачу запросом SELECT ... FOR UPDATE SKIP LOCKED,
    поэтому несколько worker'ов не возьмут одну и ту же задачу.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # Больше — раньше. Новые документы получат приоритет выше, чем первичная индексация.
    priority: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # queued → running → done | failed (failed после исчерпания попыток)
    status: Mapped[str] = mapped_column(String(16), server_default=text("'queued'"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, server_default=text("3"))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(128))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("status IN ('queued', 'running', 'done', 'failed')", name="ck_jobs_status"),
        Index(
            "ix_jobs_dequeue",
            text("priority DESC"),
            text("id"),
            postgresql_where=text("status = 'queued'"),
        ),
    )
