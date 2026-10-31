"""Отчёт о полноте загрузки по источнику.

Пример ответа для экрана «Источники»: у источника 4 812 документов (без удалённых),
загружено 3 120, в очереди 1 683, обрабатывается 1, с ошибкой 9; скорость 4,8 документа
в минуту за последние полчаса → осталось ≈ 5 ч 50 мин.
"""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest.service import processing_rate
from app.models import Document, Source, SyncRun


@dataclass
class SourceReport:
    at_source: int | None
    known: int
    ready: int
    queued: int
    processing: int
    error: int
    deleted: int
    excluded: int
    rate_per_minute: float
    eta_minutes: int | None
    progress: float
    errors_by_stage: dict[str, int] = field(default_factory=dict)
    runs: list[dict[str, Any]] = field(default_factory=list)


async def source_report(session: AsyncSession, source: Source, runs: int = 10) -> SourceReport:
    alive = (Document.source_id == source.id) & Document.deleted_at.is_(None)
    rows = await session.execute(
        select(Document.status, func.count()).where(alive).group_by(Document.status)
    )
    by_status = {"queued": 0, "processing": 0, "ready": 0, "error": 0}
    by_status.update({s: n for s, n in rows.all()})
    deleted = await session.scalar(
        select(func.count()).where(
            Document.source_id == source.id, Document.deleted_at.is_not(None)
        )
    )
    excluded = await session.scalar(
        select(func.count()).where(alive, Document.is_excluded.is_(True))
    )
    stage_rows = await session.execute(
        select(Document.error_stage, func.count())
        .where(alive, Document.status == "error")
        .group_by(Document.error_stage)
    )
    known = sum(by_status.values())
    rate = await processing_rate(session, source.id)
    pending = by_status["queued"] + by_status["processing"]
    eta = round(pending / rate) if rate > 0 and pending else None
    denominator = max(source.source_total or 0, known, 1)
    run_rows = (
        await session.scalars(
            select(SyncRun)
            .where(SyncRun.source_id == source.id)
            .order_by(SyncRun.id.desc())
            .limit(runs)
        )
    ).all()
    return SourceReport(
        at_source=source.source_total,
        known=known,
        ready=by_status["ready"],
        queued=by_status["queued"],
        processing=by_status["processing"],
        error=by_status["error"],
        deleted=deleted or 0,
        excluded=excluded or 0,
        rate_per_minute=round(rate, 2),
        eta_minutes=eta,
        progress=round(by_status["ready"] / denominator, 4),
        errors_by_stage={s or "unknown": n for s, n in stage_rows.all()},
        runs=[
            {
                "id": r.id,
                "kind": r.kind,
                "status": r.status,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "stats": r.stats,
                "error": r.error,
            }
            for r in run_rows
        ],
    )
