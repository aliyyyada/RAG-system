import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.llm_priority import background_llm_allowed, user_llm_activity

pytestmark = pytest.mark.db


async def test_background_waits_while_user_is_waiting_for_answer(engine: AsyncEngine) -> None:
    async with engine.connect() as user1, engine.connect() as user2, engine.connect() as worker:
        assert await background_llm_allowed(worker) is True

        async with user_llm_activity(user1):
            # Несколько пользователей одновременно не мешают друг другу (разделяемая блокировка)
            async with user_llm_activity(user2):
                assert await background_llm_allowed(worker) is False
            assert await background_llm_allowed(worker) is False

        assert await background_llm_allowed(worker) is True


async def test_lock_released_when_app_connection_dies(engine: AsyncEngine) -> None:
    async with engine.connect() as worker:
        conn = await engine.connect()
        activity = user_llm_activity(conn)
        await activity.__aenter__()
        assert await background_llm_allowed(worker) is False
        await conn.invalidate()  # имитация падения процесса app
        await conn.close()
        assert await background_llm_allowed(worker) is True
