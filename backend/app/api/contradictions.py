"""Противоречия между документами (этап 6).

Для пользователя (право `contradictions.view`):
    GET  /api/contradictions              — список: вкладки «Новые / Подтверждённые /
                                            Отклонённые», фильтры по теме и документу;
    GET  /api/contradictions/count        — сколько новых активных (счётчик в шапке);
    GET  /api/contradictions/{id}         — одна пара;
    POST /api/contradictions/{id}/review  — «Подтвердить» / «Отклонить» (право
                                            `contradictions.manage`, пишется в аудит).
Для администратора (`admin.integrations`):
    GET  /api/admin/contradictions               — прогресс, прогноз, счётчики;
    POST /api/admin/contradictions/enabled       — включить / выключить поиск;
    POST /api/admin/contradictions/rescan        — пройти все документы заново (кэш);
    POST /api/admin/contradictions/retry-errors  — повторить пары с ошибкой модели.

**Права.** Пара видна, только если человеку доступны ОБА документа: условие
`visible(access)` для document_a и document_b прямо в SQL. Пример: T-0003 видит приказ
№ 112, но не приказ № 140 — пары «112 ↔ 140» для него нет ни в списке, ни в счётчике, ни
по прямой ссылке (404), и «Подтвердить» по ней тоже 404.

**Активность.** Противоречие не активно, если один из документов отменён («Отменён» по
связям или по данным источника) или документы связаны «отменяет / изменяет» (приказ
№ 140 изменяет п. 4.2 приказа № 112 — это изменение, а не противоречие). По умолчанию
список показывает только активные; неактивные — с пометкой «почему».
"""

from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, and_, case, exists, func, or_, select, text
from sqlalchemy.orm import aliased

from app.access.filters import visible
from app.access.service import DocumentAccess, resolve_access
from app.auth import permissions as perms
from app.auth.deps import SessionDep, SettingsDep, client_ip, require_permission
from app.auth.rbac import Principal
from app.contradictions import scan
from app.contradictions.candidates import prepare
from app.contradictions.judge import VERDICT_LABELS
from app.core.flags import CONTRA_ENABLED, get_flag, set_flag
from app.graph.references import KIND_LABELS
from app.graph.search import caption_of
from app.graph.status import CANCELLED
from app.models import (
    AuditLog,
    Chunk,
    Contradiction,
    ContradictionCheck,
    Document,
    DocumentLink,
    Job,
    User,
)
from app.search.hybrid import chunk_place

router = APIRouter(tags=["противоречия"])

Viewer = Annotated[Principal, Depends(require_permission(perms.CONTRADICTIONS_VIEW))]
Manager = Annotated[Principal, Depends(require_permission(perms.CONTRADICTIONS_MANAGE))]
Integrations = Annotated[Principal, Depends(require_permission(perms.ADMIN_INTEGRATIONS))]

REVIEW_LABELS = {"new": "Новое", "confirmed": "Подтверждено", "rejected": "Отклонено"}
# Оценка до замера: пара ≈ 900 токенов промпта (~2 с) + ≈ 120 токенов ответа (~4 с).
DEFAULT_SECONDS_PER_PAIR = 6.0
DEFAULT_PAIRS_PER_DOCUMENT = 6.0


def _err(code: int, key: str, message: str) -> HTTPException:
    return HTTPException(status_code=code, detail={"code": key, "message": message})


# ---------------------------------------------------------------------------
# Схемы
# ---------------------------------------------------------------------------


class SideDocument(BaseModel):
    id: int
    caption: str
    legal_status: str | None
    doc_date: date | None


class SideOut(BaseModel):
    document: SideDocument
    chunk_id: int
    place: str
    # Отрывок фрагмента вокруг цитаты; цитату интерфейс подсвечивает (<mark>).
    excerpt: str
    quote: str


class ContradictionOut(BaseModel):
    id: int
    verdict: str
    verdict_label: str
    topic: str
    explanation: str
    found_at: datetime
    origin: str
    similarity: float | None
    review_status: str
    review_label: str
    reviewed_by: str | None
    reviewed_at: datetime | None
    review_note: str | None
    active: bool
    inactive_reason: str | None
    # Сначала более новый документ.
    sides: list[SideOut]


class UnavailableOut(BaseModel):
    id: int
    name: str
    message: str


