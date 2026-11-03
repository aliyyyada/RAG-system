"""Редакции ЕПоЗ: загрузка файлов, черновик, отчёт, активация, группа доступа.

Как это работает:

1. Администратор создаёт **черновик**. Он наследует все части действующей редакции
   (те же документы), поэтому заменить можно один файл, а не все 15.
2. Каждый загруженный файл (Word) разбирается `docx_parse.parse_epoz_docx`: восстанавливается
   нумерация, текст режется на фрагменты (дальше — векторизация обычным worker'ом), пункты
   с номерами сохраняются в `epoz_clauses`. Файл становится **отдельным документом** в
   коллекции «ЕПоЗ»: до активации ни у кого нет прав на него, в поиск он не попадает.
3. По черновику строится **отчёт**: какие части на месте, как восстановлена нумерация
   (сколько заголовков сопоставлено с оглавлением, какая доля ссылок находит пункт),
   находятся ли пункты, на которые ссылаются критерии проверки.
4. **Активация**: черновик становится действующей редакцией, прежняя — архивной; все документы
   редакции помечаются защищёнными (их нельзя удалить, исключить из поиска, переразобрать
   общим способом — проверяет API, а в БД стоит триггер); одна группа получает доступ ко всему
   комплекту. Дальше ЕПоЗ работает в обычном поиске и чате по тем же правилам, что остальные
   документы: виден только тем, у кого есть доступ.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import write_audit
from app.config import Settings
from app.core.flags import get_flag, set_flag
from app.ingest.chunker import chunk_blocks
from app.ingest.files import MIME, detect_type
from app.ingest.pipeline import add_event
from app.ingest.storage import FileStore
from app.models import Chunk, Collection, Document, DocumentFile, DocumentGrant, Group
from app.search.indexer import enqueue_embedding
from epoz.docx_parse import ParsedEpoz, parse_epoz_docx
from epoz.models import EpozClause, EpozEdition, EpozPart

EPOZ_GROUPS_FLAG = "epoz_groups"
CUSTOMER_GROUP_FLAG = "customer_group"
COLLECTION_NAME = "ЕПоЗ"

PARTS: dict[str, str] = {
    "main": "Единое положение о закупке (ЕПоЗ)",
    "app1": "ЕПоЗ. Приложение 1. Порядок работы закупочных комиссий",
    "app2": "ЕПоЗ. Приложение 2. Условия применения способов закупки",
    "app3": "ЕПоЗ. Приложение 3. Применимость формы закупки",
    "app4": "ЕПоЗ. Приложение 4. Применимость дополнительных элементов закупки",
    "app5": "ЕПоЗ. Приложение 5. Порядок определения и обоснования НМЦ",
    "app6": "ЕПоЗ. Приложение 6. Оценка и сопоставление заявок",
    "app7": "ЕПоЗ. Приложение 7. Критерии аккредитации поставщиков",
    "app8": "ЕПоЗ. Приложение 8. Перечень продукции для квалификационного отбора для серии закупок",
    "app9": "ЕПоЗ. Приложение 9. Комиссия по рассмотрению жалоб",
    "app10": "ЕПоЗ. Приложение 10. Требования к гарантам и банкам",
    "app11": "ЕПоЗ. Приложение 11. Критерии к поставщикам финансовых услуг",
    "app12": "ЕПоЗ. Приложение 12. Порядок приглашения поставщиков в закрытых закупках",
    "app13": "ЕПоЗ. Приложение 13. Перечень, составляющий государственную тайну",
    "app14": "ЕПоЗ. Приложение 14. Перечень продукции с иными сроками оплаты",
}

# Пункты, на которые ссылаются критерии проверки: без них проверка по ним невозможна.
REQUIRED_REFS: tuple[tuple[str, str], ...] = (
    ("main", "10.3"),
    ("main", "10.8"),
    ("app5", "6.5"),
    ("app5", "6.6"),
    ("app5", "6.9"),
    ("app5", "6.10"),
    ("app5", "6.13"),
    ("app5", "6.15"),
)

IGNORED = "ignored"


class LoaderError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def detect_part(filename: str) -> str | None:
    """Какая часть комплекта этот файл: «Приложение 5_Определение НМЦ.docx» → app5.

    Возвращает "ignored" для «Перечня изменений» (его не загружаем: Положение уже в новой
    редакции), None — если часть определить нельзя."""
    name = filename.lower().replace("ё", "е")
    if "перечень изменений" in name or name.startswith("прил.2 к прот"):
        return IGNORED
    m = re.search(r"приложение\s*(?:№\s*)?(\d{1,2})", name)
    if m:
        n = int(m.group(1))
        return f"app{n}" if 1 <= n <= 14 else None
    if "епоз" in name or "единое положение" in name or "положение о закупк" in name:
        return "main"
    return None


# ---------------------------------------------------------------------------
# Редакции
# ---------------------------------------------------------------------------


async def get_active(session: AsyncSession) -> EpozEdition | None:
    return await session.scalar(select(EpozEdition).where(EpozEdition.status == "active"))


async def get_draft(session: AsyncSession) -> EpozEdition | None:
    return await session.scalar(select(EpozEdition).where(EpozEdition.status == "draft"))


async def create_draft(session: AsyncSession, by: str | None) -> EpozEdition:
    """Черновик (один на всех). Наследует части действующей редакции."""
    draft = await get_draft(session)
    if draft is not None:
        return draft
    draft = EpozEdition(status="draft", created_by=by, title="Черновик редакции ЕПоЗ")
    session.add(draft)
    await session.flush()
    active = await get_active(session)
    if active is not None:
        for p in await session.scalars(select(EpozPart).where(EpozPart.edition_id == active.id)):
            session.add(
                EpozPart(
                    edition_id=draft.id,
                    part_key=p.part_key,
                    document_id=p.document_id,
                    file_name=p.file_name,
                    uploaded_by=p.uploaded_by,
                    uploaded_at=p.uploaded_at,
                    report=p.report,
                    inherited=True,
                )
            )
    return draft


async def _collection(session: AsyncSession) -> Collection:
    coll = await session.scalar(select(Collection).where(Collection.name == COLLECTION_NAME))
    if coll is None:
        coll = Collection(
            name=COLLECTION_NAME,
            kind="topic",
            description="Единое положение о закупке и приложения",
        )
        session.add(coll)
        await session.flush()
    return coll


def _quality(part_key: str, rep: dict[str, Any]) -> tuple[str, list[str]]:
    """Статус разбора части: ok / warning / error и пояснения для администратора."""
    msgs: list[str] = []
    status = "ok"
    if rep.get("clauses", 0) == 0:
        return "error", ["В файле не найден текст"]
    if part_key == "main":
        h, hm = rep.get("headings", 0), rep.get("headings_matched", 0)
        if rep.get("method") != "toc":
            status = "error"
            msgs.append(
                "Не найдено оглавление со стилями «[Ростех] … (Уровень N)»: нумерацию пунктов восстановить нельзя"
            )
        elif h and hm / h < 0.95:
            status = "error"
            msgs.append(
                f"С оглавлением сопоставлено только {hm} из {h} заголовков: нумерация пунктов ненадёжна"
            )
        rate = rep.get("refs_rate")
        if rate is not None and rate < 0.9:
            status = "error"
            msgs.append(
                f"Только {round(rate * 100)}% ссылок «п. N.N» находят пункт: нумерация, вероятно, сдвинута"
            )
    else:
        rate = rep.get("refs_rate")
        if rate is not None and rep.get("refs_total", 0) >= 5 and rate < 0.5:
            status = "warning"
            msgs.append(
                f"{round(rate * 100)}% внутренних ссылок находят пункт (часть ссылок ведёт в Положение — это нормально)"
            )
    return status, msgs


async def add_part_file(
    session: AsyncSession,
    settings: Settings,
    draft: EpozEdition,
    part_key: str,
    path: Path,
    file_name: str,
    by: str | None,
) -> EpozPart:
    """Разбирает файл Word и ставит его в черновик как часть `part_key`."""
    if part_key not in PARTS:
        raise LoaderError("unknown_part", f"Неизвестная часть комплекта: {part_key}")
    if draft.status != "draft":
        raise LoaderError("not_draft", "Файлы можно загружать только в черновик")
    kind = await asyncio.to_thread(detect_type, path)
    if kind != "docx":
        raise LoaderError("bad_type", "ЕПоЗ загружается в формате Word (.docx)")
    try:
        parsed: ParsedEpoz = await asyncio.to_thread(parse_epoz_docx, path)
    except Exception as exc:  # noqa: BLE001
        message = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
        raise LoaderError("parse_error", f"Файл не удалось разобрать: {message}"[:500]) from exc

    data = await asyncio.to_thread(path.read_bytes)
    sha = hashlib.sha256(data).hexdigest()
    store = FileStore(settings)
    tmp = store.tmp_path()
    await asyncio.to_thread(tmp.write_bytes, data)
    store.commit(tmp, sha)

    now = datetime.now(UTC)
    coll = await _collection(session)
    doc = Document(
        collection_id=coll.id,
        title=PARTS[part_key],
        epoz_part=part_key,
        header={"epoz_part": part_key, "file": file_name},
        status="ready",
        version=now,
        indexed_version=now,
        # Фоновые «досылки» графа и противоречий ЕПоЗ не трогают (см. их backfill_tick).
        links_version=now,
        contra_version=now,
    )
    session.add(doc)
    await session.flush()
    file_row = DocumentFile(
        document_id=doc.id,
        ord=0,
        name=file_name[:500],
        mime=MIME["docx"],
        size=len(data),
        sha256=sha,
        pages=None,
        ocr_pages=0,
        warnings=[],
        text=parsed.text,
    )
    session.add(file_row)
    await session.flush()

    drafts = chunk_blocks(
        parsed.blocks, target=settings.chunk_target_chars, maximum=settings.chunk_max_chars
    )
    for i, c in enumerate(drafts):
        session.add(
            Chunk(
                document_id=doc.id,
                file_id=file_row.id,
                ord=i,
                kind=c.kind,
                text=c.text,
                section=c.section,
                point=c.point,
                page_from=None,
                page_to=None,
                token_estimate=c.token_estimate,
                graph_state="skipped",
                graph_note="ЕПоЗ",
            )
        )
    doc.chunk_count = len(drafts)
    for i, cl in enumerate(parsed.clauses):
        session.add(
            EpozClause(
                document_id=doc.id,
                ord=i,
                label=cl.label[:64],
                level=cl.level,
                is_heading=cl.is_heading,
                text=cl.text,
            )
        )
    await session.flush()
    await enqueue_embedding(session, doc.id)

    report = dict(parsed.report)
    report["chunks"] = len(drafts)
    report["sha256"] = sha
    status, msgs = _quality(part_key, report)
    report["status"] = status
    report["messages"] = msgs
    await add_event(session, doc.id, "uploaded", actor=by, file=file_name, part=part_key)

    # Прежняя часть черновика: убираем документ, если он создан только для черновика.
    old = await session.scalar(
        select(EpozPart).where(EpozPart.edition_id == draft.id, EpozPart.part_key == part_key)
    )
    old_doc_id = old.document_id if old is not None else None
    if old is not None:
        await session.delete(old)
        await session.flush()
    part = EpozPart(
        edition_id=draft.id,
        part_key=part_key,
        document_id=doc.id,
        file_name=file_name[:500],
        uploaded_by=by,
        report=report,
        inherited=False,
    )
    session.add(part)
    await session.flush()
    if old_doc_id is not None:
        await _drop_unused_document(session, old_doc_id)
    return part


async def _drop_unused_document(session: AsyncSession, document_id: int) -> None:
    """Удаляет документ части, если он не защищён и ни одна редакция его больше не использует."""
    protected = await session.scalar(
        select(Document.is_protected).where(Document.id == document_id)
    )
    if protected:
        return
    used = await session.scalar(
        select(func.count()).select_from(EpozPart).where(EpozPart.document_id == document_id)
    )
    if not used:
        await session.execute(delete(Document).where(Document.id == document_id))


async def cancel_draft(session: AsyncSession) -> bool:
    draft = await get_draft(session)
    if draft is None:
        return False
    doc_ids = list(
        await session.scalars(select(EpozPart.document_id).where(EpozPart.edition_id == draft.id))
    )
    await session.execute(delete(EpozPart).where(EpozPart.edition_id == draft.id))
    await session.delete(draft)
    await session.flush()
    for d in doc_ids:
        await _drop_unused_document(session, d)
    return True


# ---------------------------------------------------------------------------
# Отчёт и активация
# ---------------------------------------------------------------------------


async def clause_exists(session: AsyncSession, document_id: int, label: str) -> bool:
    return bool(
        await session.scalar(
            select(func.count())
            .select_from(EpozClause)
            .where(EpozClause.document_id == document_id, EpozClause.label == label)
        )
    )


async def edition_report(session: AsyncSession, edition: EpozEdition) -> dict[str, Any]:
    parts = {
        p.part_key: p
        for p in await session.scalars(select(EpozPart).where(EpozPart.edition_id == edition.id))
    }
    out_parts: dict[str, Any] = {}
    blockers: list[str] = []
    warnings: list[str] = []
    for key, title in PARTS.items():
        p = parts.get(key)
        if p is None:
            out_parts[key] = {"title": title, "present": False}
            continue
        rep = p.report or {}
        st = rep.get("status", "ok")
        out_parts[key] = {
            "title": title,
            "present": True,
            "document_id": p.document_id,
            "file_name": p.file_name,
            "inherited": p.inherited,
            "uploaded_at": p.uploaded_at.isoformat(),
            "status": st,
            "messages": rep.get("messages", []),
            "chunks": rep.get("chunks"),
            "clauses": rep.get("clauses"),
            "method": rep.get("method"),
            "headings": rep.get("headings"),
            "headings_matched": rep.get("headings_matched"),
            "refs_rate": rep.get("refs_rate"),
        }
        if st == "error":
            blockers.append(f"{title}: " + "; ".join(rep.get("messages", ["ошибка разбора"])))
        elif st == "warning":
            warnings.append(f"{title}: " + "; ".join(rep.get("messages", [])))
    missing = [PARTS[k] for k in PARTS if k not in parts]
    if missing:
        blockers.append("Не загружены части комплекта: " + "; ".join(missing))
    missing_refs: list[str] = []
    for part_key, label in REQUIRED_REFS:
        p = parts.get(part_key)
        if p is None:
            continue
        if not await clause_exists(session, p.document_id, label):
            missing_refs.append(f"{PARTS[part_key]}: п. {label}")
    if missing_refs:
        blockers.append(
            "Не найдены пункты, на которые ссылаются критерии проверки: " + "; ".join(missing_refs)
        )
    return {
        "edition_id": edition.id,
        "status": edition.status,
        "parts": out_parts,
        "missing_parts": missing,
        "missing_refs": missing_refs,
        "blockers": blockers,
        "warnings": warnings,
        "can_activate": not blockers,
    }


async def active_document_ids(session: AsyncSession) -> list[int]:
    active = await get_active(session)
    if active is None:
        return []
    return list(
        await session.scalars(select(EpozPart.document_id).where(EpozPart.edition_id == active.id))
    )


async def get_groups(session: AsyncSession) -> list[int]:
    raw = await get_flag(session, EPOZ_GROUPS_FLAG, [])
    return [int(x) for x in raw] if isinstance(raw, list) else []


async def apply_grants(session: AsyncSession) -> None:
    """Одна группа (или несколько) — доступ ко всему действующему комплекту; к архивным частям
    доступа нет ни у кого. Те же `document_grants`, что у ручных загрузок: видимость считает
    общий фильтр `visible()`."""
    desired = set(await get_groups(session))
    active_docs = set(await active_document_ids(session))
    all_epoz = set(
        await session.scalars(select(Document.id).where(Document.epoz_part.is_not(None)))
    )
    stale = all_epoz - active_docs
    if stale:
        await session.execute(delete(DocumentGrant).where(DocumentGrant.document_id.in_(stale)))
    if not active_docs:
        return
    keep_other = DocumentGrant.group_id.is_(None) | DocumentGrant.group_id.not_in(sorted(desired))
    await session.execute(
        delete(DocumentGrant).where(
            DocumentGrant.document_id.in_(active_docs),
            keep_other if desired else DocumentGrant.document_id.in_(active_docs),
        )
    )
    have = set(
        (d, g)
        for d, g in (
            await session.execute(
                select(DocumentGrant.document_id, DocumentGrant.group_id).where(
                    DocumentGrant.document_id.in_(active_docs), DocumentGrant.group_id.is_not(None)
                )
            )
        ).all()
    )
    for d in active_docs:
        for g in desired:
            if (d, g) not in have:
                session.add(DocumentGrant(document_id=d, group_id=g))


async def set_groups(session: AsyncSession, group_ids: list[int], by: str | None) -> list[int]:
    ids = sorted(set(group_ids))
    known = set(await session.scalars(select(Group.id).where(Group.id.in_(ids)))) if ids else set()
    if missing := [g for g in ids if g not in known]:
        raise LoaderError("unknown_group", f"Нет групп с id {missing}")
    await set_flag(session, EPOZ_GROUPS_FLAG, ids, by)
    await session.flush()
    await apply_grants(session)
    return ids


async def activate(
    session: AsyncSession,
    draft: EpozEdition,
    by: str | None,
    *,
    force: bool = False,
    ip: str | None = None,
    actor: Any = None,
) -> EpozEdition:
    if draft.status != "draft":
        raise LoaderError("not_draft", "Активировать можно только черновик")
    report = await edition_report(session, draft)
    if not report["can_activate"] and not force:
        raise LoaderError("not_ready", "Черновик не готов: " + " | ".join(report["blockers"])[:900])
    previous = await get_active(session)
    if previous is not None:
        previous.status = "archived"
        await session.flush()
    doc_ids = list(
        await session.scalars(select(EpozPart.document_id).where(EpozPart.edition_id == draft.id))
    )
    if doc_ids:
        await session.execute(
            update(Document).where(Document.id.in_(doc_ids)).values(is_protected=True)
        )
    draft.status = "active"
    draft.activated_at = datetime.now(UTC)
    draft.activated_by = by
    draft.title = f"Редакция ЕПоЗ от {draft.activated_at:%d.%m.%Y}"
    await session.flush()
    await apply_grants(session)
    await write_audit(
        session,
        "epoz.activate",
        actor=actor,
        target_type="epoz_edition",
        target_id=draft.id,
        details={"forced": force, "blockers": report["blockers"], "parts": len(doc_ids)},
        ip=ip,
    )
    return draft


# ---------------------------------------------------------------------------
# Пункты
# ---------------------------------------------------------------------------


async def get_clauses(
    session: AsyncSession, part_key: str, label: str, *, edition_id: int | None = None
) -> list[EpozClause]:
    """Пункт `label` и все его подпункты в действующей редакции (или в указанной)."""
    if edition_id is None:
        active = await get_active(session)
        if active is None:
            return []
        edition_id = active.id
    part = await session.scalar(
        select(EpozPart).where(EpozPart.edition_id == edition_id, EpozPart.part_key == part_key)
    )
    if part is None:
        return []
    like_dot = label.replace("%", "").replace("_", "") + ".%"
    like_br = label.replace("%", "").replace("_", "") + "(%"
    rows = await session.scalars(
        select(EpozClause)
        .where(
            EpozClause.document_id == part.document_id,
            (EpozClause.label == label)
            | EpozClause.label.like(like_dot)
            | EpozClause.label.like(like_br),
        )
        .order_by(EpozClause.ord)
    )
    return list(rows)
