"""Server-Sent Events с heartbeat.

Windows обрывает HTTP-соединение, по которому несколько секунд ничего не приходит.
LLM может «думать» над первым токеном 5–10 секунд, поэтому, пока источник событий
молчит, мы каждые `heartbeat_seconds` отправляем SSE-комментарий `: ping`.
Браузер его игнорирует, а соединение остаётся живым.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

HEARTBEAT = b": ping\n\n"


def format_event(data: Any, event: str | None = None) -> bytes:
    payload = json.dumps(data, ensure_ascii=False)
    lines = []
    if event:
        lines.append(f"event: {event}")
    lines.extend(f"data: {line}" for line in payload.splitlines() or [""])
    return ("\n".join(lines) + "\n\n").encode("utf-8")


async def with_heartbeat(
    events: AsyncIterator[bytes], heartbeat_seconds: float
) -> AsyncIterator[bytes]:
    """Пропускает события из `events` и вставляет heartbeat, пока источник молчит."""
    iterator = aiter(events)
    pending: asyncio.Task[bytes] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(iterator))
            done, _ = await asyncio.wait({pending}, timeout=heartbeat_seconds)
            if not done:
                yield HEARTBEAT
                continue
            try:
                chunk = pending.result()
            except StopAsyncIteration:
                return
            finally:
                pending = None
            yield chunk
    finally:
        if pending is not None:
            pending.cancel()


SSE_HEADERS = {
    "Cache-Control": "no-cache",
    # Подсказка nginx не буферизовать поток (дублирует proxy_buffering off в конфиге).
    "X-Accel-Buffering": "no",
}
