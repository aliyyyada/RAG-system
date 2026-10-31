"""Синхронизация с источником: инкрементальная (каждые N минут) и ночная сверка.

**Инкрементальная** (`source_sync`). Спрашиваем у источника документы, изменённые после
`sync_cursor − overlap_minutes`. Перекрытие нужно, потому что источник может записать
документ с датой изменения «в прошлом» (транзакция закоммитилась позже). Повторно
увиденный документ ничего не ломает: если его версия совпадает с нашей, он пропускается.
Курсор сохраняется после каждой страницы, поэтому перезапуск продолжает с того же места.

**Ночная сверка** (`source_reconcile`). Берём у источника полный список id с датами
изменения и сравниваем с реестром: чего нет у нас или у нас старее — ставим в очередь,
чего нет у источника — помечаем удалённым. Документы, «застрявшие» в очереди без задачи,
ставятся заново. Самая первая синхронизация источника — это и есть сверка (курсора ещё нет).

Обе работы только ставят задачи `document_process` на каждый документ; сам документ
получает и разбирает pipeline.py.
"""

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.ingest.pipeline import (
    JOB_KIND,
    PRIORITY_CHANGED,
    PRIORITY_INITIAL,
    add_event,
    enqueue_document,
    set_status,
    soft_delete,
)
from app.models import Collection, Document, Source, SyncRun
from app.sources import open_source
from app.sources.base import DocRef, SourceError
from app.worker import queue
from app.worker.queue import ClaimedJob

log = logging.getLogger(__name__)

SYNC_JOB = "source_sync"
RECONCILE_JOB = "source_reconcile"


def _now() -> datetime:
    return datetime.now(UTC)


async def source_collection(session: AsyncSession, source: Source) -> Collection:
    coll = await session.scalar(select(Collection).where(Collection.source_id == source.id))
    if coll is None:
        coll = Collection(name=source.name, kind="source", source_id=source.id)
        session.add(coll)
        await session.flush()
    return coll


async def apply_ref(
    session: AsyncSession,
    source: Source,
    collection_id: int,
    ref: DocRef,
    settings: Settings,
    priority: int,
    stats: dict[str, int],
) -> None:
    """Одна строка списка источника → наш реестр. Идемпотентно: повтор ничего не меняет."""
    doc = await session.scalar(
        select(Document)
        .where(Document.source_id == source.id, Document.external_id == ref.external_id)
        .with_for_update()
    )
    if ref.deleted_at is not None:
        if doc is not None and await soft_delete(session, doc, "deleted_in_source"):
            stats["deleted"] += 1
        else:
            stats["unchanged"] += 1
        return

    if doc is None:
        doc = Document(
            source_id=source.id,
            collection_id=collection_id,
            external_id=ref.external_id,
            version=ref.modified_at,
            status="queued",
        )
        session.add(doc)
        await session.flush()
        await add_event(session, doc.id, "discovered", ref.modified_at)
        stats["new"] += 1
    elif doc.deleted_at is not None:
        # Документ «воскрес» у источника (удаление отменили).
        doc.deleted_at = doc.deleted_reason = None
        doc.version = max(doc.version or ref.modified_at, ref.modified_at)
        set_status(doc, "queued")
        await add_event(session, doc.id, "restored", ref.modified_at)
        stats["restored"] += 1
    elif doc.version is None or ref.modified_at > doc.version:
        doc.version = ref.modified_at
        set_status(doc, "queued")
        await add_event(session, doc.id, "changed", ref.modified_at)
        stats["changed"] += 1
    else:
        stats["unchanged"] += 1
        return
    await enqueue_document(session, doc.id, settings, priority)


def _new_stats() -> dict[str, int]:
    return {"seen": 0, "new": 0, "changed": 0, "deleted": 0, "restored": 0, "unchanged": 0}


async def _start_run(sm: async_sessionmaker[AsyncSession], source_id: int, kind: str) -> int:
    async with sm() as s, s.begin():
        run = SyncRun(source_id=source_id, kind=kind)
        s.add(run)
        src = await s.get(Source, source_id)
        assert src is not None
        src.last_checked_at = _now()
        await s.flush()
        return run.id


async def _finish_run(
    sm: async_sessionmaker[AsyncSession],
    run_id: int,
    source_id: int,
    stats: dict[str, int],
    error: str | None,
    **source_updates: object,
) -> None:
    async with sm() as s, s.begin():
        run = await s.get(SyncRun, run_id)
        src = await s.get(Source, source_id)
        assert run is not None and src is not None
        run.finished_at = _now()
        run.stats = stats
        run.status = "error" if error else "ok"
        run.error = error
        src.available = error is None
        src.last_error = error
        if error is None:
            for key, value in source_updates.items():
                setattr(src, key, value)


