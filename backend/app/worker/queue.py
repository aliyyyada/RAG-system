"""Очередь фоновых задач в PostgreSQL.

Пример: синхронизация нашла 300 новых документов источника и ставит 300 задач
`enqueue(session, "parse_document", {"document_id": ...})`. Worker по одной забирает
их через `dequeue`, а если контейнер перезапустили посреди работы, `requeue_stale`
вернёт «зависшие» задачи в очередь, и индексация продолжится с того же места.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


async def enqueue(
    session: AsyncSession, kind: str, payload: dict[str, Any] | None = None, priority: int = 0
) -> int:
    job = Job(kind=kind, payload=payload or {}, priority=priority)
    session.add(job)
    await session.flush()
    return job.id


_DEQUEUE_SQL = text(
    """
    UPDATE jobs SET status = 'running', locked_at = now(), locked_by = :worker,
                    attempts = attempts + 1
    WHERE id = (
        SELECT id FROM jobs
        WHERE status = 'queued' AND run_after <= now()
        ORDER BY priority DESC, id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, kind, payload, attempts, max_attempts
    """
)


async def dequeue(session: AsyncSession, worker_id: str) -> ClaimedJob | None:
    """Атомарно забирает самую приоритетную готовую задачу. Коммит делает вызывающий."""
    row = (await session.execute(_DEQUEUE_SQL, {"worker": worker_id})).mappings().first()
    if row is None:
        return None
    return ClaimedJob(**row)


async def mark_done(session: AsyncSession, job_id: int) -> None:
    await session.execute(
        text(
            "UPDATE jobs SET status = 'done', finished_at = now(), locked_at = NULL, "
            "locked_by = NULL, last_error = NULL WHERE id = :id"
        ),
        {"id": job_id},
    )


async def mark_failed(
    session: AsyncSession, job_id: int, error: str, retry_delay_seconds: int = 60
) -> None:
    """Ошибка: повторяем позже, пока не кончатся попытки, затем статус failed."""
    await session.execute(
        text(
            """
            UPDATE jobs SET
                status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
                finished_at = CASE WHEN attempts >= max_attempts THEN now() END,
                run_after = now() + make_interval(secs => :delay),
                locked_at = NULL, locked_by = NULL, last_error = :error
            WHERE id = :id
            """
        ),
        {"id": job_id, "error": error[:4000], "delay": retry_delay_seconds},
    )


async def requeue_stale(session: AsyncSession, stale_after_seconds: int) -> int:
    """Возвращает в очередь задачи, которые слишком долго в статусе running."""
    result = await session.execute(
        text(
            """
            UPDATE jobs SET status = 'queued', locked_at = NULL, locked_by = NULL
            WHERE status = 'running'
              AND locked_at < now() - make_interval(secs => :stale)
            """
        ),
        {"stale": stale_after_seconds},
    )
    return result.rowcount or 0


async def queue_stats(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(text("SELECT status, count(*) FROM jobs GROUP BY status"))
    stats = {"queued": 0, "running": 0, "done": 0, "failed": 0}
    stats.update({status: count for status, count in rows.all()})
    return stats
