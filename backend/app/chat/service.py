"""Ответ на вопрос по документам потоком (SSE).

Что видит пользователь и какие события уходят в браузер:

    event: meta     {"chat_id": 7, "message_id": 31, "unavailable_sources": [...]}
    event: status   {"stage": "search"}                       «Ищу в документах»
    event: status   {"stage": "rerank", "found": 14}          «Найдено 14 фрагментов»
    event: sources  {"items": [{"n": 1, "document_id": 3, "chunk_id": 41, ...}]}
    event: status   {"stage": "queue", "position": 2}         «вы 2-й в очереди»
    event: status   {"stage": "generate", "selected": 5}      «Отобрано 5 лучших»
    event: token    {"text": "Для допуска"} … (по кусочку)
    event: done     {"answer": "...", "cited": [1, 3], "elapsed_ms": 9120, ...}
    : ping          ← каждые 2 с, пока ничего не происходит (Windows рвёт тихие соединения)

Если подходящих фрагментов нет, модель не вызывается: сразу «в доступных вам документах
ответа нет». Модель видит только фрагменты, разрешённые человеку (фильтр в SQL).
"""

import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import anyio
from sqlalchemy import update

from app.access.service import DocumentAccess
from app.chat.llm_queue import LlmQueue
from app.chat.prompt import (
    NO_ANSWER,
    build_context,
    build_messages,
    cited_numbers,
    is_refusal,
)
from app.config import Settings
from app.core.llm_priority import user_llm_activity
from app.core.sse import format_event
from app.db import get_engine, get_sessionmaker
from app.llm.ollama import Ollama, OllamaError, OllamaUnavailable
from app.models import AuditLog, Chat, ChatMessage
from app.search.hybrid import Candidate, SearchFilters
from app.search.service import retrieve

log = logging.getLogger(__name__)


@dataclass
class AskContext:
    question: str
    filters: SearchFilters
    access: DocumentAccess
    queue_priority: int
    audit_id: int
    history: list[dict[str, str]] = field(default_factory=list)
    chat_id: int | None = None
    # Сообщение-ответ, созданное заранее со статусом pending (только для вошедших).
    message_id: int | None = None


def source_item(n: int, c: Candidate) -> dict[str, Any]:
    return {
        "n": n,
        "document_id": c.document_id,
        "chunk_id": c.chunk_id,
        "caption": c.caption,
        "place": c.place,
        "source_name": c.source_name,
        "legal_status": c.legal_status,
        "snippet": c.snippet,
        "score": round(c.rerank if c.rerank is not None else c.rrf, 4),
        "cited": False,
    }


def unavailable_out(access: DocumentAccess) -> list[dict[str, Any]]:
    return [
        {
            "id": u.id,
            "name": u.name,
            "message": f"Документы из системы «{u.name}» сейчас недоступны",
        }
        for u in access.unavailable
    ]


def _context_query(question: str, history: list[dict[str, str]]) -> str | None:
    prev = next((h["content"] for h in reversed(history) if h.get("role") == "user"), None)
    return f"{prev}\n{question}" if prev else None


async def _finish(
    ctx: AskContext,
    *,
    answer: str,
    sources: list[dict[str, Any]],
    status: str,
    meta: dict[str, Any],
) -> None:
    """Сохраняет ответ и дописывает в аудит, какие документы попали в ответ."""
    # Отмена (пользователь закрыл вкладку) не должна помешать записать итог.
    with anyio.CancelScope(shield=True):
        try:
            async with get_sessionmaker()() as s, s.begin():
                if ctx.message_id is not None:
                    await s.execute(
                        update(ChatMessage)
                        .where(ChatMessage.id == ctx.message_id)
                        .values(content=answer, sources=sources, status=status, meta=meta)
                    )
                    await s.execute(
                        update(Chat)
                        .where(Chat.id == ctx.chat_id)
                        .values(updated_at=datetime.now(UTC))
                    )
                audit = await s.get(AuditLog, ctx.audit_id)
                if audit is not None:
                    audit.details = {
                        **(audit.details or {}),
                        "status": status,
                        "context_documents": sorted({x["document_id"] for x in sources}),
                        "cited_documents": sorted(
                            {x["document_id"] for x in sources if x.get("cited")}
                        ),
                    }
        except Exception:  # noqa: BLE001 — запись итога не должна ронять поток
            log.exception("Не удалось сохранить ответ")