async def _refresh_common(session: AsyncSession, source_id: int, ids: set[str]) -> int:
    """Признак «общий» у документов источника = входит в список общих id источника."""
    # = ANY(массив): тысячи id уходят одним параметром, а не тысячами.
    result = await session.execute(
        text(
            """
            UPDATE documents SET is_common = coalesce(external_id = ANY(:ids), false),
                                 updated_at = now()
            WHERE source_id = :sid
              AND is_common IS DISTINCT FROM coalesce(external_id = ANY(:ids), false)
            """
        ),
        {"sid": source_id, "ids": sorted(ids)},
    )
    return result.rowcount or 0


async def run_incremental(
    source_id: int, settings: Settings, sm: async_sessionmaker[AsyncSession]
) -> dict[str, int]:
    async with sm() as s:
        source = await s.get(Source, source_id)
    if source is None or not source.enabled:
        return {}
    if source.sync_cursor is None:
        # Первая синхронизация = полная сверка: курсора ещё нет.
        return await run_reconcile(source_id, settings, sm)

    run_id = await _start_run(sm, source_id, "incremental")
    stats = _new_stats()
    since = source.sync_cursor - timedelta(minutes=source.overlap_minutes)
    cursor = source.sync_cursor
    adapter = open_source(source, settings)
    try:
        async for page in adapter.iter_documents(modified_since=since):
            async with sm() as s, s.begin():
                src = await s.get(Source, source_id)
                assert src is not None
                coll = await source_collection(s, src)
                for ref in page:
                    stats["seen"] += 1
                    await apply_ref(s, src, coll.id, ref, settings, PRIORITY_CHANGED, stats)
                    cursor = max(cursor, ref.modified_at, ref.deleted_at or cursor)
                src.sync_cursor = cursor  # сохраняем после каждой страницы
        common = await adapter.common_ids()
        async with sm() as s, s.begin():
            stats["common_changed"] = await _refresh_common(s, source_id, common)
    except SourceError as exc:
        await _finish_run(sm, run_id, source_id, stats, str(exc))
        log.warning("Синхронизация источника %s: %s", source.code, exc)
        return stats
    finally:
        await adapter.aclose()
    await _finish_run(sm, run_id, source_id, stats, None, last_sync_at=_now())
    return stats


async def run_reconcile(
    source_id: int, settings: Settings, sm: async_sessionmaker[AsyncSession]
) -> dict[str, int]:
    async with sm() as s:
        source = await s.get(Source, source_id)
    if source is None or not source.enabled:
        return {}
    first_load = source.sync_cursor is None
    priority = PRIORITY_INITIAL if first_load else PRIORITY_CHANGED

    run_id = await _start_run(sm, source_id, "reconcile")
    stats = _new_stats() | {"missing": 0, "requeued": 0}
    seen: set[str] = set()
    alive = 0
    cursor: datetime | None = source.sync_cursor
    adapter = open_source(source, settings)
    try:
        async for page in adapter.iter_documents(modified_since=None):
            async with sm() as s, s.begin():
                src = await s.get(Source, source_id)
                assert src is not None
                coll = await source_collection(s, src)
                for ref in page:
                    stats["seen"] += 1
                    seen.add(ref.external_id)
                    if ref.deleted_at is None:
                        alive += 1
                    await apply_ref(s, src, coll.id, ref, settings, priority, stats)
                    cursor = max(cursor or ref.modified_at, ref.modified_at)
        common = await adapter.common_ids()
    except SourceError as exc:
        await _finish_run(sm, run_id, source_id, stats, str(exc))
        log.warning("Сверка источника %s: %s", source.code, exc)
        return stats
    finally:
        await adapter.aclose()

    async with sm() as s, s.begin():
        # У нас есть, у источника нет — помечаем удалённым (fail-safe: только после полного
        # успешного списка; при обрыве связи выше мы сюда не доходим).
        extra_ids = (
            await s.scalars(
                text(
                    "SELECT id FROM documents WHERE source_id = :sid AND deleted_at IS NULL "
                    "AND NOT (external_id = ANY(:ids))"
                ),
                {"sid": source_id, "ids": sorted(seen)},
            )
        ).all()
        for doc_id in extra_ids:
            doc = await s.get(Document, doc_id, with_for_update=True)
            assert doc is not None
            if await soft_delete(s, doc, "missing_in_source"):
                stats["missing"] += 1
        stats["common_changed"] = await _refresh_common(s, source_id, common)
        stats["requeued"] = await requeue_orphans(s, source_id, settings)

    await _finish_run(
        sm,
        run_id,
        source_id,
        stats,
        None,
        last_reconcile_at=_now(),
        last_sync_at=_now(),
        source_total=alive,
        sync_cursor=cursor or _now(),
    )
    return stats


