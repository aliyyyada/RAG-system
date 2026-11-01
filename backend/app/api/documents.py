"""Документы для пользователя: список с фильтрами, карточка, текст по фрагментам, файл.

Все запросы фильтруются по правам на уровне SQL (app/access): человек получает только
документы, которые ему разрешил источник, общие документы и открытые ему ручные загрузки.
Гость — только общие. Пример: T-0001 запрашивает /api/documents и видит 5 документов
«Документооборота» из 8; приказ № 45 ему не виден ни в списке, ни по прямой ссылке
/api/documents/2 (403 «Нет доступа к документу»), ни при скачивании файла.

Админка (/api/admin/documents) — отдельный раздел для отладки разбора и права источника
не учитывает (docs/open-questions.md, вопрос 15).
"""

from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.filters import alive, visible
from app.access.service import DocumentAccess, resolve_access
from app.auth import permissions as perms
from app.auth.deps import SessionDep, SettingsDep, require_permission
from app.auth.rbac import Principal
from app.ingest.assemble import FIELD_LABELS, TECHNICAL_FIELDS, HeaderMeta, document_caption
from app.ingest.storage import FileStore
from app.models import Chunk, Document, DocumentFile, Source

router = APIRouter(prefix="/api/documents", tags=["документы"])

Reader = Annotated[
    Principal, Depends(require_permission(perms.SEARCH_COMMON, perms.DOCUMENTS_VIEW))
]

MANUAL = "manual"
MANUAL_NAME = "Ручная загрузка"


def _err(code: int, key: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=code, detail={"code": key, "message": message, **extra})


# ---------------------------------------------------------------------------
# Схемы
# ---------------------------------------------------------------------------


class UnavailableOut(BaseModel):
    id: int
    name: str
    message: str


class DocumentItem(BaseModel):
    id: int
    caption: str
    number: str | None
    title: str
    doc_type: str | None
    doc_date: date | None
    topic: str | None
    source_name: str
    # Статус документа так, как его назвал источник («Действует», «Отменён»).
    # На этапе 5 статус будет вычисляться по связям «отменяет / изменяет».
    legal_status: str | None
    is_common: bool


class DocumentList(BaseModel):
    items: list[DocumentItem]
    total: int
    # Сколько документов в системе всего (без учёта прав) — «Доступно вам: 5 из 8».
    total_in_system: int
    page: int
    page_size: int
    unavailable_sources: list[UnavailableOut]


class Option(BaseModel):
    value: str
    label: str


class Filters(BaseModel):
    sources: list[Option]
    doc_types: list[str]
    unavailable_sources: list[UnavailableOut]


class FieldOut(BaseModel):
    label: str
    value: str


class FileItem(BaseModel):
    id: int
    name: str
    mime: str
    size: int
    pages: int | None


class DocumentView(DocumentItem):
    fields: list[FieldOut]
    description: str | None
    files: list[FileItem]
    parts: list[Any]
    version: datetime | None


class ChunkItem(BaseModel):
    id: int
    ord: int
    kind: str
    text: str
    section: str | None
    point: str | None
    page_from: int | None
    page_to: int | None
    file_id: int | None


class ChunkList(BaseModel):
    items: list[ChunkItem]
    total: int
    page: int
    page_size: int


# ---------------------------------------------------------------------------
# Помощники
# ---------------------------------------------------------------------------


def _unavailable(access: DocumentAccess) -> list[UnavailableOut]:
    return [
        UnavailableOut(
            id=u.id, name=u.name, message=f"Документы из системы «{u.name}» сейчас недоступны"
        )
        for u in access.unavailable
    ]


def _item(doc: Document, source_name: str | None) -> dict[str, Any]:
    status = doc.header.get("status") if isinstance(doc.header, dict) else None
    return {
        "id": doc.id,
        "caption": document_caption(
            HeaderMeta(doc.number, doc.title, doc.doc_type, doc.topic, doc.doc_date)
        ),
        "number": doc.number,
        "title": doc.title,
        "doc_type": doc.doc_type,
        "doc_date": doc.doc_date,
        "topic": doc.topic,
        "source_name": source_name or MANUAL_NAME,
        "legal_status": str(status) if status else None,
        "is_common": doc.is_common,
    }


