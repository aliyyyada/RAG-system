"""Связи «документ → документ» по правилам и статус документа (задача worker'а `graph_rules`).

Задача ставится после каждого разбора документа (pipeline._store). Модель не нужна,
документ обрабатывается за миллисекунды:

1. Старые связи документа удалены вместе со старыми фрагментами (каскад по chunk_id).
2. В каждом фрагменте ищем ссылки (app/graph/references.py) и записываем связи.
3. Цель ищем в реестре по номеру, виду и дате: «Приказ № 45 от 10.01.2022» → документ
   с number «45», видом «Приказ» и датой 10.01.2022. Нет такого — связь остаётся
   «неразрешённой» (только реквизиты из текста). Когда документ появится, его собственная
   задача `graph_rules` найдёт неразрешённые ссылки на свой номер и привяжет их к себе.
4. Пересчитываем статус целей: «Отменён», «Действует, изменён» (app/graph/status.py).

Пример: пришёл приказ № 140 «О внесении изменений в приказ № 112» → связь amends 140 → 112,
статус приказа № 112 становится «Действует, изменён».
"""

import logging
from datetime import date

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.graph import status as legal
from app.graph.references import Reference, find_references, normalize_number, type_matches
from app.models import Chunk, Document, DocumentLink
from app.worker import queue
from app.worker.queue import ClaimedJob

log = logging.getLogger(__name__)

RULES_JOB = "graph_rules"
# Выше векторизации (1): правила быстрые, а статус «Отменён» важен для ответа.
PRIORITY_RULES = 2


async def enqueue_rules(session: AsyncSession, document_id: int) -> int | None:
    return await queue.enqueue(
        session,
        RULES_JOB,
        {"document_id": document_id},
        PRIORITY_RULES,
        dedupe_key=f"graph_rules:{document_id}",
        max_attempts=3,
    )


async def resolve_target(
    session: AsyncSession,
    ref_type: str | None,
    number_norm: str,
    ref_date: date | None,
    *,
    exclude_id: int | None = None,
    prefer_source_id: int | None = None,
) -> int | None:
    """id документа реестра по реквизитам ссылки или None (нет или неоднозначно)."""
    core = number_norm.split("-")[0] if number_norm else ""
    if not core:
        return None
    rows = (
        await session.execute(
            select(
                Document.id,
                Document.number,
                Document.doc_type,
                Document.doc_date,
                Document.source_id,
            )
            .where(
                Document.deleted_at.is_(None),
                Document.number.is_not(None),
                func.lower(Document.number).contains(core),
            )
            .limit(200)
        )
    ).all()
    cands = [
        r
        for r in rows
        if r.id != exclude_id
        and normalize_number(r.number or "") == number_norm
        and type_matches(ref_type, r.doc_type)
    ]
    if ref_date is not None:
        dated = [r for r in cands if r.doc_date == ref_date]
        # Дата в ссылке есть, но не совпала ни с одним документом: это другой документ
        # с тем же номером (номера приказов повторяются каждый год).
        cands = dated if dated or not any(r.doc_date for r in cands) else []
    if len(cands) > 1 and prefer_source_id is not None:
        same = [r for r in cands if r.source_id == prefer_source_id]
        cands = same or cands
    if len(cands) > 1:
        # Несколько приказов № 5 без даты в ссылке — не угадываем.
        return None
    return cands[0].id if cands else None


def _is_self(ref: Reference, doc: Document) -> bool:
    if not doc.number or normalize_number(doc.number) != ref.number_norm:
        return False
    if ref.doc_date is not None and doc.doc_date is not None and ref.doc_date != doc.doc_date:
        return False
    return type_matches(ref.doc_type, doc.doc_type)


async def extract_links(session: AsyncSession, doc: Document) -> list[DocumentLink]:
    """Связи из фрагментов документа (старые удаляются). Без commit."""
    await session.execute(delete(DocumentLink).where(DocumentLink.from_document_id == doc.id))
    chunks = (
        await session.execute(
            select(Chunk.id, Chunk.text).where(Chunk.document_id == doc.id).order_by(Chunk.ord)
        )
    ).all()
    links: list[DocumentLink] = []
    seen: set[tuple[int, str, str]] = set()
    for chunk_id, text in chunks:
        for ref in find_references(text):
            if _is_self(ref, doc):
                continue
            key = (chunk_id, ref.kind, ref.label)
            if key in seen:
                continue
            seen.add(key)
            target = await resolve_target(
                session,
                ref.doc_type,
                ref.number_norm,
                ref.doc_date,
                exclude_id=doc.id,
                prefer_source_id=doc.source_id,
            )
            link = DocumentLink(
                from_document_id=doc.id,
                chunk_id=chunk_id,
                kind=ref.kind,
                target_document_id=target,
                target_type=ref.doc_type,
                target_number=ref.number[:100],
                target_number_norm=ref.number_norm[:100],
                target_date=ref.doc_date,
                target_label=ref.label,
                quote=ref.quote,
            )
            session.add(link)
            links.append(link)
    await session.flush()
    return links


