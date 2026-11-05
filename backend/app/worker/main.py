"""Worker: выполняет фоновые задачи из очереди в PostgreSQL (контейнер worker).

Обработчики регистрируются в HANDLERS. Этап 2: синхронизация с источником, ночная сверка,
обработка документа. Этап 4: векторизация фрагментов. Этап 5: граф (связи по правилам,
извлечение сущностей моделью). Этап 6: поиск противоречий.
Раз в SCHEDULE_EVERY_SECONDS worker сам ставит синхронизации, которым пора (планировщик),
досылает на векторизацию фрагменты без вектора и удаляет историю чатов старше срока.
"""

import asyncio
import logging
import os
import signal
import socket
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat.history import cleanup_old_chats
from app.config import Settings, get_settings
from app.contradictions import scan as contra_scan
from app.db import dispose_engine, get_sessionmaker
from app.graph import estimate as graph_estimate
from app.graph import extract as graph_extract
from app.graph import rules as graph_rules
from app.ingest import pipeline, sync
from app.ingest.errors import IngestError
from app.ingest.parse_pool import shutdown_pool
from app.search import indexer
from app.worker import queue
from app.worker.queue import ClaimedJob, JobDeferred
from epoz.checks import runner as epoz_runner
from epoz.checks import service as epoz_service

log = logging.getLogger("worker")

Handler = Callable[[ClaimedJob, Settings, async_sessionmaker[AsyncSession]], Awaitable[None]]

SCHEDULE_EVERY_SECONDS = 30


async def _noop(job: ClaimedJob, settings: Settings, sm: async_sessionmaker[AsyncSession]) -> None:
    log.info("noop job: %s", job.payload)


HANDLERS: dict[str, Handler] = {
    "noop": _noop,
    pipeline.JOB_KIND: pipeline.process_document,
    sync.SYNC_JOB: sync.handle_sync,
    sync.RECONCILE_JOB: sync.handle_reconcile,
    indexer.EMBED_JOB: indexer.handle_embed,
    graph_rules.RULES_JOB: graph_rules.handle_rules,
    graph_extract.EXTRACT_JOB: graph_extract.handle_extract,
    graph_estimate.ESTIMATE_JOB: graph_estimate.handle_estimate,
    epoz_runner.JOB_KIND: epoz_runner.handle_check_run,
    contra_scan.SCAN_JOB: contra_scan.handle_scan,
}


# Задачи, которые обращаются к Ollama (видеокарта). Они идут своей полосой строго по одной:
# в видеокарту одновременно отправляется не больше одного фонового запроса, и вопрос
# пользователя ждёт не дольше, чем раньше. Остальные задачи (синхронизация, разбор PDF/OCR,
# связи по правилам) нагружают процессор и идут второй полосой параллельно.
GPU_KINDS: frozenset[str] = frozenset(
    {
        indexer.EMBED_JOB,
        graph_extract.EXTRACT_JOB,
        graph_estimate.ESTIMATE_JOB,
        contra_scan.SCAN_JOB,
        epoz_runner.JOB_KIND,
    }
)


async def run_once(
    worker_id: str,
    *,
    only: frozenset[str] | None = None,
    exclude: frozenset[str] | None = None,
) -> bool:
    """Берёт и выполняет одну задачу. Возвращает False, если очередь пуста."""
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        job = await queue.dequeue(session, worker_id, only=only, exclude=exclude)
    if job is None:
        return False

    handler = HANDLERS.get(job.kind)
    try:
        if handler is None:
            raise LookupError(f"Нет обработчика для задачи {job.kind!r}")
        await handler(job, get_settings(), sm)
    except JobDeferred as d:
        log.debug("job %s (%s) отложена на %s с: %s", job.id, job.kind, d.seconds, d.reason)
        async with sm() as session, session.begin():
            await queue.defer(session, job.id, d.seconds, d.reason)
    except Exception as exc:  # noqa: BLE001 — любую ошибку пишем в задачу
        if isinstance(exc, IngestError):
            # Ожидаемая ошибка данных (битый файл, неполная выгрузка): без трассировки.
            log.warning("job %s (%s): %s", job.id, job.kind, exc)
        else:
            log.exception("job %s (%s) failed", job.id, job.kind)
        delay = get_settings().document_retry_delay_seconds
        async with sm() as session, session.begin():
            await queue.mark_failed(session, job.id, f"{type(exc).__name__}: {exc}", delay)
    else:
        async with sm() as session, session.begin():
            await queue.mark_done(session, job.id)
    return True


async def _schedule_tick(settings: Settings) -> None:
    """Планировщик: синхронизации, которым пора, досылка векторизации, чистка старого."""
    try:
        async with get_sessionmaker()() as session, session.begin():
            planned = await sync.schedule_tick(session, settings)
            embeds = await indexer.backfill_tick(session)
            removed = await cleanup_old_chats(session, settings.chat_history_days)
            links = await graph_rules.backfill_tick(session)
            extracts = await graph_extract.backfill_tick(session)
            await graph_extract.cleanup_orphans(session)
            gone = await epoz_service.cleanup_expired(session, settings)
            contra = await contra_scan.backfill_tick(session)
        if planned:
            log.info("scheduled: %s", planned)
        if embeds:
            log.info("на векторизацию поставлено документов: %d", embeds)
        if links or extracts:
            log.info("граф: связи для %d документов, модель для %d", links, extracts)
        if contra:
            log.info("противоречия: на проверку поставлено документов: %d", contra)
        if removed:
            log.info("удалено чатов старше %d дней: %d", settings.chat_history_days, removed)
        if gone:
            log.info("проверки ЕПоЗ: удалено по окончании сессии или простою: %d", gone)
    except Exception:  # noqa: BLE001 — планировщик не должен ронять worker
        log.exception("scheduler error")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async with get_sessionmaker()() as session, session.begin():
        n = await queue.requeue_stale(session, settings.worker_stale_after_seconds)
    log.info("worker %s started, requeued %d stale jobs", worker_id, n)

    async def scheduler() -> None:
        while not stop.is_set():
            await _schedule_tick(settings)
            try:
                await asyncio.wait_for(stop.wait(), timeout=SCHEDULE_EVERY_SECONDS)
            except TimeoutError:
                pass

    async def lane(name: str, **kinds: frozenset[str] | None) -> None:
        lane_id = f"{worker_id}/{name}" if name else worker_id
        while not stop.is_set():
            try:
                busy = await run_once(lane_id, **kinds)
            except Exception:  # noqa: BLE001 — например, БД ещё не поднялась
                log.exception("worker loop error")
                busy = False
            if not busy:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_seconds)
                except TimeoutError:
                    pass

    tasks = []
    if settings.scheduler_enabled:
        tasks.append(scheduler())
    if settings.worker_split_lanes:
        tasks.append(lane("cpu", exclude=GPU_KINDS))
        tasks.append(lane("gpu", only=GPU_KINDS))
    else:
        tasks.append(lane(""))
    try:
        await asyncio.gather(*tasks)
    finally:
        shutdown_pool()
    await dispose_engine()
    log.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
