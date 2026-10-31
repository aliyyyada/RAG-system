"""API для самих источников: «какие мои документы у вас загружены и в какой версии».

Источник обращается с токеном, который администратор выпускает в карточке источника
(«Токен для источника») или командой `python -m app.cli source-token docflow`.
У нас хранится только SHA-256 токена.

    GET  /api/integration/v1/sources/docflow/documents?cursor=0&limit=1000
    POST /api/integration/v1/sources/docflow/documents/lookup   {"ids": ["7c1d…", …]}
    Authorization: Bearer src_docflow_…

Ответ по каждому документу: версия у источника, которую мы видели (`source_version`),
версия, которая проиндексирована и участвует в поиске (`indexed_version`), статус и ошибка.
"""

import hmac
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.auth.deps import SessionDep
from app.ingest.service import hash_token
from app.models import Document, Source

router = APIRouter(prefix="/api/integration/v1", tags=["интеграция: для источников"])
_bearer = HTTPBearer(auto_error=False)


class DocStatus(BaseModel):
    id: str
    source_version: datetime | None
    indexed_version: datetime | None
    status: str
    error: str | None
    deleted: bool


class StatusPage(BaseModel):
    items: list[DocStatus]
    next_cursor: str | None


class Lookup(BaseModel):
    ids: list[str] = Field(max_length=10000)


async def _source(
    code: str,
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Source:
    source = await session.scalar(select(Source).where(Source.code == code))
    ok = (
        source is not None
        and credentials is not None
        and source.callback_token_hash is not None
        and hmac.compare_digest(source.callback_token_hash, hash_token(credentials.credentials))
    )
    if not ok or source is None:
        # Одинаковый ответ на «нет источника» и «неверный токен»: не подсказываем коды.
        raise HTTPException(
            status_code=401,
            detail={"code": "bad_token", "message": "Неверный токен источника"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return source


SourceAuth = Annotated[Source, Depends(_source)]


def _out(d: Document) -> DocStatus:
    return DocStatus(
        id=d.external_id or "",
        source_version=d.version,
        indexed_version=d.indexed_version,
        status="deleted" if d.deleted_at else d.status,
        error=d.error_message if d.status == "error" else None,
        deleted=d.deleted_at is not None,
    )


@router.get("/sources/{code}/documents", response_model=StatusPage)
async def list_status(
    source: SourceAuth,
    session: SessionDep,
    cursor: str | None = None,
    limit: int = Query(default=1000, ge=1, le=10000),
) -> StatusPage:
    after = int(cursor) if cursor and cursor.isdigit() else 0
    rows = (
        await session.scalars(
            select(Document)
            .where(Document.source_id == source.id, Document.id > after)
            .order_by(Document.id)
            .limit(limit + 1)
        )
    ).all()
    more = len(rows) > limit
    rows = rows[:limit]
    return StatusPage(
        items=[_out(d) for d in rows],
        next_cursor=str(rows[-1].id) if more and rows else None,
    )


@router.post("/sources/{code}/documents/lookup", response_model=list[DocStatus])
async def lookup(source: SourceAuth, session: SessionDep, body: Lookup) -> list[DocStatus]:
    rows = (
        await session.scalars(
            select(Document).where(
                Document.source_id == source.id, Document.external_id.in_(body.ids)
            )
        )
    ).all()
    found = {d.external_id: _out(d) for d in rows}
    return [
        found.get(i)
        or DocStatus(
            id=i,
            source_version=None,
            indexed_version=None,
            status="unknown",
            error=None,
            deleted=False,
        )
        for i in body.ids
    ]