async def attach_unresolved(session: AsyncSession, doc: Document) -> set[int]:
    """Неразрешённые ссылки других документов на этот документ — привязать к нему.

    Возвращает id документов, чьи связи изменились (их статус пересчитывать не нужно —
    меняется статус самого doc, его пересчитает вызывающий)."""
    if not doc.number or doc.deleted_at is not None:
        return set()
    norm = normalize_number(doc.number)
    rows = (
        await session.execute(
            select(DocumentLink, Document.source_id)
            .join(Document, Document.id == DocumentLink.from_document_id)
            .where(
                DocumentLink.target_document_id.is_(None),
                DocumentLink.target_number_norm == norm,
                DocumentLink.from_document_id != doc.id,
            )
        )
    ).all()
    changed: set[int] = set()
    for link, source_id in rows:
        target = await resolve_target(
            session,
            link.target_type,
            link.target_number_norm,
            link.target_date,
            exclude_id=link.from_document_id,
            prefer_source_id=source_id,
        )
        if target == doc.id:
            link.target_document_id = doc.id
            changed.add(link.from_document_id)
    return changed


async def handle_rules(
    job: ClaimedJob, settings: Settings, sm: async_sessionmaker[AsyncSession]
) -> None:
    document_id = int(job.payload["document_id"])
    async with sm() as s, s.begin():
        doc = await s.get(Document, document_id, with_for_update=True)
        if doc is None:
            return
        # Цели, на которые документ ссылался до переразбора: их статус мог измениться.
        before = set(
            (
                await s.scalars(
                    select(DocumentLink.target_document_id).where(
                        DocumentLink.from_document_id == doc.id,
                        DocumentLink.target_document_id.is_not(None),
                    )
                )
            ).all()
        )
        links = await extract_links(s, doc)
        await attach_unresolved(s, doc)
        doc.links_version = doc.indexed_version
        targets = before | {lk.target_document_id for lk in links if lk.target_document_id}
        await legal.refresh(s, targets | {doc.id})
        # Модель (шаг 2) — только если администратор включил извлечение.
        from app.graph import extract  # noqa: PLC0415 — избегаем цикла импортов

        if await extract.llm_enabled(s):
            await extract.enqueue_extract(s, doc.id, extract.PRIORITY_NEW)
        # Противоречия (этап 6) — после связей: им нужен статус «Отменён» и связи документа.
        from app.contradictions import scan as contra  # noqa: PLC0415

        if await contra.enabled(s):
            await contra.enqueue_scan(s, doc.id, contra.PRIORITY_NEW)
    log.debug("graph_rules %s: %d связей", document_id, len(links))


async def on_document_gone(session: AsyncSession, document_id: int) -> None:
    """Документ удалён в источнике или исключён из поиска: его «отмены» больше не действуют."""
    targets = set(
        (
            await session.scalars(
                select(DocumentLink.target_document_id).where(
                    DocumentLink.from_document_id == document_id,
                    DocumentLink.target_document_id.is_not(None),
                )
            )
        ).all()
    )
    await legal.refresh(session, targets | {document_id})


async def backfill_tick(session: AsyncSession, limit: int = 500) -> int:
    """Документы, для текущей версии которых связи ещё не строились (например, загруженные
    до этапа 5) — в очередь."""
    ids = (
        await session.scalars(
            select(Document.id)
            .where(
                Document.indexed_version.is_not(None),
                Document.deleted_at.is_(None),
                (Document.links_version.is_(None))
                | (Document.links_version != Document.indexed_version),
            )
            .limit(limit)
        )
    ).all()
    n = 0
    for doc_id in ids:
        if await enqueue_rules(session, int(doc_id)) is not None:
            n += 1
    return n


async def rebuild_all(session: AsyncSession) -> int:
    """Кнопка «Пересобрать связи» в админке: правила изменились — пройти все документы."""
    await session.execute(update(Document).values(links_version=None))
    return await backfill_tick(session, limit=100_000)
