"""Поиск противоречий в фоне (задача worker'а `contra_scan`, одна на документ).

    1. Кандидаты без модели (app/contradictions/candidates.py): нормативные фрагменты
       документа × похожие фрагменты других документов + связанные графом.
    2. Пара уже проверялась (тот же текст обоих фрагментов) — берём ответ из кэша
       contradiction_checks, модель не трогаем.
    3. Иначе — модель (app/contradictions/judge.py), по одной паре за вызов, и только когда
       никто из пользователей не ждёт ответа (app/core/llm_priority.py).
    4. Противоречие (contradiction / possible) записывается в contradictions с текущими
       фрагментами; отметка documents.contra_version = indexed_version — документ готов.

За заход задача проверяет не больше CONTRA_PAIRS_PER_JOB пар и снова встаёт в очередь
(JobDeferred(0)); прогресс — в кэше, поэтому после перезапуска работа продолжается.

**Выключено, пока администратор не включит** (как извлечение сущностей на этапе 5):
сначала прогноз времени, потом кнопка «Включить поиск противоречий» в админке
(`app_flags.contradictions_enabled`, или `python -m app.cli contra on`).

Приоритеты: новые документы (−1, как граф для новых) раньше архива (−3, после графа).
"""

import logging
import time

from sqlalchemy import delete, exists, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.contradictions.candidates import Candidate, find_candidates
from app.contradictions.judge import judge
from app.core.flags import CONTRA_ENABLED, get_flag
from app.core.llm_priority import background_llm_allowed
from app.graph.extract import doc_caption
from app.graph.status import CANCELLED
from app.llm.ollama import ModelNotFound, Ollama, OllamaError, OllamaUnavailable
from app.models import Chunk, Contradiction, ContradictionCheck, Document
from app.worker import queue
from app.worker.queue import ClaimedJob, JobDeferred

log = logging.getLogger(__name__)

SCAN_JOB = "contra_scan"
PRIORITY_NEW = -1
PRIORITY_BACKLOG = -3
FOUND = ("contradiction", "possible")


async def enabled(session: AsyncSession) -> bool:
    return bool(await get_flag(session, CONTRA_ENABLED, False))


async def enqueue_scan(session: AsyncSession, document_id: int, priority: int) -> int | None:
    return await queue.enqueue(
        session,
        SCAN_JOB,
        {"document_id": document_id},
        priority,
        dedupe_key=f"contra:{document_id}",
        max_attempts=5,
    )


def caption_for_model(doc: Document) -> str:
    status = f" (статус: {doc.legal_status})" if doc.legal_status else ""
    return doc_caption(doc) + status


async def cached(session: AsyncSession, pair: Candidate) -> ContradictionCheck | None:
    return await session.scalar(
        select(ContradictionCheck).where(
            ContradictionCheck.key_a == pair.key_a, ContradictionCheck.key_b == pair.key_b
        )
    )


async def link(session: AsyncSession, check: ContradictionCheck, pair: Candidate) -> None:
    """Пара фрагментов с противоречием → строка contradictions (повтор не дублирует)."""
    if check.verdict not in FOUND:
        return
    stmt = insert(Contradiction).values(
        check_id=check.id,
        chunk_a_id=pair.chunk_a,
        chunk_b_id=pair.chunk_b,
        document_a_id=pair.document_a,
        document_b_id=pair.document_b,
        origin=pair.origin,
        similarity=pair.similarity,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Contradiction.chunk_a_id, Contradiction.chunk_b_id],
            set_={"check_id": stmt.excluded.check_id},
        )
    )


async def store_check(
    session: AsyncSession, pair: Candidate, values: dict[str, object]
) -> ContradictionCheck:
    stmt = (
        insert(ContradictionCheck)
        .values(key_a=pair.key_a, key_b=pair.key_b, **values)
        .on_conflict_do_nothing(index_elements=[ContradictionCheck.key_a, ContradictionCheck.key_b])
    )
    await session.execute(stmt)
    check = await cached(session, pair)
    assert check is not None
    return check


async def _plan(
    session: AsyncSession, doc: Document, settings: Settings
) -> tuple[list[Candidate], int]:
    """Кандидаты документа, которых ещё нет в кэше (уже проверенные сразу записываются)."""
    pairs = await find_candidates(session, doc.id, settings)
    todo: list[Candidate] = []
    for p in pairs:
        check = await cached(session, p)
        if check is None:
            todo.append(p)
        else:
            await link(session, check, p)
    return todo, len(pairs)