def _fields(doc: Document, source_name: str) -> list[FieldOut]:
    """Реквизиты для правой колонки просмотра: известные поля по-русски, затем прочие."""
    out = [
        FieldOut(label=label, value=value)
        for label, value in (
            ("Тип", doc.doc_type),
            ("Номер", doc.number),
            ("Дата", doc.doc_date.strftime("%d.%m.%Y") if doc.doc_date else None),
            ("Тема", doc.topic),
        )
        if value
    ]
    skip = TECHNICAL_FIELDS | {"doc_type", "number", "date", "title", "topic", "description"}
    for key, value in (doc.header or {}).items():
        if key in skip or value in (None, "") or isinstance(value, dict | list):
            continue
        out.append(FieldOut(label=FIELD_LABELS.get(key, key), value=str(value)))
    out.append(FieldOut(label="Источник", value=source_name))
    return out


async def _visible_document(
    session: AsyncSession, access: DocumentAccess, document_id: int, principal: Principal
) -> tuple[Document, str]:
    """Документ, если он виден человеку; иначе понятная ошибка.

    404 — документа нет (или он удалён / исключён из поиска);
    503 source_unavailable — источник не ответил о правах, проверить доступ нельзя;
    403 login_required — гостю документ не общий;
    403 no_access — источник не выдал права на документ.
    """
    row = (
        await session.execute(
            select(Document, Source.name)
            .outerjoin(Source, Source.id == Document.source_id)
            .where(Document.id == document_id, alive())
        )
    ).first()
    if row is None:
        raise _err(404, "not_found", "Документ не найден")
    doc, source_name = row
    name = source_name or MANUAL_NAME
    ok = await session.scalar(
        select(func.count()).select_from(Document).where(Document.id == doc.id, visible(access))
    )
    if ok:
        return doc, name
    if any(u.id == doc.source_id for u in access.unavailable):
        raise _err(
            503,
            "source_unavailable",
            f"Документы из системы «{name}» сейчас недоступны",
            source_name=name,
        )
    if principal.is_guest:
        raise _err(403, "login_required", "Документ доступен только после входа")
    raise _err(
        403,
        "no_access",
        f"Источник «{name}» не выдал вам права на этот документ",
        source_name=name,
    )


# ---------------------------------------------------------------------------
# Эндпоинты
# ---------------------------------------------------------------------------


