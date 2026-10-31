"""Проверки состояния: используются healthcheck'ом Docker и страницей «Состояние системы»."""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.sse import SSE_HEADERS, format_event, with_heartbeat
from app.db import get_session

router = APIRouter(prefix="/api", tags=["health"])
log = logging.getLogger(__name__)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get("/health")
async def liveness() -> dict[str, str]:
    """Процесс жив. Без обращений к БД, чтобы Docker не перезапускал app из-за БД."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, object]:
    """Готовность: БД обязательна, Ollama — информационно (поиск без LLM всё равно работает)."""
    try:
        await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:  # noqa: BLE001 — любая ошибка БД = не готов
        db_ok = False

    ollama: dict[str, object] = {"ok": False}
    try:
        async with httpx.AsyncClient(base_url=settings.ollama_url, timeout=2.0) as client:
            tags = (await client.get("/api/tags")).json()
        ollama = {"ok": True, "models": sorted(m["name"] for m in tags.get("models", []))}
    except Exception as exc:  # noqa: BLE001
        log.debug("Ollama недоступна: %s", exc)

    if not db_ok:
        raise HTTPException(status_code=503, detail={"database": False, "ollama": ollama})
    return {"status": "ready", "database": True, "ollama": ollama}


@router.get("/diag/sse")
async def sse_diagnostic(
    settings: SettingsDep,
    seconds: Annotated[int, Query(ge=1, le=60)] = 10,
) -> StreamingResponse:
    """Проверка потоковой передачи через nginx и Windows.

    Сервер молчит `seconds` секунд (как LLM перед первым токеном), затем шлёт событие.
    Если соединение не рвётся и в браузере видны `: ping`, heartbeat работает.
    Пример: curl -k -N https://localhost/api/diag/sse?seconds=20
    """

    async def slow_events() -> AsyncIterator[bytes]:
        await asyncio.sleep(seconds)
        yield format_event({"text": f"Прошло {seconds} с, соединение живо"}, event="done")

    return StreamingResponse(
        with_heartbeat(slow_events(), settings.sse_heartbeat_seconds),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
