"""Фильтр документов по правам — на уровне SQL, до поиска.

    access = await resolve_access(session, principal, settings)
    rows = await session.scalars(select(Document).where(visible(access), ...))

Документ виден, если он «живой» (не удалён, не исключён, хотя бы раз обработан) и:
- он общий, а человеку доступны общие документы; или
- он из источника, который ответил о правах, и его guid есть в списке этого человека; или
- это ручная загрузка, и администратор открыл её этому человеку или его группе.

Чанки (поиск на этапе 4) фильтруются так же: `Chunk.document_id IN (SELECT id ... visible)`.
"""

from sqlalchemy import ColumnElement, and_, exists, false, or_, select

from app.access.service import DocumentAccess
from app.models import AccessCacheId, Document, DocumentGrant, group_members


def alive() -> ColumnElement[bool]:
    """Документ участвует в поиске и просмотре (без учёта прав)."""
    return and_(
        Document.deleted_at.is_(None),
        Document.is_excluded.is_(False),
        Document.indexed_version.is_not(None),
    )


def visible(access: DocumentAccess) -> ColumnElement[bool]:
    alternatives: list[ColumnElement[bool]] = []
    if access.common:
        alternatives.append(Document.is_common.is_(True))
    uid = access.user_id
    if uid is not None:
        if access.sources:
            alternatives.append(
                and_(
                    Document.source_id.in_(sorted(access.sources)),
                    exists().where(
                        AccessCacheId.user_id == uid,
                        AccessCacheId.source_id == Document.source_id,
                        AccessCacheId.external_id == Document.external_id,
                    ),
                )
            )
        my_groups = select(group_members.c.group_id).where(group_members.c.user_id == uid)
        alternatives.append(
            and_(
                Document.source_id.is_(None),
                exists().where(
                    DocumentGrant.document_id == Document.id,
                    or_(DocumentGrant.user_id == uid, DocumentGrant.group_id.in_(my_groups)),
                ),
            )
        )
    if not alternatives:
        return false()
    return and_(alive(), or_(*alternatives))