@router.get("", response_model=DocumentList)
async def list_documents(
    principal: Reader,
    session: SessionDep,
    settings: SettingsDep,
    q: str | None = Query(default=None, max_length=200),
    source: str | None = Query(default=None, pattern=r"^(manual|\d+)$"),
    doc_type: str | None = Query(default=None, max_length=100),
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=200),
) -> DocumentList:
    access = await resolve_access(session, principal, settings)
    cond = [visible(access)]
    if q and q.strip():
        like = f"%{q.strip()}%"
        cond.append(or_(Document.title.ilike(like), Document.number.ilike(like)))
    if source == MANUAL:
        cond.append(Document.source_id.is_(None))
    elif source is not None:
        cond.append(Document.source_id == int(source))
    if doc_type:
        cond.append(Document.doc_type == doc_type)
    if date_from:
        cond.append(Document.doc_date >= date_from)
    if date_to:
        cond.append(Document.doc_date <= date_to)

    total = await session.scalar(select(func.count()).select_from(Document).where(*cond))
    in_system = await session.scalar(select(func.count()).select_from(Document).where(alive()))
    rows = await session.execute(
        select(Document, Source.name)
        .outerjoin(Source, Source.id == Document.source_id)
        .where(*cond)
        .order_by(Document.doc_date.desc().nulls_last(), Document.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return DocumentList(
        items=[DocumentItem(**_item(d, n)) for d, n in rows.all()],
        total=total or 0,
        total_in_system=in_system or 0,
        page=page,
        page_size=page_size,
        unavailable_sources=_unavailable(access),
    )


@router.get("/filters", response_model=Filters)
async def filters(principal: Reader, session: SessionDep, settings: SettingsDep) -> Filters:
    """Значения для фильтров — только из доступных человеку документов."""
    access = await resolve_access(session, principal, settings)
    cond = visible(access)
    src_rows = await session.execute(
        select(Document.source_id, Source.name)
        .outerjoin(Source, Source.id == Document.source_id)
        .where(cond)
        .group_by(Document.source_id, Source.name)
        .order_by(Source.name.nulls_last())
    )
    sources = [
        Option(value=str(sid) if sid is not None else MANUAL, label=name or MANUAL_NAME)
        for sid, name in src_rows.all()
    ]
    types = await session.scalars(
        select(Document.doc_type)
        .where(cond, Document.doc_type.is_not(None))
        .group_by(Document.doc_type)
        .order_by(Document.doc_type)
    )
    return Filters(
        sources=sources,
        doc_types=[t for t in types if t],
        unavailable_sources=_unavailable(access),
    )


@router.get("/{document_id}", response_model=DocumentView)
async def get_document(
    document_id: int, principal: Reader, session: SessionDep, settings: SettingsDep
) -> DocumentView:
    access = await resolve_access(session, principal, settings)
    doc, source_name = await _visible_document(session, access, document_id, principal)
    files = await session.scalars(
        select(DocumentFile).where(DocumentFile.document_id == doc.id).order_by(DocumentFile.ord)
    )
    description = (doc.header or {}).get("description")
    return DocumentView(
        **_item(doc, source_name),
        fields=_fields(doc, source_name),
        description=str(description) if description else None,
        files=[
            FileItem(id=f.id, name=f.name, mime=f.mime, size=f.size, pages=f.pages) for f in files
        ],
        parts=doc.parts_summary,
        version=doc.indexed_version,
    )


@router.get("/{document_id}/chunks", response_model=ChunkList)
async def document_chunks(
    document_id: int,
    principal: Reader,
    session: SessionDep,
    settings: SettingsDep,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=200, ge=1, le=500),
) -> ChunkList:
    """Текст документа по фрагментам — для просмотра с подсветкой найденного места."""
    access = await resolve_access(session, principal, settings)
    doc, _ = await _visible_document(session, access, document_id, principal)
    total = await session.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.document_id == doc.id)
    )
    rows = await session.scalars(
        select(Chunk)
        .where(Chunk.document_id == doc.id)
        .order_by(Chunk.ord)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return ChunkList(
        items=[
            ChunkItem(
                id=c.id,
                ord=c.ord,
                kind=c.kind,
                text=c.text,
                section=c.section,
                point=c.point,
                page_from=c.page_from,
                page_to=c.page_to,
                file_id=c.file_id,
            )
            for c in rows
        ],
        total=total or 0,
        page=page,
        page_size=page_size,
    )


@router.get("/{document_id}/files/{file_id}", response_class=FileResponse)
async def download_file(
    document_id: int,
    file_id: int,
    principal: Reader,
    session: SessionDep,
    settings: SettingsDep,
) -> FileResponse:
    access = await resolve_access(session, principal, settings)
    doc, _ = await _visible_document(session, access, document_id, principal)
    f = await session.get(DocumentFile, file_id)
    if f is None or f.document_id != doc.id:
        raise _err(404, "not_found", "Файл не найден")
    path = FileStore(settings).path_for(f.sha256)
    if not path.exists():
        raise _err(
            410, "file_missing", "Файл документа не найден на диске, обратитесь к администратору"
        )
    return FileResponse(
        path,
        media_type=f.mime,
        filename=f.name,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )
