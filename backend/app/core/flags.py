"""Переключатели админки в таблице app_flags (без правки .env и перезапуска)."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppFlag

GRAPH_LLM_ENABLED = "graph_llm_enabled"
CONTRA_ENABLED = "contradictions_enabled"


async def get_flag(session: AsyncSession, key: str, default: Any = None) -> Any:
    value = await session.scalar(select(AppFlag.value).where(AppFlag.key == key))
    return default if value is None else value


async def set_flag(session: AsyncSession, key: str, value: Any, by: str | None) -> None:
    stmt = insert(AppFlag).values(key=key, value=value, updated_by=by)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[AppFlag.key],
            set_={"value": stmt.excluded.value, "updated_by": by, "updated_at": datetime.now(UTC)},
        )
    )
