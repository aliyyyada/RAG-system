"""Граф знаний (этап 5).

Для пользователя (права те же, что у «Документов»):
    GET /api/documents/{id}/graph  — связи документа (отменяет / изменён / ссылается) и
                                     сущности, упомянутые в нём;
    GET /api/graph/entities        — поиск сущностей («литейный цех», «Иванова»);
    GET /api/graph/entities/{id}   — сущность: связи и документы, где она упомянута.
Для администратора (admin.integrations):
    GET  /api/admin/graph               — прогресс, прогноз времени, счётчики;
    POST /api/admin/graph/llm           — включить / выключить извлечение моделью;
    POST /api/admin/graph/estimate      — замерить время на выборке (в фоне);
    POST /api/admin/graph/rebuild-links — пересобрать связи по правилам;
    POST /api/admin/graph/retry-errors  — повторить фрагменты с ошибкой модели.

**Права.** Всё, что видит пользователь, проходит через `visible(access)`: связь документов —
только если доступны оба документа (недоступная цель показывается лишь реквизитами из
текста доступного документа), сущность — если упомянута в доступном фрагменте, связь
сущностей — если доступен её фрагмент-доказательство.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, distinct, func, select, text, update
from sqlalchemy.orm import aliased

from app.access.filters import visible
from app.access.service import resolve_access
from app.api.documents import Reader, _visible_document
from app.auth import permissions as perms
from app.auth.deps import SessionDep, SettingsDep, client_ip, require_permission
from app.auth.rbac import Principal
from app.core.flags import GRAPH_LLM_ENABLED, get_flag, set_flag
from app.graph import extract, rules
from app.graph.estimate import ESTIMATE_JOB
from app.graph.references import INCOMING_LABELS, KIND_LABELS
from app.graph.search import caption_of
from app.graph.status import CANCELLED, is_inactive_header
from app.models import (
    AuditLog,
    Chunk,
    Document,
    DocumentLink,
    Entity,
    EntityMention,
    Job,
    Relation,
)
from app.worker import queue

router = APIRouter(tags=["граф знаний"])

Integrations = Annotated[Principal, Depends(require_permission(perms.ADMIN_INTEGRATIONS))]


def _err(code: int, key: str, message: str) -> HTTPException:
    return HTTPException(status_code=code, detail={"code": key, "message": message})


# ---------------------------------------------------------------------------
# Схемы
# ---------------------------------------------------------------------------


class LinkedDoc(BaseModel):
    id: int
    caption: str
    legal_status: str | None


class LinkOut(BaseModel):
    kind: str
    kind_label: str
    # Документ на другом конце связи; None — его нет в системе или он вам недоступен
    # (тогда только реквизиты, как они написаны в тексте).
    document: LinkedDoc | None
    label: str
    # Где связь написана: документ и фрагмент (ссылка «открыть на этом месте»).
    evidence_document_id: int
    chunk_id: int
    quote: str


class EntityBrief(BaseModel):
    id: int
    type: str
    type_label: str
    name: str
    mentions: int = 0


class DocumentGraph(BaseModel):
    legal_status: str | None
    # «Отменён», но отменяющий документ вам недоступен (или статус пришёл из источника).
    status_note: str | None
    outgoing: list[LinkOut]
    incoming: list[LinkOut]
    entities: list[EntityBrief]


class EntityPage(BaseModel):
    items: list[EntityBrief]
    total: int


class RelationOut(BaseModel):
    subject: EntityBrief
    predicate: str
    predicate_label: str
    object: EntityBrief
    document_id: int
    document_caption: str
    chunk_id: int


class EntityDocument(BaseModel):
    id: int
    caption: str
    legal_status: str | None
    chunk_id: int


class EntityView(EntityBrief):
    relations: list[RelationOut]
    documents: list[EntityDocument]


def _brief(e: Entity, mentions: int = 0) -> EntityBrief:
    return EntityBrief(
        id=e.id,
        type=e.type,
        type_label=extract.ENTITY_LABELS.get(e.type, e.type),
        name=e.name,
        mentions=mentions,
    )


# ---------------------------------------------------------------------------
# Пользователь
# ---------------------------------------------------------------------------


@router.get("/api/documents/{document_id}/graph", response_model=DocumentGraph)
async def document_graph(
    document_id: int, principal: Reader, session: SessionDep, settings: SettingsDep
) -> DocumentGraph:
    access = await resolve_access(session, principal, settings)
    doc, _ = await _visible_document(session, access, document_id, principal)
    visible_doc_ids = select(Document.id).where(visible(access))
    other = aliased(Document)

    # Исходящие: фрагмент — этого документа (виден), цель — только если тоже видна.
    out_rows = (
        await session.execute(
            select(DocumentLink, other)
            .outerjoin(
                other,
                and_(
                    other.id == DocumentLink.target_document_id,
                    other.id.in_(visible_doc_ids),
                ),
            )
            .where(DocumentLink.from_document_id == doc.id)
            .order_by(DocumentLink.id)
        )
    ).all()
    outgoing: list[LinkOut] = []
    seen: set[tuple[str, str]] = set()
    for link, target in out_rows:
        key = (link.kind, str(target.id) if target else link.target_label)
        if key in seen:
            continue
        seen.add(key)
        outgoing.append(
            LinkOut(
                kind=link.kind,
                kind_label=KIND_LABELS[link.kind],
                document=LinkedDoc(
                    id=target.id, caption=caption_of(target), legal_status=target.legal_status
                )
                if target
                else None,
                label=caption_of(target) if target else link.target_label,
                evidence_document_id=doc.id,
                chunk_id=link.chunk_id,
                quote=link.quote,
            )
        )

    # Входящие: только от документов, которые человеку видны (иначе не показываем вовсе).
    in_rows = (
        await session.execute(
            select(DocumentLink, Document)
            .join(Document, Document.id == DocumentLink.from_document_id)
            .where(DocumentLink.target_document_id == doc.id, visible(access))
            .order_by(Document.doc_date.desc().nulls_last(), DocumentLink.id)
        )
    ).all()
    incoming: list[LinkOut] = []
    seen_in: set[tuple[str, int]] = set()
    for link, src in in_rows:
        if (link.kind, src.id) in seen_in:
            continue
        seen_in.add((link.kind, src.id))
        incoming.append(
            LinkOut(
                kind=link.kind,
                kind_label=INCOMING_LABELS[link.kind],
                document=LinkedDoc(
                    id=src.id, caption=caption_of(src), legal_status=src.legal_status
                ),
                label=caption_of(src),
                evidence_document_id=src.id,
                chunk_id=link.chunk_id,
                quote=link.quote,
            )
        )

    note = None
    if doc.legal_status == CANCELLED and not any(i.kind == "cancels" for i in incoming):
        header = doc.header.get("status") if isinstance(doc.header, dict) else None
        note = (
            "Отменён по данным системы-источника"
            if is_inactive_header(header)
            else "Отменён документом, который вам недоступен"
        )

    ent_rows = (
        await session.execute(
            select(Entity, func.count(distinct(EntityMention.chunk_id)))
            .join(EntityMention, EntityMention.entity_id == Entity.id)
            .where(EntityMention.document_id == doc.id)
            .group_by(Entity.id)
            .order_by(Entity.type, Entity.name)
            .limit(200)
        )
    ).all()
    return DocumentGraph(
        legal_status=doc.legal_status,
        status_note=note,
        outgoing=outgoing,
        incoming=incoming,
        entities=[_brief(e, n) for e, n in ent_rows],
    )


@router.get("/api/graph/entities", response_model=EntityPage)
async def list_entities(
    principal: Reader,
    session: SessionDep,
    settings: SettingsDep,
    q: str | None = Query(default=None, max_length=200),
    type: str | None = Query(default=None, pattern=r"^[a-z]{2,16}$"),  # noqa: A002
    limit: int = Query(default=50, ge=1, le=200),
) -> EntityPage:
    access = await resolve_access(session, principal, settings)
    cond = []
    if q and q.strip():
        cond.append(Entity.name.ilike(f"%{q.strip()}%"))
    if type:
        cond.append(Entity.type == type)
    mentions = func.count(distinct(EntityMention.chunk_id))
    base = (
        select(Entity, mentions.label("n"))
        .join(EntityMention, EntityMention.entity_id == Entity.id)
        .join(Document, Document.id == EntityMention.document_id)
        .where(visible(access), *cond)
        .group_by(Entity.id)
    )
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = (await session.execute(base.order_by(mentions.desc(), Entity.name).limit(limit))).all()
    return EntityPage(items=[_brief(e, n) for e, n in rows], total=total or 0)


@router.get("/api/graph/entities/{entity_id}", response_model=EntityView)
async def get_entity(
    entity_id: int, principal: Reader, session: SessionDep, settings: SettingsDep
) -> EntityView:
    access = await resolve_access(session, principal, settings)
    ent = await session.get(Entity, entity_id)
    doc_rows = (
        (
            await session.execute(
                select(Document, func.min(EntityMention.chunk_id))
                .join(EntityMention, EntityMention.document_id == Document.id)
                .where(EntityMention.entity_id == entity_id, visible(access))
                .group_by(Document.id)
                .order_by(Document.doc_date.desc().nulls_last())
                .limit(100)
            )
        ).all()
        if ent
        else []
    )
    if ent is None or not doc_rows:
        # Сущность, упомянутая только в чужих документах, для человека не существует.
        raise _err(404, "not_found", "Сущность не найдена")
    subj, obj = aliased(Entity), aliased(Entity)
    rel_rows = (
        await session.execute(
            select(Relation, subj, obj, Document)
            .join(subj, subj.id == Relation.subject_id)
            .join(obj, obj.id == Relation.object_id)
            .join(Document, Document.id == Relation.document_id)
            .where(
                (Relation.subject_id == entity_id) | (Relation.object_id == entity_id),
                visible(access),
            )
            .order_by(Relation.predicate, Relation.id)
            .limit(200)
        )
    ).all()
    return EntityView(
        **_brief(ent, len(doc_rows)).model_dump(),
        relations=[
            RelationOut(
                subject=_brief(s),
                predicate=r.predicate,
                predicate_label=extract.PREDICATE_LABELS.get(r.predicate, r.predicate),
                object=_brief(o),
                document_id=d.id,
                document_caption=caption_of(d),
                chunk_id=r.chunk_id,
            )
            for r, s, o, d in rel_rows
        ],
        documents=[
            EntityDocument(id=d.id, caption=caption_of(d), legal_status=d.legal_status, chunk_id=c)
            for d, c in doc_rows
        ],
    )


# ---------------------------------------------------------------------------
# Администратор
# ---------------------------------------------------------------------------

# Оценка до замера на сервере: qwen2.5:7b Q4 на GTX 1070 (см. app/graph/extract.py).
DEFAULT_SECONDS_PER_CHUNK = 12.0


class LlmToggle(BaseModel):
    enabled: bool


class EstimateIn(BaseModel):
    sample: int = Field(default=20, ge=3, le=200)


async def _last_estimate(session: SessionDep) -> dict[str, Any] | None:
    row = (
        await session.execute(
            select(AuditLog.created_at, AuditLog.details)
            .where(AuditLog.action == "graph.estimate")
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
    ).first()
    return {"at": row[0].isoformat(), **row[1]} if row else None


@router.get("/api/admin/graph")
async def graph_admin(
    principal: Integrations, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    links = dict(
        (
            await session.execute(
                select(DocumentLink.kind, func.count()).group_by(DocumentLink.kind)
            )
        ).all()
    )
    unresolved = await session.scalar(
        select(func.count())
        .select_from(DocumentLink)
        .where(DocumentLink.target_document_id.is_(None))
    )
    statuses = dict(
        (
            await session.execute(
                select(Document.legal_status, func.count())
                .where(Document.deleted_at.is_(None), Document.legal_status.is_not(None))
                .group_by(Document.legal_status)
            )
        ).all()
    )
    docs_indexed, docs_linked = (
        await session.execute(
            select(
                func.count().filter(Document.indexed_version.is_not(None)),
                func.count().filter(
                    Document.indexed_version.is_not(None),
                    Document.links_version == Document.indexed_version,
                ),
            ).where(Document.deleted_at.is_(None))
        )
    ).one()
    states = dict(
        (
            await session.execute(
                select(func.coalesce(Chunk.graph_state, "pending"), func.count()).group_by(
                    Chunk.graph_state
                )
            )
        ).all()
    )
    avg_ms = await session.scalar(
        text(
            "SELECT avg(graph_ms) FROM (SELECT graph_ms FROM chunks WHERE graph_state = 'done' "
            "AND graph_ms IS NOT NULL ORDER BY id DESC LIMIT 300) t"
        )
    )
    done, skipped = states.get("done", 0), states.get("skipped", 0)
    pending = states.get("pending", 0)
    last = await _last_estimate(session)
    if done + skipped >= 20:
        share = done / (done + skipped)
    elif last:
        share = float(last.get("eligible_share") or 0.0)
    else:
        share = 0.6
    if avg_ms is not None:
        seconds, basis = float(avg_ms) / 1000, "measured"
    elif last and last.get("avg_seconds") is not None:
        seconds, basis = float(last["avg_seconds"]), "estimate"
    else:
        seconds, basis = DEFAULT_SECONDS_PER_CHUNK, "assumption"
    entities = dict(
        (await session.execute(select(Entity.type, func.count()).group_by(Entity.type))).all()
    )
    jobs = dict(
        (
            await session.execute(
                select(Job.kind, func.count())
                .where(
                    Job.kind.in_([rules.RULES_JOB, extract.EXTRACT_JOB, ESTIMATE_JOB]),
                    Job.status.in_(["queued", "running"]),
                )
                .group_by(Job.kind)
            )
        ).all()
    )
    return {
        "llm_enabled": bool(await get_flag(session, GRAPH_LLM_ENABLED, False)),
        "model": settings.llm_model,
        "links": {
            "by_kind": {k: links.get(k, 0) for k in ("cancels", "amends", "executes", "refers")},
            "unresolved": unresolved or 0,
            "documents_indexed": docs_indexed,
            "documents_done": docs_linked,
            "statuses": statuses,
        },
        "llm": {
            "chunks": {s: states.get(s, 0) for s in ("pending", "done", "skipped", "error")},
            "seconds_per_chunk": round(seconds, 1),
            "seconds_basis": basis,
            "eligible_share": round(share, 3),
            "forecast_seconds": int(pending * share * seconds),
            "forecast_hours": round(pending * share * seconds / 3600, 1),
        },
        "entities": {"by_type": entities, "total": sum(entities.values())},
        "relations": await session.scalar(select(func.count()).select_from(Relation)) or 0,
        "jobs": jobs,
        "estimate_running": bool(jobs.get(ESTIMATE_JOB)),
        "last_estimate": last,
        "labels": {"entities": extract.ENTITY_LABELS, "kinds": KIND_LABELS},
    }


def _audit(principal: Principal, request: Request, action: str, **details: Any) -> AuditLog:
    u = principal.user
    return AuditLog(
        actor_user_id=u.id if u else None,
        actor_login=u.personnel_number if u else None,
        action=action,
        details=details,
        ip=client_ip(request),
    )


@router.post("/api/admin/graph/llm")
async def toggle_llm(
    body: LlmToggle, request: Request, principal: Integrations, session: SessionDep
) -> dict[str, Any]:
    by = principal.user.personnel_number if principal.user else None
    await set_flag(session, GRAPH_LLM_ENABLED, body.enabled, by)
    queued = await extract.backfill_tick(session, limit=100_000) if body.enabled else 0
    session.add(_audit(principal, request, "graph.llm_toggle", enabled=body.enabled))
    await session.commit()
    return {"enabled": body.enabled, "queued_documents": queued}


@router.post("/api/admin/graph/estimate", status_code=202)
async def start_estimate(
    body: EstimateIn, request: Request, principal: Integrations, session: SessionDep
) -> dict[str, Any]:
    actor = principal.user.personnel_number if principal.user else None
    job = await queue.enqueue(
        session,
        ESTIMATE_JOB,
        {"sample": body.sample, "actor": actor},
        priority=3,
        dedupe_key="graph_estimate",
        max_attempts=1,
    )
    session.add(_audit(principal, request, "graph.estimate_requested", sample=body.sample))
    await session.commit()
    return {"queued": job is not None}


@router.post("/api/admin/graph/rebuild-links")
async def rebuild_links(
    request: Request, principal: Integrations, session: SessionDep
) -> dict[str, int]:
    n = await rules.rebuild_all(session)
    session.add(_audit(principal, request, "graph.rebuild_links", documents=n))
    await session.commit()
    return {"queued_documents": n}


@router.post("/api/admin/graph/retry-errors")
async def retry_errors(
    request: Request, principal: Integrations, session: SessionDep
) -> dict[str, int]:
    result = await session.execute(
        update(Chunk).where(Chunk.graph_state == "error").values(graph_state=None, graph_note=None)
    )
    n = result.rowcount or 0
    session.add(_audit(principal, request, "graph.retry_errors", chunks=n))
    await session.commit()
    return {"chunks": n}
