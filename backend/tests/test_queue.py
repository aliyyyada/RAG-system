import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.worker import queue

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_jobs")]


async def test_dequeue_respects_priority_and_order(engine: AsyncEngine) -> None:
    async with AsyncSession(engine) as s, s.begin():
        low = await queue.enqueue(s, "parse", {"doc": 1}, priority=0)
        high = await queue.enqueue(s, "parse", {"doc": 2}, priority=10)
        low2 = await queue.enqueue(s, "parse", {"doc": 3}, priority=0)

    order = []
    for _ in range(3):
        async with AsyncSession(engine) as s, s.begin():
            job = await queue.dequeue(s, "w1")
            order.append(job.id)
    assert order == [high, low, low2]

    async with AsyncSession(engine) as s, s.begin():
        assert await queue.dequeue(s, "w1") is None


async def test_two_workers_never_get_same_job(engine: AsyncEngine) -> None:
    async with AsyncSession(engine) as s, s.begin():
        await queue.enqueue(s, "parse")
        await queue.enqueue(s, "parse")

    # Первая транзакция ещё открыта (задача заблокирована) — вторая берёт другую.
    async with AsyncSession(engine) as s1, s1.begin():
        a = await queue.dequeue(s1, "w1")
        async with AsyncSession(engine) as s2, s2.begin():
            b = await queue.dequeue(s2, "w2")
            async with AsyncSession(engine) as s3, s3.begin():
                c = await queue.dequeue(s3, "w3")
    assert a and b and a.id != b.id
    assert c is None


async def test_failed_job_retries_then_fails(engine: AsyncEngine) -> None:
    async with AsyncSession(engine) as s, s.begin():
        job_id = await queue.enqueue(s, "parse")
        await s.execute(text("UPDATE jobs SET max_attempts = 2 WHERE id = :id"), {"id": job_id})

    for expected in ("queued", "failed"):
        async with AsyncSession(engine) as s, s.begin():
            job = await queue.dequeue(s, "w1")
            assert job and job.id == job_id
            await queue.mark_failed(s, job.id, "boom", retry_delay_seconds=0)
        async with AsyncSession(engine) as s:
            sql = text("SELECT status, last_error FROM jobs WHERE id = :id")
            status, err = (await s.execute(sql, {"id": job_id})).one()
        assert (status, err) == (expected, "boom")


async def test_stale_running_job_is_requeued_after_restart(engine: AsyncEngine) -> None:
    async with AsyncSession(engine) as s, s.begin():
        job_id = await queue.enqueue(s, "embed")
        await queue.dequeue(s, "crashed-worker")
        await s.execute(
            text("UPDATE jobs SET locked_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": job_id},
        )
    async with AsyncSession(engine) as s, s.begin():
        assert await queue.requeue_stale(s, stale_after_seconds=600) == 1
        job = await queue.dequeue(s, "new-worker")
    assert job and job.id == job_id and job.attempts == 2


async def test_worker_runs_job_and_records_unknown_kind_error(engine: AsyncEngine) -> None:
    from app.worker.main import run_once

    async with AsyncSession(engine) as s, s.begin():
        ok = await queue.enqueue(s, "noop", {"x": 1})
        bad = await queue.enqueue(s, "no-such-kind")

    assert await run_once("w") is True
    assert await run_once("w") is True
    async with AsyncSession(engine) as s:
        rows = dict((await s.execute(text("SELECT id, status FROM jobs"))).all())
        sql = text("SELECT last_error FROM jobs WHERE id = :i")
        err = (await s.execute(sql, {"i": bad})).scalar()
        stats = await queue.queue_stats(s)
    assert rows[ok] == "done"
    assert rows[bad] == "queued"  # будет повторена позже
    assert "no-such-kind" in err
    assert stats["done"] == 1
