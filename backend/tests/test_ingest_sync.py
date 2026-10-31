"""Синхронизация с фейковым источником: полная загрузка, инкремент, удаление, сверка, ошибки."""

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from app.config import get_settings
from app.db import get_sessionmaker
from app.ingest import sync
from app.ingest.service import retry_document
from app.ingest.storage import FileStore
from app.models import Chunk, Document, DocumentFile, Source, StagingDocument, SyncRun
from app.worker import queue
from app.worker.main import run_once
from fakes.fake_source import FILES_DIR, FakeSource

pytestmark = pytest.mark.db


async def drain(limit: int = 200) -> int:
    """Выполняет задачи очереди, пока они есть (как worker)."""
    n = 0
    while await run_once("test-worker"):
        n += 1
        assert n < limit, "очередь не опустела"
    return n


async def docs_by_key(source: FakeSource) -> dict[str, Document]:
    async with get_sessionmaker()() as s:
        rows = (await s.scalars(select(Document))).all()
    by_id = {d.external_id: d for d in rows}
    return {d["key"]: by_id[k] for k, d in source.docs.items() if k in by_id}


async def chunks_of(doc_id: int) -> list[Chunk]:
    async with get_sessionmaker()() as s:
        return list(
            await s.scalars(select(Chunk).where(Chunk.document_id == doc_id).order_by(Chunk.ord))
        )


async def first_load(source_id: int) -> dict[str, int]:
    stats = await sync.run_incremental(source_id, get_settings(), get_sessionmaker())
    await drain()
    return stats


async def test_first_sync_loads_everything_and_reports_errors(
    docflow: int, fake_source: FakeSource
) -> None:
    stats = await first_load(docflow)
    # Первая синхронизация — полная сверка: 9 строк у источника, из них 1 удалённая.
    assert stats["seen"] == 9 and stats["new"] == 8

    docs = await docs_by_key(fake_source)
    assert "prikaz-12" not in docs  # удалён в источнике до первой загрузки — не заводим
    ready = {k for k, d in docs.items() if d.status == "ready"}
    assert ready == {
        "prikaz-112",
        "prikaz-45",
        "prikaz-140",
        "iot-044",
        "polozhenie-suot",
        "reglament-to",
    }

    broken = docs["prikaz-97"]
    assert (broken.status, broken.error_stage, broken.attempts) == ("error", "parse", 3)
    assert "пустой (0 байт)" in broken.error_message

    journal = docs["journal-f3"]
    assert (journal.status, journal.error_stage) == ("error", "completeness")
    assert "«Строки»: ожидалось 148 строк, пришло 120" in journal.error_message

    # Общие документы — из списка источника.
    assert {k for k, d in docs.items() if d.is_common} == {"iot-044", "polozhenie-suot"}

    # В staging остаются только документы, не прошедшие проверку.
    async with get_sessionmaker()() as s:
        staged = (await s.scalars(select(StagingDocument))).all()
        src = await s.get(Source, docflow)
    assert {(x.external_id, x.status) for x in staged} == {
        (broken.external_id, "failed"),
        (journal.external_id, "failed"),
    }
    assert src is not None and src.available is True and src.source_total == 8
    assert src.sync_cursor is not None and src.last_reconcile_at is not None


