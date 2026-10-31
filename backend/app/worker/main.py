"""Worker: выполняет фоновые задачи из очереди в PostgreSQL (контейнер worker).

Обработчики задач (синхронизация, парсинг, векторизация, граф, противоречия)
появятся на следующих этапах и регистрируются в HANDLERS.
"""

import asyncio
import logging
import os
import signal
import socket
from collections.abc import Awaitable, Callable
from typing import Any

from app.config import get_settings
from app.db import dispose_engine, get_sessionmaker
from app.worker import queue

log = logging.getLogger("worker")

Handler = Callable[[dict[str, Any]], Awaitable[None]]


async def _noop(payload: dict[str, Any]) -> None:
    log.info("noop job: %s", payload)


HANDLERS: dict[str, Handler] = {"noop": _noop}


async def run_once(worker_id: str) -> bool:
    """Берёт и выполняет одну задачу. Возвращает False, если очередь пуста."""
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        job = await queue.dequeue(session, worker_id)
    if job is None:
        return False

    handler = HANDLERS.get(job.kind)
    try:
        if handler is None:
            raise LookupError(f"Нет обработчика для задачи {job.kind!r}")
        await handler(job.payload)
    except Exception as exc:  # noqa: BLE001 — любую ошибку пишем в задачу
        log.exception("job %s (%s) failed", job.id, job.kind)
        async with sm() as session, session.begin():
            await queue.mark_failed(session, job.id, f"{type(exc).__name__}: {exc}")
    else:
        async with sm() as session, session.begin():
            await queue.mark_done(session, job.id)
    return True


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

    while not stop.is_set():
        try:
            busy = await run_once(worker_id)
        except Exception:  # noqa: BLE001 — например, БД ещё не поднялась
            log.exception("worker loop error")
            busy = False
        if not busy:
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_seconds)
            except TimeoutError:
                pass
    await dispose_engine()
    log.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
