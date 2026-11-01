"""Поиск целиком: вектор вопроса → гибридный поиск → реранкер → лучшие фрагменты.

Используется и чатом (app/chat/service.py), и простым поиском /api/search, и оценкой
качества (app/search/evaluate.py), поэтому все три ищут одинаково.
"""

import logging
import time
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.access.service import DocumentAccess
from app.config import Settings
from app.llm.ollama import Ollama, OllamaError
from app.search.hybrid import Candidate, SearchFilters, hybrid_search, load_details, rrf_fuse
from app.search.rerank import get_reranker

log = logging.getLogger(__name__)


@dataclass
class Retrieval:
    # Все найденные (после RRF, с текстом), в порядке итоговой оценки.
    ranked: list[Candidate]
    # Лучшие rag_top_k, прошедшие порог реранкера: они идут в модель.
    selected: list[Candidate]
    found: int
    reranked: bool
    vector_ok: bool
    timings_ms: dict[str, int] = field(default_factory=dict)


async def embed_query(texts: list[str], settings: Settings) -> list[list[float]] | None:
    try:
        async with Ollama(settings) as client:
            return await client.embed(texts)
    except OllamaError as exc:
        # Без вектора поиск продолжится полнотекстовым — ответ хуже, но он есть.
        log.warning("Вектор вопроса не получен: %s", exc)
        return None


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


async def retrieve(
    session: AsyncSession,
    access: DocumentAccess,
    query: str,
    filters: SearchFilters,
    settings: Settings,
    *,
    context_query: str | None = None,
    top_k: int | None = None,
) -> Retrieval:
    """context_query — вопрос вместе с предыдущим вопросом диалога. На уточнение
    «а для сварщика?» поиск только по нему ничего не найдёт, поэтому ищем по обоим
    вариантам и сливаем результаты тем же RRF."""
    top_k = top_k or settings.rag_top_k
    timings: dict[str, int] = {}
    queries = [query] + ([context_query] if context_query and context_query != query else [])

    t0 = time.perf_counter()
    vectors = await embed_query(queries, settings)
    timings["embed"] = _ms(t0)

    t0 = time.perf_counter()
    lists: list[list[Candidate]] = []
    for i, q in enumerate(queries):
        vec = vectors[i] if vectors else None
        lists.append(
            await hybrid_search(
                session,
                access,
                q,
                vec,
                filters,
                candidates=settings.search_candidates,
                rrf_k=settings.search_rrf_k,
            )
        )
    fused = lists[0] if len(lists) == 1 else _merge(lists)
    found = len(fused)
    top = fused[: settings.rerank_top_n]
    details = await load_details(session, access, top, queries[-1])
    timings["search"] = _ms(t0)

    reranker = get_reranker(settings)
    reranked = False
    ranked = details
    if reranker is not None and details:
        t0 = time.perf_counter()
        try:
            scores = await reranker.score(queries[-1], [c.text for c in details])
            for c, sc in zip(details, scores, strict=True):
                c.rerank = sc
            ranked = sorted(details, key=lambda c: -(c.rerank or 0.0))
            reranked = True
        except Exception:  # noqa: BLE001 — реранкер не должен ронять поиск
            log.exception("Ошибка реранкера, используем порядок RRF")
        timings["rerank"] = _ms(t0)

    if reranked:
        selected = [c for c in ranked if (c.rerank or 0.0) >= settings.rerank_min_score][:top_k]
    else:
        selected = ranked[:top_k]
    return Retrieval(
        ranked=ranked,
        selected=selected,
        found=found,
        reranked=reranked,
        vector_ok=vectors is not None,
        timings_ms=timings,
    )


def _merge(lists: list[list[Candidate]]) -> list[Candidate]:
    """Сложить RRF-оценки нескольких поисков (по вопросу и по вопросу с контекстом)."""
    by_id: dict[int, Candidate] = {}
    for cands in lists:
        for c in cands:
            prev = by_id.get(c.chunk_id)
            if prev is None:
                by_id[c.chunk_id] = c
            else:
                prev.rrf += c.rrf
                prev.vector_rank = min(
                    filter(None, (prev.vector_rank, c.vector_rank)), default=None
                )
                prev.text_rank = min(filter(None, (prev.text_rank, c.text_rank)), default=None)
    return sorted(by_id.values(), key=lambda c: (-c.rrf, c.chunk_id))


__all__ = ["Retrieval", "embed_query", "retrieve", "rrf_fuse"]