class ContradictionList(BaseModel):
    items: list[ContradictionOut]
    total: int
    page: int
    page_size: int
    counts: dict[str, int]
    topics: list[str]
    unavailable_sources: list[UnavailableOut]


class ReviewIn(BaseModel):
    status: Literal["new", "confirmed", "rejected"]
    note: str | None = Field(default=None, max_length=1000)


# ---------------------------------------------------------------------------
# Запросы с правами
# ---------------------------------------------------------------------------

DocA = aliased(Document)
DocB = aliased(Document)


def _base(access: DocumentAccess) -> Any:
    """Пары, где человеку доступны оба документа. Возвращает select(C, check, A, B)."""
    visible_ids = select(Document.id).where(visible(access))
    return (
        select(Contradiction, ContradictionCheck, DocA, DocB)
        .join(ContradictionCheck, ContradictionCheck.id == Contradiction.check_id)
        .join(DocA, DocA.id == Contradiction.document_a_id)
        .join(DocB, DocB.id == Contradiction.document_b_id)
        .where(
            Contradiction.document_a_id.in_(visible_ids),
            Contradiction.document_b_id.in_(visible_ids),
        )
    )


def _resolving_link() -> ColumnElement[bool]:
    """Один документ пары отменяет или изменяет другой."""
    return exists().where(
        DocumentLink.kind.in_(("cancels", "amends")),
        or_(
            and_(
                DocumentLink.from_document_id == Contradiction.document_a_id,
                DocumentLink.target_document_id == Contradiction.document_b_id,
            ),
            and_(
                DocumentLink.from_document_id == Contradiction.document_b_id,
                DocumentLink.target_document_id == Contradiction.document_a_id,
            ),
        ),
    )


def _active() -> ColumnElement[bool]:
    return and_(
        DocA.legal_status.is_distinct_from(CANCELLED),
        DocB.legal_status.is_distinct_from(CANCELLED),
        ~_resolving_link(),
    )


def _unavailable(access: DocumentAccess) -> list[UnavailableOut]:
    return [
        UnavailableOut(
            id=u.id, name=u.name, message=f"Документы из системы «{u.name}» сейчас недоступны"
        )
        for u in access.unavailable
    ]


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(line: str) -> bool:
    return bool(line.strip()) and set(line.replace("|", "").strip()) <= set("-: ")


def _table_row(lines: list[str], i: int) -> str:
    """Строка таблицы «читаемо»: «Группа: 1 · Кто: Работники … · Периодичность: 1 раз в 3 года»,
    перед ней — ближайший текст над таблицей («4.4. Периодичность обучения по группам…»)."""
    j = i
    while j > 0 and lines[j - 1].lstrip().startswith("|"):
        j -= 1
    header = _cells(lines[j]) if j + 1 < len(lines) and _is_separator(lines[j + 1]) else []
    cells = _cells(lines[i])
    row = (
        " · ".join(f"{h}: {c}" for h, c in zip(header, cells, strict=False) if c)
        if header and j != i
        else " · ".join(c for c in cells if c)
    )
    above = next(
        (
            ln.strip()
            for ln in reversed(lines[:j])
            if ln.strip() and not ln.lstrip().startswith("|")
        ),
        "",
    )
    return f"{above} {row}".strip() if above and len(above) <= 160 else row


