"""История чатов вошедших пользователей.

Гостевой чат на сервере не хранится: история живёт только в браузере гостя (п. 6 ТЗ),
в аудит пишутся IP, время и вопрос. Чаты удаляются через CHAT_HISTORY_DAYS дней (90 по
умолчанию) после последнего вопроса — это делает worker (`cleanup_old_chats`).
"""

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChatMessage

TITLE_MAX = 80


def chat_title(question: str) -> str:
    t = " ".join(question.split())
    return t if len(t) <= TITLE_MAX else t[: TITLE_MAX - 1].rstrip() + "…"


async def recent_turns(session: AsyncSession, chat_id: int, turns: int) -> list[dict[str, str]]:
    """Последние реплики диалога для модели (без незаконченных и ошибочных ответов)."""
    if turns <= 0:
        return []
    rows = (
        await session.scalars(
            select(ChatMessage)
            .where(ChatMessage.chat_id == chat_id, ChatMessage.status == "done")
            .order_by(ChatMessage.id.desc())
            .limit(turns)
        )
    ).all()
    return [{"role": m.role, "content": m.content} for m in reversed(rows)]


async def cleanup_old_chats(session: AsyncSession, days: int) -> int:
    result = await session.execute(
        text("DELETE FROM chats WHERE updated_at < now() - make_interval(days => :d)"),
        {"d": days},
    )
    return int(getattr(result, "rowcount", 0) or 0)