async def test_document_assembled_from_header_parts_and_pdf(
    docflow: int, fake_source: FakeSource
) -> None:
    await first_load(docflow)
    doc = (await docs_by_key(fake_source))["prikaz-112"]
    assert (doc.number, doc.doc_type, doc.title) == (
        "112",
        "Приказ",
        "О допуске персонала к работам на высоте",
    )
    assert str(doc.doc_date) == "2024-03-14"
    assert doc.parts_summary == [{"name": "Ознакомлены", "rows": 46, "declared": 46}]

    chunks = await chunks_of(doc.id)
    header = [c for c in chunks if c.kind == "header"]
    assert header[0].text.startswith("# Приказ № 112 от 14.03.2024 «О допуске")
    assert any("| Таб. № | ФИО | Должность | Дата ознакомления |" in c.text for c in header)

    # Пункт 4.2 — на 2-й странице PDF, в разделе «4. Порядок допуска».
    p42 = next(c for c in chunks if "4.2. Допуск к работам на высоте" in c.text)
    assert (p42.page_from, p42.section) == (2, "4. Порядок допуска")
    # Таблица из PDF сохранена таблицей.
    assert any("| Группа | Кто | Периодичность |" in c.text and c.kind == "table" for c in chunks)

    # Файл лежит на диске под именем = SHA-256.
    async with get_sessionmaker()() as s:
        f = await s.scalar(select(DocumentFile).where(DocumentFile.document_id == doc.id))
    assert f is not None and f.pages == 2 and f.ocr_pages == 0
    stored = FileStore(get_settings()).path_for(f.sha256)
    assert stored.read_bytes() == (FILES_DIR / "prikaz_112.pdf").read_bytes()


@pytest.mark.skipif(not __import__("shutil").which("tesseract"), reason="tesseract не установлен")
async def test_scan_is_recognized_by_ocr(docflow: int, fake_source: FakeSource) -> None:
    await first_load(docflow)
    doc = (await docs_by_key(fake_source))["prikaz-45"]
    async with get_sessionmaker()() as s:
        f = await s.scalar(select(DocumentFile).where(DocumentFile.document_id == doc.id))
    assert f is not None and f.ocr_pages == 1
    text = " ".join(c.text for c in await chunks_of(doc.id))
    assert "наряду-допуску" in text and "Смирнова" in text


async def test_large_table_split_by_rows_with_header(docflow: int, fake_source: FakeSource) -> None:
    await first_load(docflow)
    doc = (await docs_by_key(fake_source))["reglament-to"]
    tables = [c for c in await chunks_of(doc.id) if c.kind == "table"]
    assert len(tables) >= 2
    seen_rows: list[str] = []
    for c in tables:
        lines = c.text.splitlines()
        assert lines[0] == "| № | Оборудование | Участок | Вид ТО | Периодичность |"
        for line in lines[2:]:
            assert line.startswith("| ") and line.endswith(" |")  # строка целиком
            seen_rows.append(line.split(" | ")[0].strip("| "))
    assert [r for r in seen_rows if r != "№"] == [str(i) for i in range(1, 61)]


async def test_second_sync_is_idempotent(docflow: int, fake_source: FakeSource) -> None:
    await first_load(docflow)
    async with get_sessionmaker()() as s:
        before = (await s.scalars(select(Chunk.id).order_by(Chunk.id))).all()

    stats = await sync.run_incremental(docflow, get_settings(), get_sessionmaker())
    assert stats["new"] == stats["changed"] == 0
    assert await drain() == 0

    stats = await sync.run_reconcile(docflow, get_settings(), get_sessionmaker())
    assert stats["new"] == stats["changed"] == stats["missing"] == 0
    assert await drain() == 0
    async with get_sessionmaker()() as s:
        after = (await s.scalars(select(Chunk.id).order_by(Chunk.id))).all()
    assert before == after


async def test_changed_document_is_reindexed(docflow: int, fake_source: FakeSource) -> None:
    await first_load(docflow)
    old = (await docs_by_key(fake_source))["iot-044"]
    fake_source.touch("iot-044", title="Инструкция по охране труда для электрогазосварщика")

    stats = await sync.run_incremental(docflow, get_settings(), get_sessionmaker())
    assert stats["changed"] == 1
    assert (await docs_by_key(fake_source))["iot-044"].status == "queued"
    # Пока документ ждёт переобработки, старые фрагменты остаются (он не пропадает из поиска).
    assert await chunks_of(old.id)

    await drain()
    new = (await docs_by_key(fake_source))["iot-044"]
    assert new.status == "ready" and new.title.endswith("электрогазосварщика")
    assert new.indexed_version > old.indexed_version


