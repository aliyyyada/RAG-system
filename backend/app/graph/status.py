"""Юридический статус документа по связям «отменяет / изменяет» и шапке источника.

    «Отменён»             — источник так сказал (status «Отменён», «Утратил силу»…) или
                            есть живой документ, который его отменяет (связь cancels);
    «Действует, изменён»  — не отменён, но есть живой документ, который его изменяет;
    иначе                 — статус из шапки источника как есть («Действует», «Проект») или NULL.

«Живой» отменяющий документ — не удалён и не исключён из поиска: если приказ № 112 удалили
в источнике, приказ № 45 перестаёт считаться отменённым им.

Статус считается по ВСЕМ документам, а не только по доступным человеку: если приказ № 45
отменён приказом, который человеку не виден, он всё равно увидит «Отменён» (но не увидит,
каким документом) — иначе человек мог бы работать по отменённому приказу
(docs/open-questions.md, вопрос 24).
"""

from collections.abc import Iterable

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import Document, DocumentLink

CANCELLED = "Отменён"
AMENDED = "Действует, изменён"
# Статусы источника, при которых документ не считается действующим.
INACTIVE_STATUSES = ("отменён", "отменен", "утратил силу", "недействующий", "архив")


def is_inactive_header(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() in INACTIVE_STATUSES


def compute(header_status: object, cancelled: bool, amended: bool) -> str | None:
    if cancelled or is_inactive_header(header_status):
        return CANCELLED
    if amended:
        return AMENDED
    if isinstance(header_status, str) and header_status.strip():
        return header_status.strip()[:64]
    return None


async def refresh(session: AsyncSession, document_ids: Iterable[int | None]) -> None:
    ids = sorted({i for i in document_ids if i is not None})
    if not ids:
        return
    src = aliased(Document)

    def incoming(kind: str):  # type: ignore[no-untyped-def]
        return (
            exists()
            .where(
                DocumentLink.target_document_id == Document.id,
                DocumentLink.kind == kind,
                src.id == DocumentLink.from_document_id,
                src.deleted_at.is_(None),
                src.is_excluded.is_(False),
            )
            .correlate(Document)
        )

    rows = (
        await session.execute(
            select(
                Document,
                incoming("cancels").label("cancelled"),
                incoming("amends").label("amended"),
            ).where(Document.id.in_(ids))
        )
    ).all()
    for doc, cancelled, amended in rows:
        header = doc.header.get("status") if isinstance(doc.header, dict) else None
        new = compute(header, bool(cancelled), bool(amended))
        if doc.legal_status != new:
            if doc.legal_status == CANCELLED:
                # Документ снова действует: противоречия с ним снова важны, а пока он был
                # отменён, его пропускали (app/contradictions/candidates.py) — проверить заново.
                doc.contra_version = None
            doc.legal_status = new