async def requeue_orphans(session: AsyncSession, source_id: int, settings: Settings) -> int:
    """Документы в статусе «в очереди» без задачи в очереди (например, задачу удалили)."""
    rows = await session.execute(
        text(
            """
            SELECT d.id FROM documents d
            WHERE d.source_id = :sid AND d.deleted_at IS NULL
              AND d.status IN ('queued', 'processing')
              AND NOT EXISTS (
                SELECT 1 FROM jobs j
                WHERE j.kind = :kind AND j.status IN ('queued', 'running')
                  AND (j.payload->>'document_id')::bigint = d.id
              )
            """
        ),
        {"sid": source_id, "kind": JOB_KIND},
    )
    n = 0
    for (doc_id,) in rows.all():
        await enqueue_document(session, doc_id, settings, PRIORITY_INITIAL)
        n += 1
    return n


# ---------------------------------------------------------------------------
# Планировщик: worker раз в полминуты проверяет, не пора ли синхронизироваться
# ---------------------------------------------------------------------------


async def enqueue_sync(session: AsyncSession, source_id: int, kind: str = SYNC_JOB) -> int | None:
    return await queue.enqueue(
        session,
        kind,
        {"source_id": source_id},
        priority=30,  # сама синхронизация быстрая и только ставит задачи
        dedupe_key=f"{kind}:{source_id}",
        max_attempts=1,
    )


async def _job_active(session: AsyncSession, kind: str, source_id: int) -> bool:
    n = await session.scalar(
        text(
            "SELECT count(*) FROM jobs WHERE kind = :kind AND status IN ('queued', 'running') "
            "AND (payload->>'source_id')::bigint = :sid"
        ),
        {"kind": kind, "sid": source_id},
    )
    return bool(n)


def reconcile_due(source: Source, now: datetime, tz: ZoneInfo) -> bool:
    """Пора ли ночной сверке: местное время прошло reconcile_hour, а сегодня ещё не сверяли."""
    local = now.astimezone(tz)
    if local.hour < source.reconcile_hour:
        return False
    if source.last_reconcile_at is None:
        return True
    last_local = source.last_reconcile_at.astimezone(tz)
    today_slot = local.replace(hour=source.reconcile_hour, minute=0, second=0, microsecond=0)
    return last_local < today_slot


async def schedule_tick(session: AsyncSession, settings: Settings) -> list[tuple[str, int]]:
    """Ставит в очередь синхронизации и сверки, которым пора. Возвращает, что поставлено."""
    now = _now()
    tz = ZoneInfo(settings.app_timezone)
    planned: list[tuple[str, int]] = []
    sources = (await session.scalars(select(Source).where(Source.enabled.is_(True)))).all()
    for src in sources:
        if await _job_active(session, SYNC_JOB, src.id) or await _job_active(
            session, RECONCILE_JOB, src.id
        ):
            continue
        if src.sync_cursor is not None and reconcile_due(src, now, tz):
            await enqueue_sync(session, src.id, RECONCILE_JOB)
            planned.append((RECONCILE_JOB, src.id))
            continue
        last = src.last_checked_at
        if last is None or now - last >= timedelta(minutes=src.sync_interval_minutes):
            await enqueue_sync(session, src.id, SYNC_JOB)
            planned.append((SYNC_JOB, src.id))
    return planned


async def handle_sync(job: ClaimedJob, settings: Settings, sm: async_sessionmaker) -> None:  # type: ignore[type-arg]
    await run_incremental(int(job.payload["source_id"]), settings, sm)


async def handle_reconcile(job: ClaimedJob, settings: Settings, sm: async_sessionmaker) -> None:  # type: ignore[type-arg]
    await run_reconcile(int(job.payload["source_id"]), settings, sm)


__all__ = [
    "RECONCILE_JOB",
    "SYNC_JOB",
    "apply_ref",
    "enqueue_sync",
    "reconcile_due",
    "run_incremental",
    "run_reconcile",
    "schedule_tick",
]