async def test_overlap_window_catches_backdated_change(
    docflow: int, fake_source: FakeSource
) -> None:
    await first_load(docflow)
    async with get_sessionmaker()() as s:
        src = await s.get(Source, docflow)
        assert src is not None and src.sync_cursor is not None
        cursor = src.sync_cursor
    # Источник записал изменение «задним числом» — на 30 минут раньше нашего курсора.
    # Перекрытие окна (60 минут) его ловит.
    fake_source.touch("prikaz-140", at=cursor - timedelta(minutes=30), title="Редакция 2")
    stats = await sync.run_incremental(docflow, get_settings(), get_sessionmaker())
    assert stats["changed"] == 1
    await drain()
    assert (await docs_by_key(fake_source))["prikaz-140"].title == "Редакция 2"

    # Изменение старше перекрытия инкремент не видит — его догрузит ночная сверка.
    fake_source.touch("iot-044", at=cursor - timedelta(hours=2), title="Редакция 3")
    stats = await sync.run_incremental(docflow, get_settings(), get_sessionmaker())
    assert stats["changed"] == 0
    stats = await sync.run_reconcile(docflow, get_settings(), get_sessionmaker())
    assert stats["changed"] == 1
    await drain()
    assert (await docs_by_key(fake_source))["iot-044"].title == "Редакция 3"


async def test_soft_delete_and_restore(docflow: int, fake_source: FakeSource) -> None:
    await first_load(docflow)
    fake_source.delete("iot-044")
    stats = await sync.run_incremental(docflow, get_settings(), get_sessionmaker())
    assert stats["deleted"] == 1
    doc = (await docs_by_key(fake_source))["iot-044"]
    assert doc.deleted_at is not None and doc.deleted_reason == "deleted_in_source"
    # Запись и фрагменты остаются (история), но документ помечен удалённым.
    assert await chunks_of(doc.id)

    fake_source.docs[fake_source.id_of("iot-044")]["deleted_at"] = None
    fake_source.touch("iot-044")
    stats = await sync.run_incremental(docflow, get_settings(), get_sessionmaker())
    assert stats["restored"] == 1
    await drain()
    doc = (await docs_by_key(fake_source))["iot-044"]
    assert doc.deleted_at is None and doc.status == "ready"


async def test_nightly_reconcile_marks_extra_and_loads_missing(
    docflow: int, fake_source: FakeSource
) -> None:
    await first_load(docflow)
    fake_source.drop("polozhenie-suot")  # пропал у источника без пометки об удалении
    lost = (await docs_by_key(fake_source))["prikaz-140"]
    async with get_sessionmaker()() as s, s.begin():
        await s.delete(await s.get(Document, lost.id))  # «потеряли» у себя

    stats = await sync.run_reconcile(docflow, get_settings(), get_sessionmaker())
    assert stats["missing"] == 1 and stats["new"] == 1
    await drain()

    async with get_sessionmaker()() as s:
        gone = await s.scalar(
            select(Document).where(Document.deleted_reason == "missing_in_source")
        )
    assert gone is not None and gone.title.startswith("Положение")
    assert (await docs_by_key(fake_source))["prikaz-140"].status == "ready"


async def test_source_unavailable_is_reported_and_nothing_breaks(
    docflow: int, fake_source: FakeSource
) -> None:
    await first_load(docflow)
    fake_source.unavailable = True
    fake_source.touch("iot-044")
    await sync.run_incremental(docflow, get_settings(), get_sessionmaker())

    async with get_sessionmaker()() as s:
        src = await s.get(Source, docflow)
        run = await s.scalar(select(SyncRun).order_by(SyncRun.id.desc()))
        alive = await s.scalar(
            select(func.count()).select_from(Document).where(Document.deleted_at.is_(None))
        )
    assert src is not None and src.available is False and "503" in (src.last_error or "")
    assert run is not None and run.status == "error"
    assert alive == 8  # ничего не удалили из-за того, что источник не ответил

    # Ночная сверка при недоступном источнике тоже ничего не помечает удалённым.
    await sync.run_reconcile(docflow, get_settings(), get_sessionmaker())
    async with get_sessionmaker()() as s:
        alive = await s.scalar(
            select(func.count()).select_from(Document).where(Document.deleted_at.is_(None))
        )
    assert alive == 8