async def answer_stream(ctx: AskContext, settings: Settings) -> AsyncIterator[bytes]:
    t_start = time.perf_counter()
    answer = ""
    sources: list[dict[str, Any]] = []
    meta: dict[str, Any] = {"filters": ctx.filters.as_dict()}
    status = "interrupted"
    queue = LlmQueue(ctx.queue_priority, settings.llm_queue_stale_seconds)
    try:
        yield format_event(
            {
                "chat_id": ctx.chat_id,
                "message_id": ctx.message_id,
                "unavailable_sources": unavailable_out(ctx.access),
            },
            "meta",
        )
        yield format_event({"stage": "search"}, "status")
        async with get_sessionmaker()() as s:
            r = await retrieve(
                s,
                ctx.access,
                ctx.question,
                ctx.filters,
                settings,
                context_query=_context_query(ctx.question, ctx.history),
            )
        meta.update(
            found=r.found,
            reranked=r.reranked,
            vector_ok=r.vector_ok,
            timings_ms=r.timings_ms,
        )
        if not r.vector_ok:
            meta["warning"] = "Поиск по смыслу временно недоступен, искали по словам"
        yield format_event({"stage": "rerank", "found": r.found, "reranked": r.reranked}, "status")

        context, used = build_context(r.selected, settings.rag_context_chars)
        sources = [source_item(i, c) for i, c in enumerate(used, start=1)]
        meta["selected"] = len(used)
        yield format_event({"items": sources, "warning": meta.get("warning")}, "sources")

        if not used:
            # Ничего подходящего: модель не нужна, выдумывать ей не из чего.
            answer = NO_ANSWER
            status = "done"
            meta["no_answer"] = True
            meta["elapsed_ms"] = int((time.perf_counter() - t_start) * 1000)
            yield format_event({"text": answer}, "token")
            yield format_event({"answer": answer, "cited": [], **_public(meta)}, "done")
            return

        await queue.enter()
        async for position in queue.wait_turn():
            yield format_event({"stage": "queue", "position": position}, "status")
        yield format_event({"stage": "generate", "selected": len(used)}, "status")

        messages = build_messages(ctx.question, context, ctx.history)
        t_gen = time.perf_counter()
        stats: dict[str, Any] = {}
        model = None
        async with get_engine().connect() as conn, user_llm_activity(conn):
            async with Ollama(settings) as client:
                async for delta in client.chat_stream(messages):
                    if delta.text:
                        answer += delta.text
                        yield format_event({"text": delta.text}, "token")
                    if delta.done:
                        stats, model = delta.stats, delta.model
        await queue.leave()

        cited = [] if is_refusal(answer) else cited_numbers(answer, len(sources))
        for item in sources:
            item["cited"] = item["n"] in cited
        status = "done"
        meta.update(
            model=model,
            no_answer=is_refusal(answer),
            generate_ms=int((time.perf_counter() - t_gen) * 1000),
            elapsed_ms=int((time.perf_counter() - t_start) * 1000),
        )
        if stats.get("eval_count") and stats.get("eval_duration"):
            meta["tokens"] = stats["eval_count"]
            meta["tokens_per_second"] = round(
                stats["eval_count"] / (stats["eval_duration"] / 1e9), 1
            )
        yield format_event(
            {"answer": answer, "cited": cited, "sources": sources, **_public(meta)}, "done"
        )
    except OllamaError as exc:
        status = "error"
        meta["error"] = str(exc)
        message = (
            "Модель сейчас недоступна. Найденные фрагменты — справа, ответ сформировать не удалось."
            if isinstance(exc, OllamaUnavailable)
            else "Модель не смогла сформировать ответ. Найденные фрагменты — справа."
        )
        log.warning("Ответ не сформирован: %s", exc)
        yield format_event({"code": "llm_unavailable", "message": message}, "error")
    except Exception:
        status = "error"
        log.exception("Ошибка при ответе на вопрос")
        yield format_event(
            {"code": "internal", "message": "Не удалось ответить. Попробуйте ещё раз."}, "error"
        )
    finally:
        with anyio.CancelScope(shield=True):
            try:
                await queue.leave()
            except Exception:  # noqa: BLE001
                log.exception("Не удалось выйти из очереди к LLM")
        meta.setdefault("elapsed_ms", int((time.perf_counter() - t_start) * 1000))
        await _finish(ctx, answer=answer, sources=sources, status=status, meta=meta)


def _public(meta: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "elapsed_ms",
        "found",
        "selected",
        "reranked",
        "model",
        "tokens_per_second",
        "warning",
        "no_answer",
    )
    return {k: meta[k] for k in keys if k in meta}
