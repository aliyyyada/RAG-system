"""Поиск и чат по документам (этап 4).

POST /api/search                 — найти фрагменты (без модели): для отладки и оценки качества.
POST /api/chat/ask               — вопрос → ответ потоком (SSE) со ссылками на источники.
GET  /api/chats                  — история чатов вошедшего пользователя (90 дней).
GET  /api/chats/{id}             — сообщения чата.
PATCH/DELETE /api/chats/{id}     — переименовать / удалить чат.
POST /api/chats/{id}/messages/{mid}/feedback — «Ответ неточный».

Права: поиск и вопросы — search.common (гость и все вошедшие) или documents.view.
Какие документы участвуют, решает только `visible(access)` — права источника.
Гость: история не хранится на сервере (её присылает браузер), частота вопросов
ограничена по IP, в очереди к модели гости стоят после вошедших.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.filters import visible
from app.access.service import resolve_access
from app.auth import permissions as perms
from app.auth.deps import SessionDep, SettingsDep, client_ip, require_permission
from app.auth.rbac import Principal
from app.chat.history import chat_title, recent_turns
from app.chat.llm_queue import PRIORITY_GUEST, PRIORITY_USER
from app.chat.service import AskContext, answer_stream, source_item, unavailable_out
from app.core.sse import SSE_HEADERS, with_heartbeat
from app.models import AuditLog, Chat, ChatMessage, Document
from app.search.hybrid import HL_END, HL_START, SearchFilters
from app.search.service import retrieve

router = APIRouter(prefix="/api", tags=["поиск и чат"])

Asker = Annotated[Principal, Depends(require_permission(perms.SEARCH_COMMON, perms.DOCUMENTS_VIEW))]


def _err(code: int, key: str, message: str) -> HTTPException:
    return HTTPException(status_code=code, detail={"code": key, "message": message})


class FiltersIn(BaseModel):
    source: str | None = Field(default=None, pattern=r"^(manual|\d+)$")
    collection_id: int | None = None
    doc_type: str | None = Field(default=None, max_length=100)
    date_from: date | None = None
    date_to: date | None = None
    only_active: bool = False

    def to_filters(self) -> SearchFilters:
        return SearchFilters(**self.model_dump())


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    filters: FiltersIn = FiltersIn()
    limit: int = Field(default=10, ge=1, le=50)


class HistoryItem(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=20000)
    chat_id: int | None = None
    filters: FiltersIn = FiltersIn()
    # Только для гостя: его история живёт в браузере. Вошедшим история берётся из БД.
    history: list[HistoryItem] = Field(default_factory=list, max_length=40)


def _actor(principal: Principal) -> dict[str, Any]:
    u = principal.user
    return {
        "actor_user_id": u.id if u else None,
        "actor_login": u.personnel_number if u else None,
    }


# ---------------------------------------------------------------------------
# Поиск без модели
# ---------------------------------------------------------------------------


@router.post("/search")
async def search(
    body: SearchIn,
    request: Request,
    principal: Asker,
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    access = await resolve_access(session, principal, settings)
    r = await retrieve(
        session, access, body.query.strip(), body.filters.to_filters(), settings, top_k=body.limit
    )
    items = [
        {
            **source_item(i, c),
            "rrf": round(c.rrf, 5),
            "rerank": c.rerank,
            "vector_rank": c.vector_rank,
            "text_rank": c.text_rank,
        }
        for i, c in enumerate(r.ranked[: body.limit], start=1)
    ]
    session.add(
        AuditLog(
            **_actor(principal),
            action="search.query",
            details={
                "query": body.query[:2000],
                "documents": sorted({i["document_id"] for i in items}),
            },
            ip=client_ip(request),
        )
    )
    await session.commit()
    return {
        "items": items,
        "found": r.found,
        "reranked": r.reranked,
        "vector_ok": r.vector_ok,
        "highlight": {"start": HL_START, "end": HL_END},
        "unavailable_sources": unavailable_out(access),
    }


# ---------------------------------------------------------------------------
# Вопрос → ответ потоком
# ---------------------------------------------------------------------------


async def _guest_rate_limited(session: AsyncSession, ip: str | None, per_minute: int) -> bool:
    if ip is None:
        return False
    since = datetime.now(UTC) - timedelta(minutes=1)
    n = await session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.action == "chat.question",
            AuditLog.actor_user_id.is_(None),
            AuditLog.ip == ip,
            AuditLog.created_at > since,
        )
    )
    return (n or 0) >= per_minute


@router.post("/chat/ask")
async def ask(
    body: AskIn,
    request: Request,
    principal: Asker,
    session: SessionDep,
    settings: SettingsDep,
) -> StreamingResponse:
    question = body.question.strip()
    if not question:
        raise _err(422, "empty_question", "Введите вопрос")
    if len(question) > settings.question_max_chars:
        raise _err(
            422, "question_too_long", f"Вопрос длиннее {settings.question_max_chars} символов"
        )
    ip = client_ip(request)
    user = principal.user
    if user is None and await _guest_rate_limited(session, ip, settings.guest_questions_per_minute):
        raise _err(
            429,
            "rate_limited",
            "Слишком много вопросов подряд. Подождите минуту или войдите в систему.",
        )

    access = await resolve_access(session, principal, settings)
    filters = body.filters.to_filters()
    turns = settings.chat_history_turns
    chat: Chat | None = None
    message_id: int | None = None
    if user is not None:
        if body.chat_id is not None:
            chat = await session.get(Chat, body.chat_id)
            if chat is None or chat.user_id != user.id:
                raise _err(404, "not_found", "Чат не найден")
            history = await recent_turns(session, chat.id, turns)
        else:
            chat = Chat(user_id=user.id, title=chat_title(question))
            session.add(chat)
            await session.flush()
            history = []
        session.add(ChatMessage(chat_id=chat.id, role="user", content=question))
        pending = ChatMessage(chat_id=chat.id, role="assistant", status="pending")
        session.add(pending)
        chat.updated_at = datetime.now(UTC)
        await session.flush()
        message_id = pending.id
    else:
        history = [h.model_dump() for h in body.history][-turns:] if turns else []

    audit = AuditLog(
        **_actor(principal),
        action="chat.question",
        target_type="chat" if chat else None,
        target_id=str(chat.id) if chat else None,
        details={"question": question[:4000], "filters": filters.as_dict()},
        ip=ip,
    )
    session.add(audit)
    await session.commit()

    ctx = AskContext(
        question=question,
        filters=filters,
        access=access,
        queue_priority=PRIORITY_USER if user else PRIORITY_GUEST,
        audit_id=audit.id,
        history=history,
        chat_id=chat.id if chat else None,
        message_id=message_id,
    )
    return StreamingResponse(
        with_heartbeat(answer_stream(ctx, settings), settings.sse_heartbeat_seconds),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


# ---------------------------------------------------------------------------
# История чатов
# ---------------------------------------------------------------------------


class ChatItem(BaseModel):
    id: int
    title: str
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    sources: list[Any]
    status: str
    meta: dict[str, Any]
    feedback: int | None
    created_at: datetime


class ChatOut(ChatItem):
    messages: list[MessageOut]
    history_days: int


class RenameIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class FeedbackIn(BaseModel):
    value: Literal[-1, 0, 1]


async def _own_chat(session: AsyncSession, principal: Principal, chat_id: int) -> Chat:
    user = principal.user
    if user is None:
        raise HTTPException(
            status_code=401, detail={"code": "login_required", "message": "Войдите в систему"}
        )
    chat = await session.get(Chat, chat_id)
    if chat is None or chat.user_id != user.id:
        raise _err(404, "not_found", "Чат не найден")
    return chat


def _require_login(principal: Principal) -> None:
    if principal.user is None:
        raise HTTPException(
            status_code=401, detail={"code": "login_required", "message": "Войдите в систему"}
        )


@router.get("/chats", response_model=list[ChatItem])
async def list_chats(
    principal: Asker,
    session: SessionDep,
    settings: SettingsDep,
    q: str | None = Query(default=None, max_length=200),
) -> list[ChatItem]:
    _require_login(principal)
    assert principal.user is not None
    since = datetime.now(UTC) - timedelta(days=settings.chat_history_days)
    stmt = select(Chat).where(Chat.user_id == principal.user.id, Chat.updated_at >= since)
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            Chat.title.ilike(like)
            | Chat.id.in_(
                select(ChatMessage.chat_id).where(
                    ChatMessage.role == "user", ChatMessage.content.ilike(like)
                )
            )
        )
    rows = await session.scalars(stmt.order_by(Chat.updated_at.desc()).limit(300))
    return [
        ChatItem(id=c.id, title=c.title, created_at=c.created_at, updated_at=c.updated_at)
        for c in rows
    ]


@router.get("/chats/{chat_id}", response_model=ChatOut)
async def get_chat(
    chat_id: int, principal: Asker, session: SessionDep, settings: SettingsDep
) -> ChatOut:
    chat = await _own_chat(session, principal, chat_id)
    msgs = list(
        await session.scalars(
            select(ChatMessage).where(ChatMessage.chat_id == chat.id).order_by(ChatMessage.id)
        )
    )
    # Права могли измениться с момента ответа: источники, которые человеку больше
    # не доступны, показываем без названия и отрывка.
    doc_ids = {s["document_id"] for m in msgs for s in (m.sources or []) if "document_id" in s}
    allowed: set[int] = set()
    if doc_ids:
        access = await resolve_access(session, principal, settings)
        allowed = set(
            await session.scalars(
                select(Document.id).where(Document.id.in_(doc_ids), visible(access))
            )
        )
    out: list[MessageOut] = []
    for m in msgs:
        sources = []
        for s in m.sources or []:
            if s.get("document_id") in allowed:
                sources.append({**s, "available": True})
            else:
                sources.append(
                    {
                        "n": s.get("n"),
                        "cited": s.get("cited", False),
                        "available": False,
                        "caption": "Документ сейчас недоступен",
                    }
                )
        out.append(
            MessageOut(
                id=m.id,
                role=m.role,
                content=m.content,
                sources=sources,
                status=m.status,
                meta=m.meta or {},
                feedback=m.feedback,
                created_at=m.created_at,
            )
        )
    return ChatOut(
        id=chat.id,
        title=chat.title,
        created_at=chat.created_at,
        updated_at=chat.updated_at,
        messages=out,
        history_days=settings.chat_history_days,
    )


@router.patch("/chats/{chat_id}", response_model=ChatItem)
async def rename_chat(
    chat_id: int, body: RenameIn, principal: Asker, session: SessionDep
) -> ChatItem:
    chat = await _own_chat(session, principal, chat_id)
    chat.title = body.title.strip()
    await session.commit()
    return ChatItem(
        id=chat.id, title=chat.title, created_at=chat.created_at, updated_at=chat.updated_at
    )


@router.delete("/chats/{chat_id}", status_code=204)
async def delete_chat(chat_id: int, principal: Asker, session: SessionDep) -> None:
    chat = await _own_chat(session, principal, chat_id)
    await session.execute(delete(Chat).where(Chat.id == chat.id))
    await session.commit()


@router.post("/chats/{chat_id}/messages/{message_id}/feedback", status_code=204)
async def feedback(
    chat_id: int, message_id: int, body: FeedbackIn, principal: Asker, session: SessionDep
) -> None:
    chat = await _own_chat(session, principal, chat_id)
    msg = await session.get(ChatMessage, message_id)
    if msg is None or msg.chat_id != chat.id or msg.role != "assistant":
        raise _err(404, "not_found", "Сообщение не найдено")
    msg.feedback = body.value or None
    await session.commit()
