"""Графовый поиск: вопрос → документы и сущности графа → фрагменты-доказательства.

Обычный поиск (этап 4) ищет фрагменты, похожие на вопрос. На вопросы о связях этого мало:
«Что отменило приказ 45?» — в приказе № 45 об этом ни слова, ответ — в приказе № 112
(«Приказ № 45 … признать утратившим силу»). Графовый поиск находит такие фрагменты так:

1. **Документы, названные в вопросе** («приказ 45», «ИОТ-044»): связи к ним и от них
   (document_links) → фрагменты, где эти связи написаны; плюс шапка самого документа.
2. **Сущности, названные в вопросе** («работы на высоте», «литейный цех № 2»): связи
   сущностей (relations) → фрагменты-доказательства; упоминания (entity_mentions) → фрагменты.

Режимы (настройка SEARCH_MODE и переключатель в чате):
    chunks — только обычный поиск; graph — только граф; mixed — оба списка, слитые RRF.

**Права.** Каждый шаг ограничен `visible(access)` и фильтрами поиска на уровне SQL:
сущность находится, только если она упомянута в доступном человеку документе; связь
документов берётся, только если доступны и документ-доказательство, и документ-цель.
Поэтому граф не подскажет даже факт «приказ № 45 отменён приказом № 112» тому, кому
приказ № 112 не виден.

Найденные связи (Fact) идут в промпт модели справкой — только те, чей фрагмент-доказательство
попал в ответ (app/chat/prompt.py).
"""

from dataclasses import dataclass, field

from sqlalchemy import ColumnElement, and_, case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.access.filters import visible
from app.access.service import DocumentAccess
from app.graph.extract import ENTITY_LABELS, PREDICATE_LABELS
from app.graph.references import KIND_LABELS, find_references, normalize_number, type_matches
from app.ingest.assemble import HeaderMeta, document_caption
from app.models import Chunk, Document, DocumentLink, Entity, EntityMention, Relation
from app.search.hybrid import Candidate, SearchFilters, tsquery_text

# Сначала «отменяет», потом «изменяет», «во исполнение», «ссылается на».
_KIND_ORDER = case({"cancels": 0, "amends": 1, "executes": 2}, value=DocumentLink.kind, else_=3)


@dataclass
class Fact:
    """Связь из графа для справки модели: «Приказ № 112 … отменяет Приказ № 45 …»."""

    text: str
    chunk_id: int


@dataclass
class GraphHits:
    candidates: list[Candidate] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    documents: list[int] = field(default_factory=list)
    entities: list[dict[str, object]] = field(default_factory=list)


def caption_of(doc: Document) -> str:
    return document_caption(
        HeaderMeta(doc.number, doc.title, doc.doc_type, doc.topic, doc.doc_date)
    )


def short_caption(doc: Document) -> str:
    """«Приказ № 112 от 14.03.2024» — без названия, для справки модели."""
    parts = [p for p in (doc.doc_type, f"№ {doc.number}" if doc.number else None) if p]
    if doc.doc_date:
        parts.append(f"от {doc.doc_date:%d.%m.%Y}")
    return " ".join(parts) or caption_of(doc)


def visible_ids(access: DocumentAccess) -> ColumnElement[bool]:
    """`Document.id IN (видимые)` — для второго документа связи (цели), когда первый уже в JOIN."""
    return Document.id.in_(select(Document.id).where(visible(access)))


def _scope(access: DocumentAccess, filters: SearchFilters) -> list[ColumnElement[bool]]:
    return [visible(access), *filters.conditions()]


