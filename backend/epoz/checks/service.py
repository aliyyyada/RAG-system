"""Проверка пакета: создание, сериализация, удаление и очистка по сессии и простою.

Данные проверки живут, пока жива сессия входа пользователя (и не дольше простоя
`CHECK_IDLE_MINUTES`). Очистка идёт тремя путями: при выходе (`delete_user_runs`), по тику worker'а
(`cleanup_expired`: сессия закрыта или истекла, простой) и кнопкой «Очистить». Удаление проверки
стирает всё: строки проверки и файлов, документы пакета с фрагментами (каскадом) и папку с
загруженными файлами на диске.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.flags import get_flag
from app.models import AuthSession
from epoz import loader
from epoz.checks.criteria_data import CRITERIA
from epoz.checks.roles import ROLES
from epoz.models import CheckCriterion, CheckFile, CheckResult, CheckRun


def run_dir(settings: Settings, run_id: int) -> Path:
    return Path(settings.documents_dir) / "checks" / str(run_id)


async def ensure_criteria(session: AsyncSession) -> list[CheckCriterion]:
    """Список критериев в БД: добавляет недостающие из `criteria_data` (идемпотентно)."""
    rows = list(await session.scalars(select(CheckCriterion).order_by(CheckCriterion.ord)))
    have = {r.code for r in rows}
    added = False
    for i, c in enumerate(CRITERIA):
        if c["code"] not in have:
            session.add(CheckCriterion(ord=i, **c))
            added = True
    if added:
        await session.flush()
        rows = list(await session.scalars(select(CheckCriterion).order_by(CheckCriterion.ord)))
    return rows


async def customer_group(session: AsyncSession) -> str | None:
    value = await get_flag(session, loader.CUSTOMER_GROUP_FLAG, None)
    return value if value in ("I", "II") else None


async def delete_run(session: AsyncSession, settings: Settings, run_id: int) -> None:
    """Стирает проверку целиком: БД (каскадом документы пакета) и папку с файлами."""
    await session.execute(delete(CheckRun).where(CheckRun.id == run_id))
    shutil.rmtree(run_dir(settings, run_id), ignore_errors=True)
    # Задача в очереди, если она ещё не началась, больше не нужна.
    await session.execute(
        text(
            "UPDATE jobs SET status='failed', last_error='проверка удалена', finished_at=now() "
            "WHERE dedupe_key = :k AND status = 'queued'"
        ),
        {"k": f"check:{run_id}"},
    )


async def delete_user_runs(
    session: AsyncSession, settings: Settings, user_id: int, session_id: Any = None
) -> int:
    """При выходе: удаляет проверки пользователя (всей сессии или все его проверки)."""
    cond = [CheckRun.user_id == user_id]
    if session_id is not None:
        cond.append(CheckRun.session_id == session_id)
    ids = list(await session.scalars(select(CheckRun.id).where(*cond)))
    for rid in ids:
        await delete_run(session, settings, rid)
    return len(ids)


async def cleanup_expired(session: AsyncSession, settings: Settings) -> int:
    """Тик worker'а: удаляет проверки закрытых и истёкших сессий и простаивающие проверки."""
    now = datetime.now(UTC)
    idle_before = now - timedelta(minutes=settings.check_idle_minutes)
    dead_sessions = select(AuthSession.id).where(
        (AuthSession.revoked_at.is_not(None)) | (AuthSession.expires_at <= now)
    )
    ids = list(
        await session.scalars(
            select(CheckRun.id).where(
                (CheckRun.session_id.in_(dead_sessions))
                | (CheckRun.session_id.is_(None))
                | (CheckRun.last_activity_at < idle_before)
            )
        )
    )
    for rid in ids:
        await delete_run(session, settings, rid)
    # Папки без проверки (на случай сбоя посреди удаления).
    base = Path(settings.documents_dir) / "checks"
    if base.exists():
        alive = set(map(str, await session.scalars(select(CheckRun.id))))
        for p in base.iterdir():
            if p.is_dir() and p.name not in alive:
                shutil.rmtree(p, ignore_errors=True)
    return len(ids)


async def touch(session: AsyncSession, run_id: int) -> None:
    await session.execute(
        update(CheckRun).where(CheckRun.id == run_id).values(last_activity_at=func.now())
    )


def _file_summary(cf: CheckFile) -> dict[str, Any]:
    ex = cf.extracted or {}
    out = {
        k: ex[k]
        for k in ("supplier", "inn", "date", "number", "total", "price", "okpd2", "subject")
        if ex.get(k)
    }
    if ex.get("items"):
        out["items"] = len(ex["items"])
    return out


async def serialize_run(session: AsyncSession, run: CheckRun) -> dict[str, Any]:
    criteria = await ensure_criteria(session)
    files = list(
        await session.scalars(
            select(CheckFile).where(CheckFile.run_id == run.id).order_by(CheckFile.ord)
        )
    )
    results = {
        r.criterion_id: r
        for r in await session.scalars(select(CheckResult).where(CheckResult.run_id == run.id))
    }
    out_results: list[dict[str, Any]] = []
    counts = {"ok": 0, "fail": 0, "warn": 0, "manual": 0, "pending": 0}
    for c in criteria:
        if not c.enabled:
            continue
        r = results.get(c.id)
        counts[r.status if r else "pending"] += 1
        d = (r.details or {}) if r else {}
        out_results.append(
            {
                "code": c.code,
                "block": c.block,
                "block_title": c.block_title,
                "title": c.title,
                "description": c.description,
                "status": r.status if r else None,
                "summary": r.summary if r else "",
                "items": d.get("items", []),
                "quotes": d.get("quotes", []),
                "epoz": d.get("epoz", []),
                "values": d.get("values", {}),
                "model": r.model if r else None,
                "ms": r.ms if r else None,
            }
        )
    edition = await loader.get_active(session)
    return {
        "id": run.id,
        "status": run.status,
        "created_at": run.created_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "error": run.error,
        "progress": run.progress or {},
        "params": run.params or {},
        "epoz": {
            "edition_id": run.epoz_edition_id,
            "title": edition.title if edition and edition.id == run.epoz_edition_id else None,
        },
        "files": [
            {
                "id": f.id,
                "name": f.name,
                "role": f.role,
                "role_label": ROLES.get(f.role, f.role),
                "role_detected": f.role_detected,
                "role_detected_label": ROLES.get(f.role_detected or "", None),
                "is_winner": f.is_winner,
                "parse_status": f.parse_status,
                "parse_error": f.parse_error,
                "pages": f.pages,
                "ocr_pages": f.ocr_pages,
                "data": _file_summary(f),
            }
            for f in files
        ],
        "results": out_results,
        "counts": counts,
    }
