"""Задача worker'а `check_run`: разбор файлов пакета, данные из документов, 20 критериев.

Прогресс пишется в `check_runs.progress` после каждого шага, результаты — по одному в
`check_results`: интерфейс показывает итоги по мере готовности. Если пользователь вышел или
нажал «Очистить», проверки в базе уже нет — задача замечает это между шагами и тихо
завершается (ничего не восстанавливает и не оставляет).
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.worker import queue
from app.worker.queue import ClaimedJob
from epoz import loader
from epoz.checks import pkg, service
from epoz.checks.extract import extract_file
from epoz.checks.roles import detect_role
from epoz.checks.rules import RULES, Ctx, Res
from epoz.models import CheckFile, CheckResult, CheckRun

log = logging.getLogger(__name__)

JOB_KIND = "check_run"
# Пользовательская проверка идёт раньше разбора документов из источника (10), но уступает ручной загрузке.
PRIORITY = 12


async def enqueue_run(session: AsyncSession, run_id: int) -> int | None:
    return await queue.enqueue(
        session,
        JOB_KIND,
        {"run_id": run_id},
        PRIORITY,
        dedupe_key=f"check:{run_id}",
        max_attempts=1,
    )


async def _progress(
    sm: async_sessionmaker[AsyncSession], run_id: int, job_id: int, **progress: Any
) -> bool:
    """Записывает прогресс. False — проверки уже нет (удалена) или отменена."""
    async with sm() as s, s.begin():
        run = await s.get(CheckRun, run_id)
        if run is None or run.status == "cancelled":
            return False
        run.progress = {**(run.progress or {}), **progress}
        # Задача долгая: подтверждаем, что worker жив, иначе «зависшие» задачи вернут её в очередь.
        await s.execute(text("UPDATE jobs SET locked_at = now() WHERE id = :i"), {"i": job_id})
    return True


async def handle_check_run(
    job: ClaimedJob, settings: Settings, sm: async_sessionmaker[AsyncSession]
) -> None:
    run_id = int(job.payload["run_id"])
    async with sm() as s, s.begin():
        run = await s.get(CheckRun, run_id)
        if run is None or run.status in ("cancelled", "done"):
            return
        run.status = "running"
        run.started_at = datetime.now(UTC)
        file_ids = list(
            await s.scalars(
                select(CheckFile.id).where(CheckFile.run_id == run_id).order_by(CheckFile.ord)
            )
        )
        criteria = [c for c in await service.ensure_criteria(s) if c.enabled]
        group = (run.params or {}).get("customer_group")
        run.progress = {
            "stage": "parse",
            "done": 0,
            "total": len(file_ids),
            "criteria_total": len(criteria),
        }
    t_start = time.perf_counter()

    # 1. Разбор файлов (текст, таблицы, OCR сканов)
    for i, fid in enumerate(file_ids):
        async with sm() as s, s.begin():
            run = await s.get(CheckRun, run_id)
            cf = await s.get(CheckFile, fid)
            if run is None or cf is None:
                return
            await pkg.ingest_file(s, settings, run, cf)
        if not await _progress(sm, run_id, job.id, stage="parse", done=i + 1, current=None):
            return

    # 2. Данные из документов и роль по содержимому
    async with sm() as s, s.begin():
        run = await s.get(CheckRun, run_id)
        if run is None:
            return
        files = await pkg.load_files(s, run)
        for fd in files:
            fd.role_detected = detect_role(fd.text) if fd.ok else None
            fd.extracted = extract_file(fd)
            await s.execute(
                update(CheckFile)
                .where(CheckFile.id == fd.id)
                .values(role_detected=fd.role_detected, extracted=fd.extracted)
            )
    if not await _progress(sm, run_id, job.id, stage="criteria", done=0):
        return

    # 3. Критерии: каждый в своей транзакции, результат виден сразу
    for i, crit in enumerate(criteria):
        rule = RULES.get(crit.code)
        t0 = time.perf_counter()
        async with sm() as s, s.begin():
            run = await s.get(CheckRun, run_id)
            if run is None or run.status == "cancelled":
                return
            ctx = Ctx(session=s, settings=settings, run=run, files=files, customer_group=group)
            try:
                res: Res = (
                    await rule(ctx)
                    if rule
                    else Res("manual", "Для этого критерия ещё нет правила проверки")
                )
            except Exception as exc:  # noqa: BLE001 — один критерий не должен ломать всю проверку
                log.exception("проверка %s, критерий %s", run_id, crit.code)
                res = Res(
                    "manual",
                    f"Не удалось проверить автоматически ({type(exc).__name__}). Проверьте вручную.",
                )
            s.add(
                CheckResult(
                    run_id=run_id,
                    criterion_id=crit.id,
                    status=res.status,
                    summary=res.summary,
                    details={
                        "items": res.items,
                        "quotes": res.quotes,
                        "epoz": res.epoz,
                        "values": res.values,
                    },
                    model=res.model,
                    ms=res.ms or int((time.perf_counter() - t0) * 1000),
                )
            )
        if not await _progress(
            sm, run_id, job.id, stage="criteria", done=i + 1, current=crit.title
        ):
            return

    # 4. Итог
    async with sm() as s, s.begin():
        run = await s.get(CheckRun, run_id)
        if run is None:
            return
        counts = {"ok": 0, "fail": 0, "warn": 0, "manual": 0}
        for r in await s.scalars(select(CheckResult).where(CheckResult.run_id == run_id)):
            counts[r.status] = counts.get(r.status, 0) + 1
        run.summary = {**counts, "seconds": round(time.perf_counter() - t_start)}
        run.status = "done"
        run.finished_at = datetime.now(UTC)
        run.progress = {**(run.progress or {}), "stage": "done", "current": None}
        ed = await loader.get_active(s)
        run.epoz_edition_id = run.epoz_edition_id or (ed.id if ed else None)