def excerpt(body: str, quote: str, width: int = 360) -> str:
    """Отрывок фрагмента вокруг цитаты: строка таблицы с названиями столбцов или
    предложение (без разметки Markdown — её в карточке противоречия не рендерим)."""
    lines = [ln for ln in prepare(body).splitlines() if ln.strip()]
    hit = next((i for i, ln in enumerate(lines) if quote and quote.lower() in ln.lower()), None)
    if hit is not None and lines[hit].lstrip().startswith("|"):
        return _table_row(lines, hit)
    flat = " ".join(
        " · ".join(c for c in _cells(ln) if c) if ln.lstrip().startswith("|") else ln.lstrip("# ")
        for ln in lines
        if not _is_separator(ln)
    )
    flat = " ".join(flat.split())
    if len(flat) <= width:
        return flat
    pos = flat.lower().find(quote.lower()) if quote else -1
    if pos < 0:
        return flat[: width - 1].rstrip() + "…"
    start = max(0, pos - (width - len(quote)) // 2)
    end = min(len(flat), start + width)
    start = max(0, end - width)
    return ("…" if start else "") + flat[start:end].strip() + ("…" if end < len(flat) else "")


async def _reason(session: SessionDep, a: Document, b: Document) -> str | None:
    for d in (a, b):
        if d.legal_status == CANCELLED:
            return f"{caption_of(d)} отменён — противоречие с ним не действует"
    link = await session.scalar(
        select(DocumentLink)
        .where(
            DocumentLink.kind.in_(("cancels", "amends")),
            or_(
                and_(
                    DocumentLink.from_document_id == a.id,
                    DocumentLink.target_document_id == b.id,
                ),
                and_(
                    DocumentLink.from_document_id == b.id,
                    DocumentLink.target_document_id == a.id,
                ),
            ),
        )
        .limit(1)
    )
    if link is None:
        return None
    src, tgt = (a, b) if link.from_document_id == a.id else (b, a)
    return f"{caption_of(src)} {KIND_LABELS[link.kind]} {caption_of(tgt)}"


async def _out(
    session: SessionDep,
    c: Contradiction,
    check: ContradictionCheck,
    a: Document,
    b: Document,
    names: dict[int, str],
) -> ContradictionOut:
    chunks = {
        ch.id: ch
        for ch in (
            await session.scalars(select(Chunk).where(Chunk.id.in_([c.chunk_a_id, c.chunk_b_id])))
        ).all()
    }
    sides = []
    for doc, chunk_id, quote in (
        (a, c.chunk_a_id, check.quote_a),
        (b, c.chunk_b_id, check.quote_b),
    ):
        ch = chunks[chunk_id]
        sides.append(
            SideOut(
                document=SideDocument(
                    id=doc.id,
                    caption=caption_of(doc),
                    legal_status=doc.legal_status,
                    doc_date=doc.doc_date,
                ),
                chunk_id=chunk_id,
                place=chunk_place(ch.kind, ch.page_from, ch.page_to, ch.section, ch.point),
                excerpt=excerpt(ch.text, quote),
                quote=quote,
            )
        )
    sides.sort(key=lambda s: s.document.doc_date or date.min, reverse=True)
    reason = await _reason(session, a, b)
    return ContradictionOut(
        id=c.id,
        verdict=check.verdict,
        verdict_label=VERDICT_LABELS.get(check.verdict, check.verdict),
        topic=check.topic or "Расхождение требований",
        explanation=check.explanation,
        found_at=c.found_at,
        origin=c.origin,
        similarity=c.similarity,
        review_status=check.review_status,
        review_label=REVIEW_LABELS.get(check.review_status, check.review_status),
        reviewed_by=names.get(check.reviewed_by) if check.reviewed_by else None,
        reviewed_at=check.reviewed_at,
        review_note=check.review_note,
        active=reason is None,
        inactive_reason=reason,
        sides=sides,
    )


async def _names(session: SessionDep, checks: list[ContradictionCheck]) -> dict[int, str]:
    ids = {ch.reviewed_by for ch in checks if ch.reviewed_by}
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(User.id, User.full_name, User.personnel_number).where(User.id.in_(ids))
        )
    ).all()
    return {uid: name or pn for uid, name, pn in rows}


async def _get_visible(
    session: SessionDep, access: DocumentAccess, contradiction_id: int
) -> tuple[Contradiction, ContradictionCheck, Document, Document]:
    row = (await session.execute(_base(access).where(Contradiction.id == contradiction_id))).first()
    if row is None:
        # Нет такой пары или один из документов недоступен — для человека её нет.
        raise _err(404, "not_found", "Противоречие не найдено")
    return row[0], row[1], row[2], row[3]


# ---------------------------------------------------------------------------
# Пользователь
# ---------------------------------------------------------------------------