async def documents_in_query(
    session: AsyncSession, access: DocumentAccess, query: str, limit: int = 3
) -> list[Document]:
    """Документы, названные в вопросе («что отменило приказ 45?»), среди доступных."""
    out: list[Document] = []
    for ref in find_references(query, strict=False):
        core = ref.number_norm.split("-")[0]
        rows = (
            await session.scalars(
                select(Document)
                .where(
                    visible(access),
                    Document.number.is_not(None),
                    func.lower(Document.number).contains(core),
                )
                .limit(50)
            )
        ).all()
        for d in rows:
            if normalize_number(d.number or "") != ref.number_norm:
                continue
            if not type_matches(ref.doc_type, d.doc_type):
                continue
            if ref.doc_date is not None and d.doc_date not in (None, ref.doc_date):
                continue
            if all(d.id != x.id for x in out):
                out.append(d)
        if len(out) >= limit:
            break
    return out[:limit]


async def _links_of(
    session: AsyncSession,
    access: DocumentAccess,
    filters: SearchFilters,
    doc: Document,
) -> tuple[list[int], list[Fact]]:
    """Фрагменты-доказательства связей документа (входящих и исходящих) + справка."""
    src = aliased(Document)
    chunks: list[int] = []
    facts: list[Fact] = []
    # Входящие: кто отменяет / изменяет / ссылается на doc. Документ-источник связи — в
    # области поиска (права + фильтры): его фрагмент пойдёт в ответ.
    incoming = (
        await session.execute(
            select(DocumentLink, Document)
            .join(Document, Document.id == DocumentLink.from_document_id)
            .where(DocumentLink.target_document_id == doc.id, *_scope(access, filters))
            .order_by(
                _KIND_ORDER,
                Document.doc_date.desc().nulls_last(),
            )
            .limit(20)
        )
    ).all()
    for link, from_doc in incoming:
        chunks.append(link.chunk_id)
        facts.append(
            Fact(
                f"{short_caption(from_doc)} {KIND_LABELS[link.kind]} {short_caption(doc)}",
                link.chunk_id,
            )
        )
    # Исходящие: что отменяет / изменяет сам doc. Фрагмент — из doc (он в области поиска,
    # если удовлетворяет фильтрам); цель должна быть видна, иначе берём только текст ссылки.
    doc_in_scope = await session.scalar(
        select(func.count())
        .select_from(Document)
        .where(Document.id == doc.id, *_scope(access, filters))
    )
    if doc_in_scope:
        outgoing = (
            await session.execute(
                select(DocumentLink, src)
                .outerjoin(
                    src,
                    and_(
                        src.id == DocumentLink.target_document_id,
                        src.id.in_(select(Document.id).where(visible(access))),
                    ),
                )
                .where(DocumentLink.from_document_id == doc.id)
                .order_by(
                    _KIND_ORDER,
                    DocumentLink.id,
                )
                .limit(20)
            )
        ).all()
        for link, target in outgoing:
            chunks.append(link.chunk_id)
            label = short_caption(target) if target is not None else link.target_label
            facts.append(
                Fact(f"{short_caption(doc)} {KIND_LABELS[link.kind]} {label}", link.chunk_id)
            )
        head = await session.scalar(
            select(Chunk.id).where(Chunk.document_id == doc.id).order_by(Chunk.ord).limit(1)
        )
        if head is not None:
            chunks.append(int(head))
    return chunks, facts


def _mention_in_scope(access: DocumentAccess, filters: SearchFilters) -> ColumnElement[bool]:
    return exists(
        select(EntityMention.id)
        .join(Document, Document.id == EntityMention.document_id)
        .where(EntityMention.entity_id == Entity.id, *_scope(access, filters))
    )


