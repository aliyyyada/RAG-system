"""Вызов модели из проверки: в общей очереди к видеокарте и с приоритетом пользователей.

Проверка запускается пользователем, но она долгая, поэтому каждый вызов модели встаёт в очередь
`llm_queue` **после** вопросов чата (priority 1): пока кто-то ждёт ответа в чате, он пройдёт
между вызовами проверки. На время вызова держится разделяемая блокировка «пользователь ждёт
модель» — фоновые задачи (векторизация, граф) в это время к модели не обращаются.

Если модели нет (Ollama недоступна, выключено настройкой `EPOZ_USE_LLM=false`), функция
возвращает `None`, и критерий использует запасной путь (правила или «ручная проверка»).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.chat.llm_queue import LlmQueue
from app.config import Settings
from app.core.llm_priority import user_llm_activity
from app.db import get_engine
from app.llm.ollama import Ollama, OllamaError

log = logging.getLogger(__name__)

PRIORITY_CHECK = 1


async def ask_json(
    settings: Settings,
    system: str,
    user: str,
    schema: dict[str, Any],
    *,
    max_tokens: int = 400,
) -> tuple[dict[str, Any] | None, int]:
    """Ответ модели по JSON-схеме и время в мс; (None, 0) — модели нет или она не ответила."""
    if not settings.epoz_use_llm:
        return None, 0
    queue = LlmQueue(PRIORITY_CHECK, settings.llm_queue_stale_seconds)
    try:
        await queue.enter()
        async for _position in queue.wait_turn():
            pass
        t0 = time.perf_counter()
        async with get_engine().connect() as conn, user_llm_activity(conn):
            async with Ollama(settings) as client:
                data, _stats = await client.chat_json(
                    [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    schema,
                    max_tokens=max_tokens,
                )
        return (data if isinstance(data, dict) else None), int((time.perf_counter() - t0) * 1000)
    except OllamaError as exc:
        log.warning("Проверка ЕПоЗ: модель не ответила: %s", exc)
        return None, 0
    finally:
        await queue.leave()