@router.get("/api/contradictions", response_model=ContradictionList)
async def list_contradictions(
    principal: Viewer,
    session: SessionDep,
    settings: SettingsDep,
    status: Literal["new", "confirmed", "rejected"] = "new",
    active: Literal["active", "inactive", "all"] = "active",
    topic: str | None = Query(default=None, max_length=200),
    document_id: int | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> ContradictionList:
    access = await resolve_access(session, principal, settings)
    base = _base(access)
    if active == "active":
        base = base.where(_active())
    elif active == "inactive":
        base = base.where(~_active())
    if topic:
        base = base.where(or_(DocA.topic == topic, DocB.topic == topic))
    if document_id is not None:
        base = base.where(
            or_(
                Contradiction.document_a_id == document_id,
                Contradiction.document_b_id == document_id,
            )
        )
    sub = base.subquery()
    counts = dict(
        (
            await session.execute(
                select(sub.c.review_status, func.count()).group_by(sub.c.review_status)
            )
        ).all()
    )
    topics_rows = (
        await session.execute(_base(access).with_only_columns(DocA.topic, DocB.topic).distinct())
    ).all()
    topics = sorted({t for row in topics_rows for t in row if t})
    stmt = (
        base.where(ContradictionCheck.review_status == status)
        .order_by(
            case((ContradictionCheck.verdict == "contradiction", 0), else_=1),
            Contradiction.found_at.desc(),
            Contradiction.id.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await session.execute(stmt)).all()
    names = await _names(session, [r[1] for r in rows])
    items = [await _out(session, c, ch, a, b, names) for c, ch, a, b in rows]
    return ContradictionList(
        items=items,
        total=counts.get(status, 0),
        page=page,
        page_size=page_size,
        counts={k: counts.get(k, 0) for k in REVIEW_LABELS},
        topics=topics,
        unavailable_sources=_unavailable(access),
    )


@router.get("/api/contradictions/count")
async def count_contradictions(
    principal: Viewer, session: SessionDep, settings: SettingsDep
) -> dict[str, int]:
    """Новые активные — для счётчика в шапке."""
    access = await resolve_access(session, principal, settings)
    sub = (
        _base(access)
        .where(_active(), ContradictionCheck.review_status == "new")
        .with_only_columns(Contradiction.id)
        .subquery()
    )
    n = await session.scalar(select(func.count()).select_from(sub))
    return {"new": n or 0}


@router.get("/api/contradictions/{contradiction_id}", response_model=ContradictionOut)
async def get_contradiction(
    contradiction_id: int, principal: Viewer, session: SessionDep, settings: SettingsDep
) -> ContradictionOut:
    access = await resolve_access(session, principal, settings)
    c, check, a, b = await _get_visible(session, access, contradiction_id)
    return await _out(session, c, check, a, b, await _names(session, [check]))


def _audit(principal: Principal, request: Request, action: str, **details: Any) -> AuditLog:
    u = principal.user
    return AuditLog(
        actor_user_id=u.id if u else None,
        actor_login=u.personnel_number if u else None,
        action=action,
        details=details,
        ip=client_ip(request),
    )


@router.post("/api/contradictions/{contradiction_id}/review", response_model=ContradictionOut)
async def review_contradiction(
    contradiction_id: int,
    body: ReviewIn,
    request: Request,
    principal: Manager,
    session: SessionDep,
    settings: SettingsDep,
) -> ContradictionOut:
    """Решение специалиста. Хранится у проверки пары текстов, поэтому переживает
    переиндексацию документа, если текст фрагментов не изменился."""
    if not principal.has(perms.CONTRADICTIONS_VIEW):
        raise _err(403, "forbidden", "Нет доступа к этому разделу")
    access = await resolve_access(session, principal, settings)
    c, check, a, b = await _get_visible(session, access, contradiction_id)
    check.review_status = body.status
    check.review_note = (body.note or "").strip() or None
    check.reviewed_by = principal.user.id if principal.user else None
    check.reviewed_at = func.now() if body.status != "new" else None
    session.add(
        _audit(
            principal,
            request,
            "contradictions.review",
            contradiction_id=c.id,
            status=body.status,
            documents=[a.id, b.id],
            topic=check.topic,
        )
    )
    await session.commit()
    await session.refresh(check)
    return await _out(session, c, check, a, b, await _names(session, [check]))


# ---------------------------------------------------------------------------
# Администратор
# ---------------------------------------------------------------------------


class Toggle(BaseModel):
    enabled: bool


@router.get("/api/admin/contradictions")
async def contradictions_admin(
    principal: Integrations, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    indexed, done, cancelled = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(Document.contra_version == Document.indexed_version),
                func.count().filter(Document.legal_status == CANCELLED),
            ).where(
                Document.indexed_version.is_not(None),
                Document.deleted_at.is_(None),
                Document.is_excluded.is_(False),
            )
        )
    ).one()
    pending = indexed - done
    verdicts = dict(
        (
            await session.execute(
                select(ContradictionCheck.verdict, func.count()).group_by(
                    ContradictionCheck.verdict
                )
            )
        ).all()
    )
    found = await session.scalar(select(func.count()).select_from(Contradiction)) or 0
    reviews = dict(
        (
            await session.execute(
                select(ContradictionCheck.review_status, func.count())
                .where(ContradictionCheck.verdict.in_(scan.FOUND))
                .group_by(ContradictionCheck.review_status)
            )
        ).all()
    )
    avg_ms = await session.scalar(
        text(
            "SELECT avg(ms) FROM (SELECT ms FROM contradiction_checks WHERE verdict <> 'error' "
            "AND ms IS NOT NULL ORDER BY id DESC LIMIT 300) t"
        )
    )
    scanned_docs, avg_pairs = (
        await session.execute(
            select(func.count(), func.avg(Document.contra_pairs)).where(
                Document.contra_pairs.is_not(None),
                Document.legal_status.is_distinct_from(CANCELLED),
            )
        )
    ).one()
    if avg_ms is not None:
        seconds, basis = float(avg_ms) / 1000, "measured"
    else:
        seconds, basis = DEFAULT_SECONDS_PER_PAIR, "assumption"
    if scanned_docs >= 10 and avg_pairs is not None:
        pairs_per_doc, pairs_basis = float(avg_pairs), "measured"
    else:
        pairs_per_doc, pairs_basis = DEFAULT_PAIRS_PER_DOCUMENT, "assumption"
    # Часть пар уже в кэше (пара двух непроверенных документов проверяется один раз):
    # прогноз сверху, по всем парам непроверенных документов.
    forecast = int(pending * pairs_per_doc * seconds)
    jobs = await session.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.kind == scan.SCAN_JOB, Job.status.in_(["queued", "running"]))
    )
    return {
        "enabled": bool(await get_flag(session, CONTRA_ENABLED, False)),
        "model": settings.llm_model,
        "documents": {"indexed": indexed, "done": done, "pending": pending, "cancelled": cancelled},
        "checks": {v: verdicts.get(v, 0) for v in ("contradiction", "possible", "none", "error")},
        "found": found,
        "reviews": {k: reviews.get(k, 0) for k in REVIEW_LABELS},
        "seconds_per_pair": round(seconds, 1),
        "seconds_basis": basis,
        "pairs_per_document": round(pairs_per_doc, 1),
        "pairs_basis": pairs_basis,
        "forecast_seconds": forecast,
        "forecast_hours": round(forecast / 3600, 1),
        "jobs": jobs or 0,
        "settings": {
            "neighbors": settings.contra_neighbors,
            "min_similarity": settings.contra_min_similarity,
            "graph_neighbors": settings.contra_graph_neighbors,
            "graph_min_similarity": settings.contra_graph_min_similarity,
        },
    }


@router.post("/api/admin/contradictions/enabled")
async def toggle_contradictions(
    body: Toggle, request: Request, principal: Integrations, session: SessionDep
) -> dict[str, Any]:
    by = principal.user.personnel_number if principal.user else None
    await set_flag(session, CONTRA_ENABLED, body.enabled, by)
    queued = await scan.backfill_tick(session, limit=100_000) if body.enabled else 0
    session.add(_audit(principal, request, "contradictions.toggle", enabled=body.enabled))
    await session.commit()
    return {"enabled": body.enabled, "queued_documents": queued}


@router.post("/api/admin/contradictions/rescan")
async def rescan(request: Request, principal: Integrations, session: SessionDep) -> dict[str, int]:
    n = await scan.rescan_all(session)
    session.add(_audit(principal, request, "contradictions.rescan", documents=n))
    await session.commit()
    return {"queued_documents": n}


@router.post("/api/admin/contradictions/retry-errors")
async def retry_errors(
    request: Request, principal: Integrations, session: SessionDep
) -> dict[str, int]:
    n = await scan.retry_errors(session)
    session.add(_audit(principal, request, "contradictions.retry_errors", pairs=n))
    await session.commit()
    return {"pairs": n}
