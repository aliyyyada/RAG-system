"""Действия с источниками и документами, общие для API админки и консольных команд."""

import hashlib
import secrets

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.graph import rules as graph_rules
from app.ingest.pipeline import (
    PRIORITY_CHANGED,
    PRIORITY_RETRY,
    add_event,
    enqueue_document,
    set_status,
)
from app.ingest.sync import RECONCILE_JOB, SYNC_JOB, enqueue_sync, source_collection
from app.models import Collection, Document, Source


class ServiceError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


async def create_source(
    session: AsyncSession,
    *,
    code: str,
    name: str,
    base_url: str,
    adapter: str = "http_json",
    sync_interval_minutes: int = 15,
    reconcile_hour: int = 2,
) -> Source:
    if await session.scalar(select(Source).where(Source.code == code)):
        raise ServiceError("code_taken", f"Источник с кодом {code} уже есть")
    if await session.scalar(select(Collection).where(Collection.name == name)):
        raise ServiceError("name_taken", f"Коллекция «{name}» уже есть")
    source = Source(
        code=code,
        name=name,
        base_url=base_url,
        adapter=adapter,
        sync_interval_minutes=sync_interval_minutes,
        reconcile_hour=reconcile_hour,
    )
    session.add(source)
    await session.flush()
    await source_collection(session, source)
    return source


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def issue_callback_token(session: AsyncSession, source: Source) -> str:
    """Новый токен для запросов источника «что у вас загружено». Старый перестаёт работать."""
    token = f"src_{source.code}_{secrets.token_urlsafe(32)}"
    source.callback_token_hash = hash_token(token)
    return token


async def request_sync(session: AsyncSession, source: Source, *, reconcile: bool = False) -> bool:
    """Ручной запуск синхронизации. False — такая уже стоит в очереди."""
    job_id = await enqueue_sync(session, source.id, RECONCILE_JOB if reconcile else SYNC_JOB)
    return job_id is not None


async def retry_document(
    session: AsyncSession,
    doc: Document,
    settings: Settings,
    *,
    actor: str | None,
    force: bool = False,
) -> None:
    """Кнопки «Повторить» (документ с ошибкой) и «Переиндексировать» (force=True)."""
    if doc.deleted_at is not None:
        raise ServiceError("deleted", "Документ удалён в источнике")
    doc.attempts = 0
    set_status(doc, "queued")
    await add_event(session, doc.id, "reindex" if force else "retry", doc.version, actor=actor)
    await enqueue_document(
        session, doc.id, settings, PRIORITY_CHANGED if force else PRIORITY_RETRY, force=force
    )


async def retry_failed(
    session: AsyncSession, source_id: int, settings: Settings, *, actor: str | None
) -> int:
    """«Повторить все»: все документы источника со статусом «ошибка»."""
    docs = (
        await session.scalars(
            select(Document).where(
                Document.source_id == source_id,
                Document.status == "error",
                Document.deleted_at.is_(None),
            )
        )
    ).all()
    for doc in docs:
        await retry_document(session, doc, settings, actor=actor)
    return len(docs)


async def set_excluded(
    session: AsyncSession, doc: Document, excluded: bool, *, actor: str | None
) -> None:
    if doc.is_excluded != excluded:
        doc.is_excluded = excluded
        await add_event(session, doc.id, "excluded" if excluded else "included", actor=actor)
        # Исключённый документ больше не «отменяет» другие (этап 5).
        await session.flush()
        await graph_rules.on_document_gone(session, doc.id)


async def processing_rate(session: AsyncSession, source_id: int | None, minutes: int = 30) -> float:
    """Сколько документов в минуту worker обработал за последние `minutes` минут."""
    n = await session.scalar(
        text(
            """
            SELECT count(*) FROM document_events e JOIN documents d ON d.id = e.document_id
            WHERE e.event = 'indexed' AND e.created_at > now() - make_interval(mins => :m)
              AND d.source_id IS NOT DISTINCT FROM :sid
            """
        ),
        {"m": minutes, "sid": source_id},
    )
    return (n or 0) / minutes