async def handle_scan(
    job: ClaimedJob, settings: Settings, sm: async_sessionmaker[AsyncSession]
) -> None:
    document_id = int(job.payload["document_id"])
    async with sm() as s, s.begin():
        if not await enabled(s):
            return  # выключено администратором: остальное — когда включат
        doc = await s.get(Document, document_id)
        if doc is None or doc.deleted_at is not None or doc.is_excluded:
            return
        if doc.indexed_version is None or doc.contra_version == doc.indexed_version:
            return
        if doc.legal_status == CANCELLED:
            # Противоречие с отменённым документом не активно: модель на него не тратим.
            doc.contra_version, doc.contra_pairs = doc.indexed_version, 0
            return
        missing = await s.scalar(
            select(exists().where(Chunk.document_id == document_id, Chunk.embedding.is_(None)))
        )
        if missing:
            raise JobDeferred(60, "ждём векторизацию фрагментов")
        todo, total = await _plan(s, doc, settings)
        version = doc.indexed_version
        captions = {doc.id: caption_for_model(doc)}
        for other in {p.document_a for p in todo} | {p.document_b for p in todo}:
            if other not in captions:
                d = await s.get(Document, other)
                captions[other] = caption_for_model(d) if d else ""
        if not todo:
            doc.contra_version, doc.contra_pairs = doc.indexed_version, total
            return

    done = 0
    async with Ollama(settings) as client:
        for pair in todo:
            if done >= settings.contra_pairs_per_job:
                raise JobDeferred(0, "продолжение: следующие пары документа")
            async with sm() as s:
                conn = await s.connection()
                if not await background_llm_allowed(conn, settings.llm_queue_stale_seconds):
                    raise JobDeferred(5, "пользователь ждёт ответ")
            t0 = time.perf_counter()
            values: dict[str, object]
            try:
                v = await judge(
                    client, settings, pair, captions[pair.document_a], captions[pair.document_b]
                )
                values = v.model_dump()
            except OllamaUnavailable as exc:
                raise JobDeferred(60, str(exc)) from exc
            except ModelNotFound as exc:
                raise JobDeferred(300, str(exc)) from exc
            except (OllamaError, ValueError) as exc:  # ValidationError — подкласс ValueError
                values = {"verdict": "error", "error": f"{type(exc).__name__}: {exc}"[:200]}
            values["ms"] = int((time.perf_counter() - t0) * 1000)
            values["model"] = settings.llm_model
            async with sm() as s, s.begin():
                check = await store_check(s, pair, values)
                # Пока шёл вызов, документ могли переразобрать: фрагментов уже нет.
                alive = await s.scalar(
                    text("SELECT count(*) FROM chunks WHERE id IN (:a, :b)"),
                    {"a": pair.chunk_a, "b": pair.chunk_b},
                )
                if alive == 2:
                    await link(s, check, pair)
            done += 1

    async with sm() as s, s.begin():
        doc = await s.get(Document, document_id)
        # Документ не переразобрали, пока шла проверка, — он готов.
        if doc is not None and doc.indexed_version == version:
            doc.contra_version, doc.contra_pairs = version, total


async def backfill_tick(session: AsyncSession, limit: int = 200) -> int:
    """Если поиск включён — ставит в очередь документы, которые ещё не проверены (или
    изменились после проверки) и у которых все фрагменты уже векторизованы."""
    if not await enabled(session):
        return 0
    ids = (
        await session.scalars(
            text(
                """
                SELECT d.id FROM documents d
                WHERE d.indexed_version IS NOT NULL AND d.deleted_at IS NULL
                  AND NOT d.is_excluded
                  AND d.contra_version IS DISTINCT FROM d.indexed_version
                  AND NOT EXISTS (SELECT 1 FROM chunks c
                                  WHERE c.document_id = d.id AND c.embedding IS NULL)
                  AND NOT EXISTS (SELECT 1 FROM jobs j
                                  WHERE j.dedupe_key = 'contra:' || d.id
                                    AND j.status IN ('queued', 'running'))
                ORDER BY d.indexed_version DESC
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )
    ).all()
    n = 0
    for doc_id in ids:
        if await enqueue_scan(session, int(doc_id), PRIORITY_BACKLOG) is not None:
            n += 1
    return n


async def rescan_all(session: AsyncSession) -> int:
    """Пройти все документы заново (например, после смены порогов). Проверенные пары
    берутся из кэша, модель спрашивается только о новых."""
    await session.execute(update(Document).values(contra_version=None))
    return await backfill_tick(session, limit=100_000)


async def retry_errors(session: AsyncSession) -> int:
    """Пары, на которых модель ответила не по схеме, — забыть и проверить снова."""
    result = await session.execute(
        delete(ContradictionCheck).where(ContradictionCheck.verdict == "error")
    )
    n = result.rowcount or 0
    if n:
        await rescan_all(session)
    return n
