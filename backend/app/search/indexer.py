"""Векторизация фрагментов (задача worker'а `chunk_embed`).

После разбора документа (pipeline._store) ставится задача на векторизацию его фрагментов.
Worker берёт по EMBED_BATCH_SIZE фрагментов без вектора, отправляет их в Ollama (bge-m3)
и записывает векторы. Пока документ не векторизован, он уже находится полнотекстовым
поиском, просто без поиска «по смыслу».

**Пользователи важнее.** Перед каждой пачкой worker проверяет, не ждёт ли кто-то ответа
(app/core/llm_priority.py). Ждёт — задача откладывается на несколько секунд и не тратит
попытку. Ollama недоступна — тоже откладывается (на минуту), а не падает: после её запуска
векторизация продолжится сама.

Фрагменты, оставшиеся без вектора (например, загруженные до этапа 4), находит
`backfill_tick`, который worker вызывает вместе с планировщиком синхронизаций.
"""

import logging

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.llm_priority import background_llm_allowed
from app.ingest.assemble import HeaderMeta, document_caption
from app.llm.ollama import ModelNotFound, Ollama, OllamaUnavailable
from app.models import Chunk, Document, to_pgvector
from app.worker import queue
from app.worker.queue import ClaimedJob, JobDeferred

log = logging.getLogger(__name__)

EMBED_JOB = "chunk_embed"
# Выше первичной индексации (0): документ становится доступен поиску по смыслу сразу после
# разбора, а не через несколько часов, когда разберутся все остальные.
PRIORITY_EMBED = 1


async def enqueue_embedding(session: AsyncSession, document_id: int) -> int | None:
    return await queue.enqueue(
        session,
        EMBED_JOB,
        {"document_id": document_id},
        PRIORITY_EMBED,
        dedupe_key=f"embed:{document_id}",
        max_attempts=5,
    )


def embedding_text(caption: str, section: str | None, body: str) -> str:
    """Текст для вектора: название документа и раздел + сам фрагмент.

    Фрагмент «4.2. Допуск осуществляется по наряду-допуску…» без названия документа
    теряет контекст («какой допуск? к чему?»), а с ним вектор знает, что это приказ
    о работах на высоте.
    """
    head = caption if not section else f"{caption}\n{section}"
    return f"{head}\n\n{body}"


async def handle_embed(
    job: ClaimedJob, settings: Settings, sm: async_sessionmaker[AsyncSession]
) -> None:
    document_id = int(job.payload["document_id"])
    async with Ollama(settings) as client:
        while True:
            async with sm() as s:
                doc = await s.get(Document, document_id)
                if doc is None:
                    return
                caption = document_caption(
                    HeaderMeta(doc.number, doc.title, doc.doc_type, doc.topic, doc.doc_date)
                )
                rows = (
                    await s.execute(
                        select(Chunk.id, Chunk.section, Chunk.text)
                        .where(Chunk.document_id == document_id, Chunk.embedding.is_(None))
                        .order_by(Chunk.ord)
                        .limit(settings.embed_batch_size)
                    )
                ).all()
                if not rows:
                    return
                conn = await s.connection()
                if not await background_llm_allowed(conn, settings.llm_queue_stale_seconds):
                    raise JobDeferred(5, "пользователь ждёт ответ")
            try:
                vectors = await client.embed(
                    [embedding_text(caption, sec, t) for _, sec, t in rows]
                )
            except OllamaUnavailable as exc:
                raise JobDeferred(60, str(exc)) from exc
            except ModelNotFound as exc:
                # bge-m3 не загружена в Ollama (не залили модели при установке).
                raise JobDeferred(300, str(exc)) from exc
            async with sm() as s, s.begin():
                # Пока шла векторизация, документ могли переразобрать: старых фрагментов
                # уже нет, UPDATE их просто не найдёт.
                for (chunk_id, _, _), vec in zip(rows, vectors, strict=True):
                    await s.execute(
                        text("UPDATE chunks SET embedding = CAST(:v AS vector) WHERE id = :id"),
                        {"v": to_pgvector(vec), "id": chunk_id},
                    )


async def backfill_tick(session: AsyncSession, limit: int = 500) -> int:
    """Ставит векторизацию документам, у которых есть фрагменты без вектора."""
    ids = (
        await session.scalars(
            text(
                """
                SELECT DISTINCT c.document_id FROM chunks c
                WHERE c.embedding IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM jobs j
                      WHERE j.dedupe_key = 'embed:' || c.document_id
                        AND j.status IN ('queued', 'running')
                  )
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )
    ).all()
    n = 0
    for doc_id in ids:
        if await enqueue_embedding(session, int(doc_id)) is not None:
            n += 1
    return n


async def embedding_progress(session: AsyncSession) -> dict[str, int]:
    row = (await session.execute(text("SELECT count(*), count(embedding) FROM chunks"))).one()
    return {"chunks": int(row[0]), "embedded": int(row[1])}