async def test_retry_after_source_fixed_the_file(docflow: int, fake_source: FakeSource) -> None:
    await first_load(docflow)
    doc = (await docs_by_key(fake_source))["prikaz-97"]
    assert doc.status == "error"
    # Источник заменил битый файл на нормальный.
    good = FILES_DIR / "prikaz_112.pdf"
    f = fake_source.docs[doc.external_id]["files"][0]
    f.update(path=good, size=good.stat().st_size)

    async with get_sessionmaker()() as s, s.begin():
        d = await s.get(Document, doc.id)
        assert d is not None
        await retry_document(s, d, get_settings(), actor="T-0004")
    await drain()
    doc = (await docs_by_key(fake_source))["prikaz-97"]
    assert doc.status == "ready" and doc.error_message is None and doc.attempts == 0


async def test_declared_file_size_mismatch_is_completeness_error(
    docflow: int, fake_source: FakeSource
) -> None:
    f = fake_source.docs[fake_source.id_of("iot-044")]["files"][0]
    f["size"] += 100
    await first_load(docflow)
    doc = (await docs_by_key(fake_source))["iot-044"]
    assert (doc.status, doc.error_stage) == ("error", "completeness")
    assert "iot_044.docx" in doc.error_message and "объявлено" in doc.error_message


async def test_indexing_resumes_after_restart(docflow: int, fake_source: FakeSource) -> None:
    await sync.run_incremental(docflow, get_settings(), get_sessionmaker())
    # Worker обработал два документа и «упал» посреди третьего.
    assert await run_once("w1") and await run_once("w1")
    async with get_sessionmaker()() as s, s.begin():
        job = await queue.dequeue(s, "crashed")
        assert job is not None
        await s.execute(
            text("UPDATE jobs SET locked_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": job.id},
        )
    async with get_sessionmaker()() as s, s.begin():
        assert await queue.requeue_stale(s, 600) == 1
    await drain()
    docs = await docs_by_key(fake_source)
    assert sum(d.status == "ready" for d in docs.values()) == 6


async def test_scheduler_plans_sync_once_and_reconcile_at_night(docflow: int) -> None:
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    settings = get_settings()
    async with get_sessionmaker()() as s, s.begin():
        assert await sync.schedule_tick(s, settings) == [(sync.SYNC_JOB, docflow)]
    async with get_sessionmaker()() as s, s.begin():
        assert await sync.schedule_tick(s, settings) == []  # уже в очереди

    tz = ZoneInfo("Europe/Moscow")
    src = Source(reconcile_hour=2, last_reconcile_at=None)
    at_1am = datetime(2026, 10, 31, 1, 0, tzinfo=tz).astimezone(UTC)
    at_3am = datetime(2026, 10, 31, 3, 0, tzinfo=tz).astimezone(UTC)
    assert not sync.reconcile_due(src, at_1am, tz)
    assert sync.reconcile_due(src, at_3am, tz)
    src.last_reconcile_at = datetime(2026, 10, 31, 2, 5, tzinfo=tz)
    assert not sync.reconcile_due(src, at_3am, tz)
    assert sync.reconcile_due(src, at_3am + timedelta(days=1), tz)


def test_fixture_files_exist() -> None:
    for name in ("prikaz_112.pdf", "prikaz_45_scan.pdf", "iot_044.docx", "reglament_to.pdf"):
        assert (FILES_DIR / name).stat().st_size > 0
    assert Path(FILES_DIR / "prikaz_97.pdf").stat().st_size == 0
