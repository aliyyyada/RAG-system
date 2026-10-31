import asyncio
from collections.abc import AsyncIterator

from httpx import ASGITransport, AsyncClient

from app.core.sse import HEARTBEAT, format_event, with_heartbeat
from app.main import create_app


async def _collect(it: AsyncIterator[bytes]) -> list[bytes]:
    return [chunk async for chunk in it]


async def test_heartbeat_sent_while_source_is_silent() -> None:
    async def slow() -> AsyncIterator[bytes]:
        await asyncio.sleep(0.35)
        yield b"data: 1\n\n"

    chunks = await _collect(with_heartbeat(slow(), heartbeat_seconds=0.1))
    assert chunks[-1] == b"data: 1\n\n"
    assert chunks.count(HEARTBEAT) >= 2


async def test_no_heartbeat_when_source_is_fast() -> None:
    async def fast() -> AsyncIterator[bytes]:
        for i in range(3):
            yield f"data: {i}\n\n".encode()

    assert HEARTBEAT not in await _collect(with_heartbeat(fast(), heartbeat_seconds=1))


def test_format_event_keeps_cyrillic_and_multiline() -> None:
    raw = format_event({"t": "Приказ №12"}, event="token").decode()
    assert raw == 'event: token\ndata: {"t": "Приказ №12"}\n\n'


async def test_sse_diag_endpoint_streams_ping_then_done(monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setenv("SSE_HEARTBEAT_SECONDS", "0.3")
    get_settings.cache_clear()
    try:
        async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://t") as c:
            r = await c.get("/api/diag/sse", params={"seconds": 1})
        assert r.headers["content-type"].startswith("text/event-stream")
        assert ": ping" in r.text
        assert "event: done" in r.text
    finally:
        get_settings.cache_clear()