async def entities_in_query(
    session: AsyncSession,
    access: DocumentAccess,
    filters: SearchFilters,
    query: str,
    limit: int,
) -> list[tuple[Entity, int]]:
    """Сущности, названные в вопросе, среди упомянутых в доступных документах.

    Сравниваем основы слов (русская морфология PostgreSQL): «за работы на высоте» найдёт
    сущность «работы на высоте» (совпали 2 из 2 основ). Сущность берём, если совпало не
    меньше половины её слов, — иначе «работы» нашли бы все виды работ сразу."""
    tsq_text = tsquery_text(query)
    if not tsq_text:
        return []
    q_lex = set(
        await session.scalar(select(func.tsvector_to_array(func.to_tsvector("russian", query))))
        or []
    )
    rows = (
        await session.execute(
            select(Entity, func.tsvector_to_array(Entity.name_tsv))
            .where(
                Entity.name_tsv.op("@@")(func.to_tsquery("russian", tsq_text)),
                _mention_in_scope(access, filters),
            )
            .limit(200)
        )
    ).all()
    scored: list[tuple[int, float, int, Entity]] = []
    for ent, lex in rows:
        lexemes = set(lex or [])
        if not lexemes:
            continue
        matched = len(lexemes & q_lex)
        cover = matched / len(lexemes)
        if matched and (cover >= 0.5 or matched >= 2):
            scored.append((matched, cover, ent.id, ent))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    return [(ent, matched) for matched, _, _, ent in scored[:limit]]


async def _entity_chunks(
    session: AsyncSession,
    access: DocumentAccess,
    filters: SearchFilters,
    matched: list[tuple[Entity, int]],
) -> tuple[list[int], list[Fact]]:
    ids = [e.id for e, _ in matched]
    if not ids:
        return [], []
    subj, obj = aliased(Entity), aliased(Entity)
    both = (Relation.subject_id.in_(ids)) & (Relation.object_id.in_(ids))
    rel_rows = (
        await session.execute(
            select(Relation, subj, obj)
            .join(subj, subj.id == Relation.subject_id)
            .join(obj, obj.id == Relation.object_id)
            .join(Document, Document.id == Relation.document_id)
            .where(
                or_(Relation.subject_id.in_(ids), Relation.object_id.in_(ids)),
                *_scope(access, filters),
            )
            .order_by(both.desc(), Document.doc_date.desc().nulls_last(), Relation.id)
            .limit(40)
        )
    ).all()
    chunks: list[int] = []
    facts: list[Fact] = []
    for rel, s, o in rel_rows:
        chunks.append(rel.chunk_id)
        facts.append(
            Fact(
                f"{s.name} ({ENTITY_LABELS.get(s.type, s.type).lower()}) — "
                f"{PREDICATE_LABELS.get(rel.predicate, rel.predicate)} — {o.name}",
                rel.chunk_id,
            )
        )
    order = {eid: i for i, eid in enumerate(ids)}
    men_rows = (
        await session.execute(
            select(EntityMention.entity_id, EntityMention.chunk_id)
            .join(Document, Document.id == EntityMention.document_id)
            .where(EntityMention.entity_id.in_(ids), *_scope(access, filters))
            .order_by(Document.doc_date.desc().nulls_last(), EntityMention.chunk_id)
            .limit(60)
        )
    ).all()
    for _, chunk_id in sorted(men_rows, key=lambda r: order.get(r[0], 99)):
        chunks.append(chunk_id)
    return chunks, facts


async def graph_candidates(
    session: AsyncSession,
    access: DocumentAccess,
    query: str,
    filters: SearchFilters,
    *,
    entities_limit: int,
    limit: int,
    rrf_k: int,
) -> GraphHits:
    hits = GraphHits()
    if not access.any:
        return hits
    ordered: list[int] = []
    for doc in await documents_in_query(session, access, query):
        hits.documents.append(doc.id)
        chunks, facts = await _links_of(session, access, filters, doc)
        ordered += chunks
        hits.facts += facts
    matched = await entities_in_query(session, access, filters, query, entities_limit)
    hits.entities = [{"id": e.id, "type": e.type, "name": e.name} for e, _ in matched]
    chunks, facts = await _entity_chunks(session, access, filters, matched)
    ordered += chunks
    hits.facts += facts

    seen: set[int] = set()
    for chunk_id in ordered:
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        rank = len(seen)
        hits.candidates.append(
            Candidate(chunk_id=chunk_id, graph_rank=rank, rrf=1.0 / (rrf_k + rank))
        )
        if len(hits.candidates) >= limit:
            break
    return hits
