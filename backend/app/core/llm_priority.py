"""Приоритет пользователей над фоновыми задачами при обращении к LLM.

Видеокарта одна (GTX 1070, 8 ГБ), модель одна. Правило из ТЗ: пока пользователь
ждёт ответ, фоновые задачи к LLM не обращаются.

Как это сделано (без Redis, средствами PostgreSQL):
- каждый пользовательский запрос к LLM держит *разделяемую* advisory-блокировку
  `LLM_LOCK_KEY` на время генерации ответа (`user_llm_activity`);
- worker перед каждым вызовом LLM пробует на мгновение взять *исключительную*
  блокировку того же ключа (`background_llm_allowed`). Получилось — значит,
  сейчас никто из пользователей не ждёт, блокировку сразу отпускаем и идём в LLM.
  Не получилось — ждём и пробуем снова.

Advisory-блокировки привязаны к соединению: если процесс app упадёт посреди ответа,
PostgreSQL сам снимет блокировку, и worker не застрянет навсегда.

Ограничение: если worker уже отправил в Ollama запрос на 20 секунд, пользователь
подождёт его окончания (Ollama обрабатывает запросы по очереди). Поэтому фоновые
вызовы LLM делаем короткими (один чанк за раз).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# Произвольная константа: ключ advisory-блокировки «LLM занята пользователями».
LLM_LOCK_KEY = 7_340_001


@asynccontextmanager
async def user_llm_activity(conn: AsyncConnection) -> AsyncIterator[None]:
    """Отметить, что пользователь ждёт ответ LLM (на время блока `async with`)."""
    await conn.execute(text("SELECT pg_advisory_lock_shared(:k)"), {"k": LLM_LOCK_KEY})
    try:
        yield
    finally:
        await conn.execute(text("SELECT pg_advisory_unlock_shared(:k)"), {"k": LLM_LOCK_KEY})


async def background_llm_allowed(conn: AsyncConnection, queue_stale_seconds: int = 30) -> bool:
    """True, если сейчас ни один пользователь не ждёт ответа LLM.

    «Ждёт» — это и генерация ответа (разделяемая блокировка), и стояние в очереди к модели
    (живая запись в llm_queue, этап 4): иначе worker влезал бы между ответами двум людям.
    """
    waiting = (
        await conn.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM llm_queue "
                "WHERE heartbeat_at > now() - make_interval(secs => :s))"
            ),
            {"s": queue_stale_seconds},
        )
    ).scalar_one()
    if waiting:
        return False
    got = (
        await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LLM_LOCK_KEY})
    ).scalar_one()
    if got:
        await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LLM_LOCK_KEY})
    return bool(got)
