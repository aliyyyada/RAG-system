"""Очередь к модели с местом в очереди.

Видеокарта одна, Ollama отвечает по одному (OLLAMA_NUM_PARALLEL=1). Если просто отправить
в неё 5 запросов, все 5 человек будут молча ждать. Поэтому очередь у нас: каждый запрос
записывается в llm_queue и видит, сколько записей впереди — «Ожидание модели: вы 2-й в очереди».

Порядок: сначала вошедшие пользователи (priority 0), потом гости (priority 1), внутри —
по времени. Ответ, который уже генерируется, не прерывается.

Каждый ждущий поток раз в несколько секунд обновляет heartbeat_at. Если процесс упал,
запись перестаёт учитываться через LLM_QUEUE_STALE_SECONDS, и очередь не застревает.
Пока в очереди есть живые записи, worker не обращается к LLM (app/core/llm_priority.py).
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator

from sqlalchemy import text

from app.db import get_sessionmaker

# Ключ advisory-блокировки «кто-то сейчас решает, чья очередь».
QUEUE_LOCK_KEY = 7_340_002
HEARTBEAT_EVERY_SECONDS = 5.0

PRIORITY_USER = 0
PRIORITY_GUEST = 1


class LlmQueue:
    def __init__(self, priority: int, stale_seconds: int) -> None:
        self.priority = priority
        self.stale = stale_seconds
        self.id: int | None = None
        self._beat: asyncio.Task[None] | None = None

    async def enter(self) -> None:
        async with get_sessionmaker()() as s, s.begin():
            self.id = (
                await s.execute(
                    text("INSERT INTO llm_queue (priority) VALUES (:p) RETURNING id"),
                    {"p": self.priority},
                )
            ).scalar_one()
        self._beat = asyncio.ensure_future(self._heartbeat())

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_EVERY_SECONDS)
            async with get_sessionmaker()() as s, s.begin():
                await s.execute(
                    text("UPDATE llm_queue SET heartbeat_at = now() WHERE id = :id"),
                    {"id": self.id},
                )

    _AHEAD = """
        SELECT count(*) FROM llm_queue q
        WHERE q.id <> :id
          AND q.heartbeat_at > now() - make_interval(secs => :stale)
          AND (q.started_at IS NOT NULL OR (q.priority, q.id) < (:p, :id))
    """

    async def try_start(self) -> int:
        """0 — наша очередь, модель наша (started_at выставлен). Иначе — сколько впереди."""
        async with get_sessionmaker()() as s, s.begin():
            await s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": QUEUE_LOCK_KEY})
            ahead = (
                await s.execute(
                    text(self._AHEAD), {"id": self.id, "p": self.priority, "stale": self.stale}
                )
            ).scalar_one()
            if ahead == 0:
                await s.execute(
                    text(
                        "UPDATE llm_queue SET started_at = now(), heartbeat_at = now() "
                        "WHERE id = :id"
                    ),
                    {"id": self.id},
                )
            return int(ahead)

    async def wait_turn(self, poll_seconds: float = 0.5) -> AsyncIterator[int]:
        """Выдаёт место в очереди (1 — следующий), пока не подойдёт наша очередь."""
        last: int | None = None
        while True:
            ahead = await self.try_start()
            if ahead == 0:
                return
            if ahead != last:
                last = ahead
                yield ahead
            await asyncio.sleep(poll_seconds)

    async def leave(self) -> None:
        if self._beat is not None:
            self._beat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._beat
            self._beat = None
        if self.id is not None:
            async with get_sessionmaker()() as s, s.begin():
                await s.execute(text("DELETE FROM llm_queue WHERE id = :id"), {"id": self.id})
            self.id = None
