"""Админка: источники, отчёт о полноте загрузки, коллекции и карточка документа.

Всё под правом admin.integrations («Источники и синхронизация»). Пример сценария:
админ открывает «Источники → Документооборот», видит 9 документов с ошибкой, жмёт
«Повторить все» (POST /api/admin/sources/1/retry-failed) — документы встают в очередь,
worker обрабатывает их заново.

Допущение (docs/open-questions.md, вопрос 15): тот, у кого есть admin.integrations, видит
текст фрагментов любого документа — без этого не отладить разбор. Доступ к документам по
правам источника (этап 3) на админку не распространяется.

Этап 3: кнопка «Сбросить кэш прав» (POST /api/admin/access-cache/reset) и доступ к ручным
загрузкам по людям и группам (GET/PUT /api/admin/documents/{id}/access).
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.service import reset_cache
from app.auth import permissions as perms
from app.auth.deps import SessionDep, SettingsDep, client_ip, require_permission
from app.auth.rbac import Principal
from app.auth.service import write_audit
from app.ingest import service
from app.ingest.assemble import HeaderMeta, document_caption
from app.ingest.errors import STAGE_TITLES
from app.ingest.report import source_report
from app.models import (
    Chunk,
    Collection,
    Document,
    DocumentEvent,
    DocumentFile,
    DocumentGrant,
    Group,
    Source,
    User,
)
from app.sources import ADAPTERS

router = APIRouter(prefix="/api/admin", tags=["admin: источники и документы"])

IntegrationsAdmin = Annotated[Principal, Depends(require_permission(perms.ADMIN_INTEGRATIONS))]


def _err(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _actor(p: Principal) -> str | None:
    return p.user.personnel_number if p.user else None


# ---------------------------------------------------------------------------
# Схемы
# ---------------------------------------------------------------------------


class SourceSettings(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=8, max_length=500)
    enabled: bool = True
    sync_interval_minutes: int = Field(default=15, ge=1, le=24 * 60)
    overlap_minutes: int = Field(default=60, ge=0, le=7 * 24 * 60)
    reconcile_hour: int = Field(default=2, ge=0, le=23)

    @field_validator("base_url")
    @classmethod
    def _https(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("https://"):
            raise ValueError("Адрес источника должен начинаться с https://")
        return v


class SourceCreate(SourceSettings):
    code: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,31}$")
    adapter: Literal["http_json"] = "http_json"


class SourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    base_url: str | None = Field(default=None, min_length=8, max_length=500)
    enabled: bool | None = None
    sync_interval_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    overlap_minutes: int | None = Field(default=None, ge=0, le=7 * 24 * 60)
    reconcile_hour: int | None = Field(default=None, ge=0, le=23)

    @field_validator("base_url")
    @classmethod
    def _https(cls, v: str | None) -> str | None:
        if v is not None and not v.strip().startswith("https://"):
            raise ValueError("Адрес источника должен начинаться с https://")
        return v.strip() if v else v


class ReportOut(BaseModel):
    at_source: int | None
    known: int
    ready: int
    queued: int
    processing: int
    error: int
    deleted: int
    excluded: int
    rate_per_minute: float
    eta_minutes: int | None
    progress: float
    errors_by_stage: dict[str, int]
    runs: list[dict[str, Any]]


class SourceOut(BaseModel):
    id: int
    code: str
    name: str
    adapter: str
    base_url: str
    enabled: bool
    sync_interval_minutes: int
    overlap_minutes: int
    reconcile_hour: int
    available: bool | None
    last_error: str | None
    last_sync_at: datetime | None
    last_reconcile_at: datetime | None
    last_checked_at: datetime | None
    created_at: datetime
    has_callback_token: bool
    collection_id: int | None
    report: ReportOut


class DocumentRow(BaseModel):
    id: int
    caption: str
    number: str | None
    title: str
    doc_type: str | None
    doc_date: Any
    source_id: int | None
    source_name: str | None
    collection_id: int
    external_id: str | None
    version: datetime | None
    indexed_version: datetime | None
    status: str
    error_stage: str | None
    error_stage_title: str | None
    error_message: str | None
    attempts: int
    is_common: bool
    is_excluded: bool
    deleted_at: datetime | None
    deleted_reason: str | None
    chunk_count: int
    status_changed_at: datetime


class DocumentPage(BaseModel):
    items: list[DocumentRow]
    total: int
    page: int
    page_size: int


class FileOut(BaseModel):
    id: int
    name: str
    mime: str
    size: int
    sha256: str
    pages: int | None
    ocr_pages: int
    warnings: list[Any]


class EventOut(BaseModel):
    id: int
    created_at: datetime
    event: str
    version: datetime | None
    details: dict[str, Any]


class DocumentCard(DocumentRow):
    topic: str | None
    header: dict[str, Any]
    parts_summary: list[Any]
    files: list[FileOut]
    events: list[EventOut]
    collection_name: str
    manual: bool


class ChunkOut(BaseModel):
    id: int
    ord: int
    kind: str
    text: str
    section: str | None
    point: str | None
    page_from: int | None
    page_to: int | None
    token_estimate: int
    file_id: int | None


class ChunkPage(BaseModel):
    items: list[ChunkOut]
    total: int
    page: int
    page_size: int


class CollectionOut(BaseModel):
    id: int
    name: str
    kind: str
    source_id: int | None
    documents: int
    errors: int


class SyncRequest(BaseModel):
    reconcile: bool = False


class ExcludeRequest(BaseModel):
    excluded: bool


class DocumentUpdate(BaseModel):
    is_common: bool | None = None
    title: str | None = Field(default=None, min_length=1, max_length=1000)


# ---------------------------------------------------------------------------
# Источники
# ---------------------------------------------------------------------------


async def _get_source(session: AsyncSession, source_id: int) -> Source:
    source = await session.get(Source, source_id)
    if source is None:
        raise _err(404, "not_found", "Источник не найден")
    return source


async def _source_out(session: AsyncSession, source: Source) -> SourceOut:
    rep = await source_report(session, source)
    coll_id = await session.scalar(select(Collection.id).where(Collection.source_id == source.id))
    return SourceOut(
        id=source.id,
        code=source.code,
        name=source.name,
        adapter=source.adapter,
        base_url=source.base_url,
        enabled=source.enabled,
        sync_interval_minutes=source.sync_interval_minutes,
        overlap_minutes=source.overlap_minutes,
        reconcile_hour=source.reconcile_hour,
        available=source.available,
        last_error=source.last_error,
        last_sync_at=source.last_sync_at,
        last_reconcile_at=source.last_reconcile_at,
        last_checked_at=source.last_checked_at,
        created_at=source.created_at,
        has_callback_token=source.callback_token_hash is not None,
        collection_id=coll_id,
        report=ReportOut(**rep.__dict__),
    )


@router.get("/sources", response_model=list[SourceOut])
async def list_sources(_: IntegrationsAdmin, session: SessionDep) -> list[SourceOut]:
    sources = (await session.scalars(select(Source).order_by(Source.id))).all()
    return [await _source_out(session, s) for s in sources]


@router.post("/sources", response_model=SourceOut, status_code=status.HTTP_201_CREATED)
async def create_source(
    body: SourceCreate, principal: IntegrationsAdmin, session: SessionDep, request: Request
) -> SourceOut:
    if body.adapter not in ADAPTERS:
        raise _err(422, "bad_adapter", "Неизвестный тип источника")
    try:
        source = await service.create_source(
            session,
            code=body.code,
            name=body.name,
            base_url=body.base_url,
            adapter=body.adapter,
            sync_interval_minutes=body.sync_interval_minutes,
            reconcile_hour=body.reconcile_hour,
        )
    except service.ServiceError as exc:
        raise _err(409, exc.code, exc.message) from None
    source.enabled = body.enabled
    source.overlap_minutes = body.overlap_minutes
    await write_audit(
        session,
        "source.create",
        actor=principal.user,
        target_type="source",
        target_id=source.id,
        details={"code": body.code, "name": body.name, "base_url": body.base_url},
        ip=client_ip(request),
    )
    await session.commit()
    return await _source_out(session, source)


@router.get("/sources/{source_id}", response_model=SourceOut)
async def get_source(source_id: int, _: IntegrationsAdmin, session: SessionDep) -> SourceOut:
    return await _source_out(session, await _get_source(session, source_id))


@router.patch("/sources/{source_id}", response_model=SourceOut)
async def update_source(
    source_id: int,
    body: SourceUpdate,
    principal: IntegrationsAdmin,
    session: SessionDep,
    request: Request,
) -> SourceOut:
    source = await _get_source(session, source_id)
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if "name" in changes and changes["name"] != source.name:
        clash = await session.scalar(
            select(Collection).where(
                Collection.name == changes["name"], Collection.source_id != source.id
            )
        )
        if clash:
            raise _err(409, "name_taken", f"Коллекция «{changes['name']}» уже есть")
        coll = await session.scalar(select(Collection).where(Collection.source_id == source.id))
        if coll:
            coll.name = changes["name"]
    for key, value in changes.items():
        setattr(source, key, value)
    await write_audit(
        session,
        "source.update",
        actor=principal.user,
        target_type="source",
        target_id=source.id,
        details=changes,
        ip=client_ip(request),
    )
    await session.commit()
    return await _source_out(session, source)


@router.post("/sources/{source_id}/sync")
async def sync_now(
    source_id: int,
    body: SyncRequest,
    principal: IntegrationsAdmin,
    session: SessionDep,
    request: Request,
) -> dict[str, bool]:
    source = await _get_source(session, source_id)
    if not source.enabled:
        raise _err(409, "disabled", "Источник выключен в настройках")
    queued = await service.request_sync(session, source, reconcile=body.reconcile)
    await write_audit(
        session,
        "source.reconcile" if body.reconcile else "source.sync",
        actor=principal.user,
        target_type="source",
        target_id=source.id,
        ip=client_ip(request),
    )
    await session.commit()
    return {"queued": queued}


@router.post("/sources/{source_id}/retry-failed")
async def retry_failed(
    source_id: int,
    principal: IntegrationsAdmin,
    session: SessionDep,
    settings: SettingsDep,
    request: Request,
) -> dict[str, int]:
    source = await _get_source(session, source_id)
    n = await service.retry_failed(session, source.id, settings, actor=_actor(principal))
    await write_audit(
        session,
        "source.retry_failed",
        actor=principal.user,
        target_type="source",
        target_id=source.id,
        details={"documents": n},
        ip=client_ip(request),
    )
    await session.commit()
    return {"count": n}


@router.post("/sources/{source_id}/callback-token")
async def new_callback_token(
    source_id: int, principal: IntegrationsAdmin, session: SessionDep, request: Request
) -> dict[str, str]:
    """Токен для API «что у вас загружено». Показывается один раз, у нас хранится только хэш."""
    source = await _get_source(session, source_id)
    token = await service.issue_callback_token(session, source)
    await write_audit(
        session,
        "source.callback_token",
        actor=principal.user,
        target_type="source",
        target_id=source.id,
        ip=client_ip(request),
    )
    await session.commit()
    return {"token": token}


# ---------------------------------------------------------------------------
# Коллекции и документы
# ---------------------------------------------------------------------------


@router.get("/collections", response_model=list[CollectionOut])
async def list_collections(_: IntegrationsAdmin, session: SessionDep) -> list[CollectionOut]:
    alive = Document.deleted_at.is_(None)
    docs = (
        select(
            Document.collection_id,
            func.count().label("n"),
            func.count().filter(Document.status == "error").label("errors"),
        )
        .where(alive)
        .group_by(Document.collection_id)
        .subquery()
    )
    rows = await session.execute(
        select(Collection, docs.c.n, docs.c.errors)
        .outerjoin(docs, docs.c.collection_id == Collection.id)
        .order_by(Collection.kind.desc(), Collection.name)
    )
    return [
        CollectionOut(
            id=c.id,
            name=c.name,
            kind=c.kind,
            source_id=c.source_id,
            documents=n or 0,
            errors=e or 0,
        )
        for c, n, e in rows.all()
    ]


def _row(doc: Document, source_name: str | None) -> dict[str, Any]:
    caption = document_caption(
        HeaderMeta(doc.number, doc.title, doc.doc_type, doc.topic, doc.doc_date)
    )
    if not doc.title and not doc.number:
        caption = f"Документ {doc.external_id or doc.id} (ещё не получен)"
    return {
        "id": doc.id,
        "caption": caption,
        "number": doc.number,
        "title": doc.title,
        "doc_type": doc.doc_type,
        "doc_date": doc.doc_date,
        "source_id": doc.source_id,
        "source_name": source_name,
        "collection_id": doc.collection_id,
        "external_id": doc.external_id,
        "version": doc.version,
        "indexed_version": doc.indexed_version,
        "status": doc.status,
        "error_stage": doc.error_stage,
        "error_stage_title": STAGE_TITLES.get(doc.error_stage or ""),
        "error_message": doc.error_message,
        "attempts": doc.attempts,
        "is_common": doc.is_common,
        "is_excluded": doc.is_excluded,
        "deleted_at": doc.deleted_at,
        "deleted_reason": doc.deleted_reason,
        "chunk_count": doc.chunk_count,
        "status_changed_at": doc.status_changed_at,
    }


@router.get("/documents", response_model=DocumentPage)
async def list_documents(
    _: IntegrationsAdmin,
    session: SessionDep,
    source_id: int | None = None,
    collection_id: int | None = None,
    status_: Annotated[
        Literal["queued", "processing", "ready", "error", "deleted", "excluded"] | None,
        Query(alias="status"),
    ] = None,
    q: str | None = Query(default=None, max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> DocumentPage:
    cond = []
    if source_id is not None:
        cond.append(Document.source_id == source_id)
    if collection_id is not None:
        cond.append(Document.collection_id == collection_id)
    if status_ == "deleted":
        cond.append(Document.deleted_at.is_not(None))
    else:
        cond.append(Document.deleted_at.is_(None))
        if status_ == "excluded":
            cond.append(Document.is_excluded.is_(True))
        elif status_ is not None:
            cond.append(Document.status == status_)
    if q and q.strip():
        like = f"%{q.strip()}%"
        cond.append(
            or_(
                Document.title.ilike(like),
                Document.number.ilike(like),
                Document.external_id.ilike(like),
                Document.error_message.ilike(like),
            )
        )
    total = await session.scalar(select(func.count()).select_from(Document).where(*cond))
    rows = await session.execute(
        select(Document, Source.name)
        .outerjoin(Source, Source.id == Document.source_id)
        .where(*cond)
        .order_by(Document.status_changed_at.desc(), Document.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return DocumentPage(
        items=[DocumentRow(**_row(d, n)) for d, n in rows.all()],
        total=total or 0,
        page=page,
        page_size=page_size,
    )


async def _get_document(session: AsyncSession, document_id: int) -> Document:
    doc = await session.get(Document, document_id)
    if doc is None:
        raise _err(404, "not_found", "Документ не найден")
    return doc


@router.get("/documents/{document_id}", response_model=DocumentCard)
async def get_document(document_id: int, _: IntegrationsAdmin, session: SessionDep) -> DocumentCard:
    doc = await _get_document(session, document_id)
    source = await session.get(Source, doc.source_id) if doc.source_id else None
    coll = await session.get(Collection, doc.collection_id)
    files = (
        await session.scalars(
            select(DocumentFile)
            .where(DocumentFile.document_id == doc.id)
            .order_by(DocumentFile.ord)
        )
    ).all()
    events = (
        await session.scalars(
            select(DocumentEvent)
            .where(DocumentEvent.document_id == doc.id)
            .order_by(DocumentEvent.id.desc())
            .limit(100)
        )
    ).all()
    return DocumentCard(
        **_row(doc, source.name if source else None),
        topic=doc.topic,
        header=doc.header,
        parts_summary=doc.parts_summary,
        files=[
            FileOut(
                id=f.id,
                name=f.name,
                mime=f.mime,
                size=f.size,
                sha256=f.sha256,
                pages=f.pages,
                ocr_pages=f.ocr_pages,
                warnings=f.warnings,
            )
            for f in files
        ],
        events=[
            EventOut(
                id=e.id,
                created_at=e.created_at,
                event=e.event,
                version=e.version,
                details=e.details,
            )
            for e in events
        ],
        collection_name=coll.name if coll else "",
        manual=doc.source_id is None,
    )


@router.get("/documents/{document_id}/chunks", response_model=ChunkPage)
async def document_chunks(
    document_id: int,
    _: IntegrationsAdmin,
    session: SessionDep,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> ChunkPage:
    await _get_document(session, document_id)
    total = await session.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.document_id == document_id)
    )
    rows = await session.scalars(
        select(Chunk)
        .where(Chunk.document_id == document_id)
        .order_by(Chunk.ord)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return ChunkPage(
        items=[
            ChunkOut(
                id=c.id,
                ord=c.ord,
                kind=c.kind,
                text=c.text,
                section=c.section,
                point=c.point,
                page_from=c.page_from,
                page_to=c.page_to,
                token_estimate=c.token_estimate,
                file_id=c.file_id,
            )
            for c in rows
        ],
        total=total or 0,
        page=page,
        page_size=page_size,
    )


@router.get("/documents/{document_id}/files/{file_id}/text")
async def file_text(
    document_id: int, file_id: int, _: IntegrationsAdmin, session: SessionDep
) -> dict[str, Any]:
    f = await session.get(DocumentFile, file_id)
    if f is None or f.document_id != document_id:
        raise _err(404, "not_found", "Файл не найден")
    return {"id": f.id, "name": f.name, "text": f.text, "warnings": f.warnings}


async def _doc_action(
    session: AsyncSession,
    principal: Principal,
    request: Request,
    doc: Document,
    action: str,
    **details: Any,
) -> None:
    await write_audit(
        session,
        action,
        actor=principal.user,
        target_type="document",
        target_id=doc.id,
        details=details,
        ip=client_ip(request),
    )


@router.post("/documents/{document_id}/retry", response_model=DocumentRow)
async def retry_document(
    document_id: int,
    principal: IntegrationsAdmin,
    session: SessionDep,
    settings: SettingsDep,
    request: Request,
) -> DocumentRow:
    return await _requeue(document_id, principal, session, settings, request, force=False)


@router.post("/documents/{document_id}/reindex", response_model=DocumentRow)
async def reindex_document(
    document_id: int,
    principal: IntegrationsAdmin,
    session: SessionDep,
    settings: SettingsDep,
    request: Request,
) -> DocumentRow:
    return await _requeue(document_id, principal, session, settings, request, force=True)


async def _requeue(
    document_id: int,
    principal: Principal,
    session: AsyncSession,
    settings: Any,
    request: Request,
    *,
    force: bool,
) -> DocumentRow:
    doc = await _get_document(session, document_id)
    try:
        await service.retry_document(session, doc, settings, actor=_actor(principal), force=force)
    except service.ServiceError as exc:
        raise _err(409, exc.code, exc.message) from None
    await _doc_action(
        session, principal, request, doc, "document.reindex" if force else "document.retry"
    )
    await session.commit()
    source = await session.get(Source, doc.source_id) if doc.source_id else None
    return DocumentRow(**_row(doc, source.name if source else None))


@router.post("/documents/{document_id}/exclude", response_model=DocumentRow)
async def exclude_document(
    document_id: int,
    body: ExcludeRequest,
    principal: IntegrationsAdmin,
    session: SessionDep,
    request: Request,
) -> DocumentRow:
    doc = await _get_document(session, document_id)
    await service.set_excluded(session, doc, body.excluded, actor=_actor(principal))
    await _doc_action(session, principal, request, doc, "document.exclude", excluded=body.excluded)
    await session.commit()
    source = await session.get(Source, doc.source_id) if doc.source_id else None
    return DocumentRow(**_row(doc, source.name if source else None))


@router.patch("/documents/{document_id}", response_model=DocumentRow)
async def update_document(
    document_id: int,
    body: DocumentUpdate,
    principal: IntegrationsAdmin,
    session: SessionDep,
    request: Request,
) -> DocumentRow:
    """Признак «общий» и название меняются только у ручных загрузок: у документов
    источника их задаёт источник, и следующая синхронизация вернула бы своё значение."""
    doc = await _get_document(session, document_id)
    changes = body.model_dump(exclude_none=True)
    if doc.source_id is not None and changes:
        raise _err(409, "from_source", "У документа источника это поле задаёт сам источник")
    for key, value in changes.items():
        setattr(doc, key, value)
    await _doc_action(session, principal, request, doc, "document.update", **changes)
    await session.commit()
    return DocumentRow(**_row(doc, None))


# ---------------------------------------------------------------------------
# Этап 3: кэш прав и доступ к ручным загрузкам
# ---------------------------------------------------------------------------


class CacheResetRequest(BaseModel):
    # Пусто — сбросить кэш прав всех пользователей.
    user_id: int | None = None


@router.post("/access-cache/reset")
async def reset_access_cache(
    body: CacheResetRequest,
    principal: IntegrationsAdmin,
    session: SessionDep,
    request: Request,
) -> dict[str, int]:
    """Забыть ответы источников о правах: при следующем запросе спросим их заново.

    Нужна, когда в источнике поменяли права и человек должен увидеть изменения сразу,
    не дожидаясь, пока истечёт кэш (ACCESS_CACHE_MINUTES).
    """
    count = await reset_cache(session, body.user_id)
    await write_audit(
        session,
        "access.cache_reset",
        actor=principal.user,
        target_type="user" if body.user_id else None,
        target_id=body.user_id,
        details={"entries": count},
        ip=client_ip(request),
    )
    await session.commit()
    return {"reset": count}


class GrantUser(BaseModel):
    id: int
    personnel_number: str
    full_name: str


class GrantGroup(BaseModel):
    id: int
    name: str


class DocumentAccessOut(BaseModel):
    is_common: bool
    users: list[GrantUser]
    groups: list[GrantGroup]
    # Все группы — для выбора в интерфейсе (у admin.integrations может не быть admin.users).
    all_groups: list[GrantGroup]


class DocumentAccessIn(BaseModel):
    personnel_numbers: list[str] = Field(default_factory=list, max_length=1000)
    group_ids: list[int] = Field(default_factory=list, max_length=1000)


async def _manual_document(session: AsyncSession, document_id: int) -> Document:
    doc = await _get_document(session, document_id)
    if doc.source_id is not None:
        raise _err(
            409,
            "from_source",
            "Права на документы источника задаёт сам источник; здесь — только ручные загрузки",
        )
    return doc


async def _access_out(session: AsyncSession, doc: Document) -> DocumentAccessOut:
    users = await session.execute(
        select(User.id, User.personnel_number, User.full_name)
        .join(DocumentGrant, DocumentGrant.user_id == User.id)
        .where(DocumentGrant.document_id == doc.id)
        .order_by(User.full_name, User.personnel_number)
    )
    granted_groups = set(
        await session.scalars(
            select(DocumentGrant.group_id).where(
                DocumentGrant.document_id == doc.id, DocumentGrant.group_id.is_not(None)
            )
        )
    )
    groups = (await session.execute(select(Group.id, Group.name).order_by(Group.name))).all()
    return DocumentAccessOut(
        is_common=doc.is_common,
        users=[GrantUser(id=i, personnel_number=pn, full_name=fn) for i, pn, fn in users.all()],
        groups=[GrantGroup(id=i, name=n) for i, n in groups if i in granted_groups],
        all_groups=[GrantGroup(id=i, name=n) for i, n in groups],
    )


@router.get("/documents/{document_id}/access", response_model=DocumentAccessOut)
async def get_document_access(
    document_id: int, _: IntegrationsAdmin, session: SessionDep
) -> DocumentAccessOut:
    doc = await _manual_document(session, document_id)
    return await _access_out(session, doc)


@router.put("/documents/{document_id}/access", response_model=DocumentAccessOut)
async def set_document_access(
    document_id: int,
    body: DocumentAccessIn,
    principal: IntegrationsAdmin,
    session: SessionDep,
    request: Request,
) -> DocumentAccessOut:
    """Кому открыта ручная загрузка. Список заменяется целиком.

    Человека, который ещё ни разу не входил, заводим заранее по табельному номеру (как
    `create-admin`): когда он войдёт через внешний сервис, доступ уже будет на месте.
    """
    doc = await _manual_document(session, document_id)
    numbers = sorted({n.strip() for n in body.personnel_numbers if n.strip()})
    if any(len(n) > 64 for n in numbers):
        raise _err(422, "bad_personnel_number", "Слишком длинный табельный номер")
    group_ids = sorted(set(body.group_ids))
    known_groups = set(await session.scalars(select(Group.id).where(Group.id.in_(group_ids))))
    if missing := [g for g in group_ids if g not in known_groups]:
        raise _err(422, "unknown_group", f"Нет групп с id {missing}")
    users = {
        u.personnel_number: u
        for u in await session.scalars(select(User).where(User.personnel_number.in_(numbers)))
    }
    for n in numbers:
        if n not in users:
            users[n] = User(personnel_number=n, account_type="external", roles=[], groups=[])
            session.add(users[n])
    await session.flush()
    await session.execute(delete(DocumentGrant).where(DocumentGrant.document_id == doc.id))
    for u in users.values():
        session.add(DocumentGrant(document_id=doc.id, user_id=u.id))
    for g in group_ids:
        session.add(DocumentGrant(document_id=doc.id, group_id=g))
    await _doc_action(
        session,
        principal,
        request,
        doc,
        "document.access",
        personnel_numbers=numbers,
        group_ids=group_ids,
    )
    await session.commit()
    return await _access_out(session, doc)
