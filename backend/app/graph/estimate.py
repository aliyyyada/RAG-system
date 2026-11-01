"""Прогноз времени извлечения сущностей моделью — замер на выборке, до полного запуска.

    docker compose exec app python -m app.cli graph-estimate --sample 20
или кнопка «Оценить время» на странице «Граф знаний» (задача worker'а `graph_estimate`).

Что делает:
1. Берёт случайную выборку необработанных фрагментов и считает, какая доля из них пройдёт
   фильтр (app/graph/extract.skip_reason) — остальные модель не увидит.
2. Отправляет `sample` подходящих фрагментов в модель по одному (как настоящий worker,
   уступая очередь пользователям) и меряет время каждого вызова. В граф ничего не пишет.
3. Прогноз = (необработанных фрагментов × доля подходящих) × среднее время на фрагмент.

Пример вывода на GTX 1070 (числа условные, до замера на сервере):
    фрагментов к обработке: 41 200, пройдут фильтр: 63 % (≈ 26 000)
    среднее время на фрагмент: 11,8 с (мин 6,1, макс 19,4), 31 токен/с
    прогноз: ≈ 85 часов работы видеокарты — 3,5 суток без перерыва
Результат пишется в журнал аудита (graph.estimate) и показывается в админке.
"""

import asyncio
import logging
import random
import time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.llm_priority import background_llm_allowed
from app.graph.extract import ENTITY_LABELS, doc_caption, run_extraction, skip_reason, strip_lists
from app.llm.ollama import Ollama, OllamaError
from app.models import AuditLog, Chunk, Document
from app.worker.queue import ClaimedJob

log = logging.getLogger(__name__)

ESTIMATE_JOB = "graph_estimate"
# Сколько ждём, пока пользователи освободят модель, прежде чем сдаться.
MAX_WAIT_SECONDS = 15 * 60


async def _wait_turn(sm: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    waited = 0.0
    while True:
        async with sm() as s:
            conn = await s.connection()
            if await background_llm_allowed(conn, settings.llm_queue_stale_seconds):
                return
        if waited > MAX_WAIT_SECONDS:
            raise TimeoutError("Модель всё время занята пользователями, попробуйте позже")
        await asyncio.sleep(2)
        waited += 2


async def estimate(
    settings: Settings,
    sm: async_sessionmaker[AsyncSession],
    *,
    sample: int = 20,
    actor: str | None = None,
) -> dict[str, Any]:
    async with sm() as s:
        pending = int(
            await s.scalar(
                select(func.count()).select_from(Chunk).where(Chunk.graph_state.is_(None))
            )
            or 0
        )
        total = int(await s.scalar(select(func.count()).select_from(Chunk)) or 0)
        rows = (
            await s.execute(
                select(Chunk.id, Chunk.section, Chunk.text, Document)
                .join(Document, Document.id == Chunk.document_id)
                .where(Chunk.graph_state.is_(None), Document.deleted_at.is_(None))
                .order_by(func.random())
                .limit(2000)
            )
        ).all()
    eligible = []
    for chunk_id, section, text, doc in rows:
        body = strip_lists(text)
        if skip_reason(body, min_chars=settings.graph_min_chunk_chars) is None:
            eligible.append((chunk_id, section, body, doc_caption(doc)))
    share = len(eligible) / len(rows) if rows else 0.0
    random.shuffle(eligible)
    timings: list[float] = []
    tokens_per_second: list[float] = []
    examples: list[dict[str, Any]] = []
    errors = 0
    async with Ollama(settings) as client:
        for chunk_id, section, body, caption in eligible[:sample]:
            await _wait_turn(sm, settings)
            t0 = time.perf_counter()
            try:
                data, stats = await run_extraction(client, settings, caption, section, body)
            except (OllamaError, ValueError) as exc:
                errors += 1
                log.warning("Оценка: фрагмент %s — %s", chunk_id, exc)
                continue
            timings.append(time.perf_counter() - t0)
            if stats.get("eval_count") and stats.get("eval_duration"):
                tokens_per_second.append(stats["eval_count"] / (stats["eval_duration"] / 1e9))
            if len(examples) < 3:
                examples.append(
                    {
                        "chunk_id": chunk_id,
                        "document": caption,
                        "entities": [
                            f"{ENTITY_LABELS.get(e.type, e.type)}: {e.name}" for e in data.entities
                        ][:10],
                        "relations": [
                            f"{r.subject} — {r.predicate} — {r.object}" for r in data.relations
                        ][:10],
                    }
                )
    avg = sum(timings) / len(timings) if timings else None
    to_process = round(pending * share)
    result: dict[str, Any] = {
        "model": settings.llm_model,
        "chunks_total": total,
        "chunks_pending": pending,
        "eligible_share": round(share, 3),
        "chunks_to_process": to_process,
        "sample": len(timings),
        "errors": errors,
        "avg_seconds": round(avg, 2) if avg is not None else None,
        "min_seconds": round(min(timings), 2) if timings else None,
        "max_seconds": round(max(timings), 2) if timings else None,
        "tokens_per_second": round(sum(tokens_per_second) / len(tokens_per_second), 1)
        if tokens_per_second
        else None,
        "forecast_hours": round(to_process * avg / 3600, 1) if avg is not None else None,
        "examples": examples,
    }
    async with sm() as s, s.begin():
        s.add(AuditLog(actor_login=actor, action="graph.estimate", details=result))
    return result


def report(r: dict[str, Any]) -> str:
    """Текст для консоли: python -m app.cli graph-estimate."""
    lines = [
        f"Модель: {r['model']}",
        f"Фрагментов всего: {r['chunks_total']}, ещё не обработано моделью: {r['chunks_pending']}",
        f"Пройдут фильтр: {r['eligible_share'] * 100:.0f} % (≈ {r['chunks_to_process']})",
    ]
    if r["avg_seconds"] is None:
        lines.append("Замер не удался: модель не ответила ни на один фрагмент (см. журнал).")
        return "\n".join(lines)
    lines.append(
        f"Замер на {r['sample']} фрагментах: в среднем {r['avg_seconds']} с "
        f"(мин {r['min_seconds']}, макс {r['max_seconds']})"
        + (f", {r['tokens_per_second']} токенов/с" if r["tokens_per_second"] else "")
    )
    hours = r["forecast_hours"] or 0
    lines.append(
        f"Прогноз: ≈ {hours} ч работы видеокарты (≈ {hours / 24:.1f} суток без перерыва; "
        "днём пользователи в приоритете, поэтому по календарю дольше)"
    )
    for ex in r["examples"]:
        lines.append(f"\nПример — {ex['document']}:")
        lines += [f"  • {e}" for e in ex["entities"]]
        lines += [f"  → {x}" for x in ex["relations"]]
    return "\n".join(lines)


async def handle_estimate(
    job: ClaimedJob, settings: Settings, sm: async_sessionmaker[AsyncSession]
) -> None:
    await estimate(
        settings,
        sm,
        sample=int(job.payload.get("sample", 20)),
        actor=job.payload.get("actor"),
    )
