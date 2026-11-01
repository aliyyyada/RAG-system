"""Гибридный поиск фрагментов: вектор (bge-m3) + полнотекстовый (PostgreSQL), слияние RRF.

Пример. Вопрос «Какой срок у наряда-допуска?»:
1. Полнотекстовый поиск: слова вопроса → запрос «какой | срок | наряд | допуск» с русской
   морфологией («наряда» найдёт «наряд», «наряду»). Лучше всего находит точные слова,
   номера («приказ 112») и шифры («ИОТ-044»).
2. Векторный поиск: вектор вопроса сравнивается с векторами фрагментов по смыслу —
   найдёт «Наряд выдаётся на срок не более 15 дней», даже если спросили «сколько действует наряд».
3. RRF (Reciprocal Rank Fusion): у каждого фрагмента score = Σ 1 / (60 + место в списке).
   Фрагмент, который высоко в обоих списках, поднимается наверх; оценки двух поисков
   несравнимы между собой, а места — сравнимы.

**Права проверяются в SQL до поиска**: оба запроса содержат условие `visible(access)`
(app/access/filters.py), поэтому фрагмент недоступного документа не попадает даже
в кандидаты — ни в реранкер, ни в LLM, ни в ответ.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import ColumnElement, Float, Select, func, literal, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.filters import visible
from app.access.service import DocumentAccess
from app.ingest.assemble import HeaderMeta, document_caption
from app.models import EMBEDDING_DIM, Chunk, Document, Source, Vector

# Метки подсветки найденных слов в отрывке. Фронтенд режет по ним текст и показывает
# <mark> — без v-html, поэтому текст документа не может внедрить разметку.
HL_START = "⟦"  # ⟦
HL_END = "⟧"  # ⟧

_WORD = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")
# Статусы источника, при которых документ не считается действующим. Этап 5 заменит это
# статусом, вычисленным по связям «отменяет / изменяет».
INACTIVE_STATUSES = ("отменён", "отменен", "утратил силу", "недействующий", "архив")


@dataclass(frozen=True)
class SearchFilters:
    """Фильтры поиска из панели над чатом. Все необязательные."""

    source: str | None = None  # "manual" или id источника
    collection_id: int | None = None
    doc_type: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    only_active: bool = False

    def conditions(self) -> list[ColumnElement[bool]]:
        cond: list[ColumnElement[bool]] = []
        if self.source == "manual":
            cond.append(Document.source_id.is_(None))
        elif self.source:
            cond.append(Document.source_id == int(self.source))
        if self.collection_id is not None:
            cond.append(Document.collection_id == self.collection_id)
        if self.doc_type:
            cond.append(Document.doc_type == self.doc_type)
        if self.date_from:
            cond.append(Document.doc_date >= self.date_from)
        if self.date_to:
            cond.append(Document.doc_date <= self.date_to)
        if self.only_active:
            status = func.lower(func.coalesce(Document.header["status"].astext, ""))
            cond.append(status.not_in(INACTIVE_STATUSES))
        return cond

    def as_dict(self) -> dict[str, Any]:
        return {
            k: (v.isoformat() if isinstance(v, date) else v)
            for k, v in self.__dict__.items()
            if v not in (None, False, "")
        }


@dataclass
class Candidate:
    """Найденный фрагмент с местами в обоих поисках и итоговыми оценками."""

    chunk_id: int
    vector_rank: int | None = None
    text_rank: int | None = None
    vector_distance: float | None = None
    rrf: float = 0.0
    rerank: float | None = None
    # Заполняется load_details():
    document_id: int = 0
    text: str = ""
    kind: str = ""
    section: str | None = None
    point: str | None = None
    page_from: int | None = None
    page_to: int | None = None
    caption: str = ""
    source_name: str = ""
    legal_status: str | None = None
    snippet: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def place(self) -> str:
        return chunk_place(self.kind, self.page_from, self.page_to, self.section, self.point)


def chunk_place(
    kind: str,
    page_from: int | None,
    page_to: int | None,
    section: str | None,
    point: str | None,
) -> str:
    """«стр. 2, п. 4.2» — где в документе фрагмент (для ссылки на источник)."""
    if kind == "header":
        return "реквизиты из источника"
    parts: list[str] = []
    if page_from is not None:
        if page_to is None or page_to == page_from:
            parts.append(f"стр. {page_from}")
        else:
            parts.append(f"стр. {page_from}–{page_to}")
    if point:
        parts.append(f"п. {point}")
    elif section:
        parts.append(section if len(section) <= 60 else section[:57] + "…")
    if kind == "table" and not point:
        parts.append("таблица")
    return ", ".join(parts)


def tsquery_text(query: str) -> str:
    """Слова вопроса через ИЛИ для to_tsquery('russian', …).

    websearch_to_tsquery требует все слова сразу (И), а вопрос на естественном языке
    («Какие документы нужны для допуска…») почти никогда не совпадает с текстом целиком.
    ИЛИ + ранжирование ts_rank_cd: чем больше слов совпало и чем они ближе, тем выше.
    Стоп-слова («какие», «для») PostgreSQL выбросит сам. В запрос идут только буквы и
    цифры, поэтому спецсимволы tsquery (&, |, !, :) пользователь подставить не может.
    """
    seen: list[str] = []
    for w in _WORD.findall(query.lower()):
        if w not in seen:
            seen.append(w)
    return " | ".join(seen[:32])


def _doc_scope(access: DocumentAccess, filters: SearchFilters) -> list[ColumnElement[bool]]:
    return [visible(access), *filters.conditions()]


_iterative_scan: bool | None = None


async def _has_iterative_scan(session: AsyncSession) -> bool:
    """iterative_scan появился в pgvector 0.8 (в нашем образе postgres — 0.8.7)."""
    global _iterative_scan
    if _iterative_scan is None:
        version = await session.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        parts = tuple(int(x) for x in str(version or "0").split(".")[:2] if x.isdigit())
        _iterative_scan = parts >= (0, 8)
    return _iterative_scan


async def _vector_hits(
    session: AsyncSession,
    access: DocumentAccess,
    filters: SearchFilters,
    query_vector: list[float],
    limit: int,
) -> list[tuple[int, float]]:
    # HNSW-индекс ищет ближайшие векторы, а условие прав отбрасывает чужие. Чтобы после
    # отбрасывания осталось достаточно кандидатов, индекс продолжает поиск (iterative_scan).
    await session.execute(text("SET LOCAL hnsw.ef_search = 100"))
    if await _has_iterative_scan(session):
        await session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    q = literal(query_vector, Vector(EMBEDDING_DIM))
    distance = Chunk.embedding.op("<=>", return_type=Float())(q)
    stmt: Select[Any] = (
        select(Chunk.id, distance.label("dist"))
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.embedding.is_not(None), *_doc_scope(access, filters))
        .order_by(distance)
        .limit(limit)
    )
    rows = await session.execute(stmt)
    return [(int(cid), float(dist)) for cid, dist in rows.all()]


async def _text_hits(
    session: AsyncSession,
    access: DocumentAccess,
    filters: SearchFilters,
    query: str,
    limit: int,
) -> list[int]:
    tsq_text = tsquery_text(query)
    if not tsq_text:
        return []
    tsq = func.to_tsquery("russian", tsq_text)
    rank = func.ts_rank_cd(Chunk.tsv, tsq)
    rows = await session.execute(
        select(Chunk.id)
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.tsv.op("@@")(tsq), *_doc_scope(access, filters))
        .order_by(rank.desc(), Chunk.id)
        .limit(limit)
    )
    return [int(cid) for cid in rows.scalars()]


def rrf_fuse(vector: list[tuple[int, float]], textual: list[int], k: int) -> list[Candidate]:
    """Слияние двух списков по местам (RRF). Места считаются с 1."""
    by_id: dict[int, Candidate] = {}
    for rank, (cid, dist) in enumerate(vector, start=1):
        c = by_id.setdefault(cid, Candidate(chunk_id=cid))
        c.vector_rank, c.vector_distance = rank, dist
        c.rrf += 1.0 / (k + rank)
    for rank, cid in enumerate(textual, start=1):
        c = by_id.setdefault(cid, Candidate(chunk_id=cid))
        c.text_rank = rank
        c.rrf += 1.0 / (k + rank)
    return sorted(by_id.values(), key=lambda c: (-c.rrf, c.chunk_id))


async def hybrid_search(
    session: AsyncSession,
    access: DocumentAccess,
    query: str,
    query_vector: list[float] | None,
    filters: SearchFilters,
    *,
    candidates: int,
    rrf_k: int,
) -> list[Candidate]:
    """Кандидаты из обоих поисков, слитые RRF. query_vector=None — только полнотекстовый
    (Ollama недоступна или фрагменты ещё не векторизованы)."""
    if not access.any:
        return []
    vec = (
        await _vector_hits(session, access, filters, query_vector, candidates)
        if query_vector is not None
        else []
    )
    txt = await _text_hits(session, access, filters, query, candidates)
    return rrf_fuse(vec, txt, rrf_k)


async def load_details(
    session: AsyncSession,
    access: DocumentAccess,
    found: list[Candidate],
    query: str,
) -> list[Candidate]:
    """Текст, реквизиты документа и отрывок с подсветкой для найденных фрагментов.

    Права проверяются ещё раз (visible): между поиском и этим запросом источник мог
    отозвать права, а кэш — обновиться.
    """
    if not found:
        return []
    tsq_text = tsquery_text(query) or "_"
    tsq = func.to_tsquery("russian", tsq_text)
    options = (
        f"StartSel={HL_START}, StopSel={HL_END}, MaxWords=40, MinWords=18, "
        'MaxFragments=1, FragmentDelimiter=" … ", HighlightAll=false'
    )
    headline = func.ts_headline("russian", Chunk.text, tsq, options)
    rows = await session.execute(
        select(Chunk, Document, Source.name, headline.label("snippet"))
        .join(Document, Document.id == Chunk.document_id)
        .outerjoin(Source, Source.id == Document.source_id)
        .where(Chunk.id.in_([c.chunk_id for c in found]), visible(access))
    )
    by_id = {ch.id: (ch, doc, src, snip) for ch, doc, src, snip in rows.all()}
    out: list[Candidate] = []
    for c in found:
        row = by_id.get(c.chunk_id)
        if row is None:
            continue
        ch, doc, src, snip = row
        status = doc.header.get("status") if isinstance(doc.header, dict) else None
        c.document_id = doc.id
        c.text, c.kind = ch.text, ch.kind
        c.section, c.point = ch.section, ch.point
        c.page_from, c.page_to = ch.page_from, ch.page_to
        c.caption = document_caption(
            HeaderMeta(doc.number, doc.title, doc.doc_type, doc.topic, doc.doc_date)
        )
        c.source_name = src or "Ручная загрузка"
        c.legal_status = str(status) if status else None
        c.snippet = _clean_snippet(str(snip or ""))
        out.append(c)
    return out


def _clean_snippet(snippet: str) -> str:
    """Отрывок одной строкой: без Markdown-разметки таблиц и заголовков."""
    s = re.sub(r"^#+\s*", "", snippet, flags=re.M)
    s = re.sub(r"\|\s*-{3,}\s*(\|\s*-{3,}\s*)*\|", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


__all__ = [
    "HL_END",
    "HL_START",
    "Candidate",
    "SearchFilters",
    "chunk_place",
    "hybrid_search",
    "load_details",
    "rrf_fuse",
    "tsquery_text",
]
