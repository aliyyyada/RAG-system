"""Обработка одного документа (задача worker'а `document_process`).

Путь документа из источника:

1. **Получение.** Берём у источника шапку, табличные части и список файлов.
   Источник говорит, что документ удалён, или не знает его → мягкое удаление.
   Версия не изменилась и документ уже проиндексирован → ничего не делаем (идемпотентность).
2. **Staging.** Пишем полученное в staging_documents как есть.
3. **Проверка полноты.** Строк в табличной части столько, сколько объявил источник;
   каждый файл скачался, его размер совпал с объявленным.
4. **Разбор.** Файлы открываются и разбираются (PDF, DOCX, OCR сканов); шапка и табличные
   части собираются в текст; всё режется на фрагменты.
5. **Сохранение.** Одной транзакцией заменяем файлы и фрагменты документа, ставим
   indexed_version = версия источника, status = ready. Пока шаги 1–4 идут, старые фрагменты
   остаются в поиске, документ не пропадает.

Любая ошибка записывается в документ с этапом и причиной. Задача повторяется
(document_max_attempts раз), после последней попытки документ получает статус «ошибка»
и ждёт кнопки «Повторить» в отчёте.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.graph import rules as graph_rules
from app.ingest.assemble import assemble_header, header_meta
from app.ingest.blocks import Block
from app.ingest.chunker import ChunkDraft, chunk_blocks
from app.ingest.errors import IngestError
from app.ingest.files import MIME, parse_file
from app.ingest.parsed import ParsedFile
from app.ingest.storage import FileStore
from app.models import Chunk, Document, DocumentEvent, DocumentFile, Source, StagingDocument
from app.search.indexer import enqueue_embedding
from app.sources import open_source
from app.sources.base import (
    BadSourceData,
    DocumentNotFound,
    SourceDocument,
    SourceError,
    SourceUnavailable,
)
from app.worker import queue
from app.worker.queue import ClaimedJob

log = logging.getLogger(__name__)

# Приоритеты задач: больше — раньше. Ручная загрузка и свежие изменения обгоняют
# первичную индексацию тысяч документов.
PRIORITY_MANUAL = 20
PRIORITY_CHANGED = 10
PRIORITY_RETRY = 5
PRIORITY_INITIAL = 0

JOB_KIND = "document_process"


def now() -> datetime:
    return datetime.now(UTC)


async def add_event(
    session: AsyncSession,
    document_id: int,
    event: str,
    version: datetime | None = None,
    **details: Any,
) -> None:
    session.add(
        DocumentEvent(document_id=document_id, event=event, version=version, details=details)
    )


async def enqueue_document(
    session: AsyncSession,
    document_id: int,
    settings: Settings,
    priority: int,
    *,
    force: bool = False,
) -> int | None:
    return await queue.enqueue(
        session,
        JOB_KIND,
        {"document_id": document_id, "force": force},
        priority,
        dedupe_key=f"document:{document_id}",
        max_attempts=settings.document_max_attempts,
    )


def set_status(doc: Document, status: str) -> None:
    if doc.status != status:
        doc.status = status
        doc.status_changed_at = now()
    doc.updated_at = now()


async def soft_delete(session: AsyncSession, doc: Document, reason: str) -> bool:
    """Документ пропадает из поиска; запись, файлы и история остаются."""
    if doc.deleted_at is not None:
        return False
    doc.deleted_at = now()
    doc.deleted_reason = reason
    if doc.status in ("queued", "processing"):
        set_status(doc, "ready" if doc.indexed_version else "error")
    doc.updated_at = now()
    await add_event(session, doc.id, "deleted", doc.version, reason=reason)
    # Удалённый документ больше не «отменяет» другие (этап 5).
    await session.flush()
    await graph_rules.on_document_gone(session, doc.id)
    return True


@dataclass
class PreparedFile:
    name: str
    mime: str
    size: int
    sha256: str
    parsed: ParsedFile
    source_file_id: str | None = None
    tmp: Path | None = None
    existing_id: int | None = None


@dataclass
class Prepared:
    header: dict[str, Any]
    parts_summary: list[dict[str, Any]]
    version: datetime | None
    header_blocks: list[Block]
    files: list[PreparedFile] = field(default_factory=list)
    # Для источника файлы заменяются целиком; у ручной загрузки файл тот же.
    replace_files: bool = True


async def process_document(
    job: ClaimedJob, settings: Settings, sm: async_sessionmaker[AsyncSession]
) -> None:
    document_id = int(job.payload["document_id"])
    force = bool(job.payload.get("force"))

    async with sm() as s, s.begin():
        doc = await s.get(Document, document_id, with_for_update=True)
        if doc is None or doc.deleted_at is not None:
            return
        source = await s.get(Source, doc.source_id) if doc.source_id else None
        set_status(doc, "processing")
        snapshot = _DocSnapshot(doc)

    store = FileStore(settings)
    temps: list[Path] = []
    try:
        if source is None:
            prepared = await _prepare_manual(snapshot, settings, sm, store)
        else:
            prepared = await _prepare_from_source(
                snapshot, source, settings, sm, store, temps, force
            )
        if prepared is not None:
            await _store(document_id, prepared, settings, sm, store)
    except Exception as exc:
        await _record_failure(sm, document_id, job, exc)
        raise
    finally:
        for t in temps:
            t.unlink(missing_ok=True)


class _DocSnapshot:
    """Нужные поля документа: сам ORM-объект после закрытия транзакции не используем."""

    id: int
    external_id: str | None
    title: str
    number: str | None
    doc_type: str | None
    indexed_version: datetime | None

    def __init__(self, doc: Document):
        self.id = doc.id
        self.external_id = doc.external_id
        self.title = doc.title
        self.number = doc.number
        self.doc_type = doc.doc_type
        self.indexed_version = doc.indexed_version


async def _prepare_from_source(
    snap: _DocSnapshot,
    source: Source,
    settings: Settings,
    sm: async_sessionmaker[AsyncSession],
    store: FileStore,
    temps: list[Path],
    force: bool,
) -> Prepared | None:
    assert snap.external_id is not None
    adapter = open_source(source, settings)
    try:
        try:
            src = await adapter.get_document(snap.external_id)
        except DocumentNotFound:
            await _mark_deleted(sm, snap.id, "missing_in_source")
            return None
        except BadSourceData as exc:
            raise IngestError("fetch", f"Источник отдал неполные данные: {exc}") from exc
        except SourceUnavailable as exc:
            raise IngestError("fetch", str(exc)) from exc

        if src.deleted_at is not None:
            await _mark_deleted(sm, snap.id, "deleted_in_source")
            return None
        if (
            not force
            and snap.indexed_version is not None
            and src.modified_at <= snap.indexed_version
        ):
            await _mark_unchanged(sm, snap.id)
            return None

        await _write_staging(sm, source.id, src, snap.id, first=snap.indexed_version is None)
        errors = _check_parts(src)
        files: list[PreparedFile] = []
        max_bytes = settings.source_file_max_mb * 1024 * 1024
        for ref in src.files:
            tmp = store.tmp_path()
            temps.append(tmp)
            try:
                dl = await adapter.download_file(ref.file_id, tmp, max_bytes)
            except DocumentNotFound:
                errors.append(f"Файл «{ref.name}»: источник не отдал файл")
                continue
            except BadSourceData as exc:
                errors.append(f"Файл «{ref.name}»: {exc}")
                continue
            except SourceUnavailable as exc:
                raise IngestError("fetch", f"Файл «{ref.name}»: {exc}") from exc
            if ref.size is not None and dl.size != ref.size:
                errors.append(f"Файл «{ref.name}»: объявлено {ref.size} байт, получено {dl.size}")
                continue
            files.append(
                PreparedFile(
                    name=ref.name,
                    mime=ref.mime or "",
                    size=dl.size,
                    sha256=dl.sha256,
                    parsed=ParsedFile(),
                    source_file_id=ref.file_id,
                    tmp=tmp,
                )
            )
        if errors:
            await _fail_staging(sm, source.id, src.external_id, errors)
            raise IngestError("completeness", "; ".join(errors))

        for f in files:
            assert f.tmp is not None
            try:
                kind, f.parsed = await asyncio.to_thread(parse_file, f.tmp, f.name, settings)
            except IngestError as exc:
                await _fail_staging(sm, source.id, src.external_id, [exc.message])
                raise
            f.mime = MIME[kind]
    finally:
        await adapter.aclose()

    return Prepared(
        header=src.header,
        parts_summary=[
            {"name": p.name, "rows": len(p.rows), "declared": p.declared_rows} for p in src.parts
        ],
        version=src.modified_at,
        header_blocks=assemble_header(src.header, src.parts),
        files=files,
    )


def _check_parts(src: SourceDocument) -> list[str]:
    errors = []
    for part in src.parts:
        if part.declared_rows is not None and part.declared_rows != len(part.rows):
            errors.append(
                f"Табличная часть «{part.name}»: ожидалось {part.declared_rows} "
                f"строк, пришло {len(part.rows)}"
            )
    return errors


async def _prepare_manual(
    snap: _DocSnapshot,
    settings: Settings,
    sm: async_sessionmaker[AsyncSession],
    store: FileStore,
) -> Prepared:
    async with sm() as s:
        rows = (
            await s.scalars(
                select(DocumentFile)
                .where(DocumentFile.document_id == snap.id)
                .order_by(DocumentFile.ord)
            )
        ).all()
    files = []
    for row in rows:
        path = store.path_for(row.sha256)
        if not path.exists():
            raise IngestError("store", f"Файл «{row.name}» пропал с диска")
        kind, parsed = await asyncio.to_thread(parse_file, path, row.name, settings)
        files.append(
            PreparedFile(
                name=row.name,
                mime=MIME[kind],
                size=row.size,
                sha256=row.sha256,
                parsed=parsed,
                existing_id=row.id,
            )
        )
    header = {"title": snap.title, "number": snap.number, "doc_type": snap.doc_type}
    return Prepared(
        header={k: v for k, v in header.items() if v},
        parts_summary=[],
        version=None,
        header_blocks=assemble_header(header, []),
        files=files,
        replace_files=False,
    )


async def _store(
    document_id: int,
    prepared: Prepared,
    settings: Settings,
    sm: async_sessionmaker[AsyncSession],
    store: FileStore,
) -> None:
    drafts: list[tuple[ChunkDraft, int | None]] = []
    header_chunks = chunk_blocks(
        prepared.header_blocks,
        target=settings.chunk_target_chars,
        maximum=settings.chunk_max_chars,
        kind="header",
    )

    async with sm() as s, s.begin():
        doc = await s.get(Document, document_id, with_for_update=True)
        if doc is None:
            return
        await s.execute(delete(Chunk).where(Chunk.document_id == document_id))
        if prepared.replace_files:
            await s.execute(delete(DocumentFile).where(DocumentFile.document_id == document_id))

        drafts += [(c, None) for c in header_chunks]
        for ord_, f in enumerate(prepared.files):
            if f.tmp is not None:
                store.commit(f.tmp, f.sha256)
            if f.existing_id is not None:
                row = await s.get(DocumentFile, f.existing_id)
                assert row is not None
            else:
                row = DocumentFile(document_id=document_id, ord=ord_, name=f.name)
                s.add(row)
            row.source_file_id = f.source_file_id
            row.mime, row.size, row.sha256 = f.mime, f.size, f.sha256
            row.pages, row.ocr_pages = f.parsed.pages, f.parsed.ocr_pages
            row.text, row.warnings = f.parsed.text, f.parsed.warnings
            await s.flush()
            for c in chunk_blocks(
                f.parsed.blocks,
                target=settings.chunk_target_chars,
                maximum=settings.chunk_max_chars,
            ):
                drafts.append((c, row.id))

        for i, (c, file_id) in enumerate(drafts):
            s.add(
                Chunk(
                    document_id=document_id,
                    file_id=file_id,
                    ord=i,
                    kind=c.kind,
                    text=c.text,
                    section=c.section,
                    point=c.point,
                    page_from=c.page_from,
                    page_to=c.page_to,
                    token_estimate=c.token_estimate,
                )
            )

        if prepared.replace_files:  # документ источника: метаданные из шапки
            _apply_header(doc, prepared.header)
        doc.parts_summary = prepared.parts_summary
        if prepared.version is not None:
            doc.version = max(doc.version or prepared.version, prepared.version)
            doc.indexed_version = prepared.version
        else:
            doc.indexed_version = now()
        doc.chunk_count = len(drafts)
        doc.error_stage = doc.error_message = None
        doc.attempts = 0
        # Пока шёл разбор, синхронизация могла увидеть версию новее: тогда документ снова
        # в очереди (задача уже стоит), а не «готово».
        newer = prepared.version is not None and doc.version > prepared.version
        set_status(doc, "queued" if newer else "ready")
        if doc.source_id is not None and doc.external_id is not None:
            await s.execute(
                delete(StagingDocument).where(
                    StagingDocument.source_id == doc.source_id,
                    StagingDocument.external_id == doc.external_id,
                )
            )
        # Этап 4: новые фрагменты без векторов — в очередь на векторизацию (bge-m3).
        await enqueue_embedding(s, document_id)
        # Этап 5: связи «отменяет / изменяет» по правилам и статус документа.
        await graph_rules.enqueue_rules(s, document_id)
        warnings = [w for f in prepared.files for w in f.parsed.warnings]
        await add_event(
            s,
            document_id,
            "indexed",
            prepared.version,
            chunks=len(drafts),
            files=len(prepared.files),
            ocr_pages=sum(f.parsed.ocr_pages for f in prepared.files),
            warnings=warnings[:20],
        )


async def _write_staging(
    sm: async_sessionmaker[AsyncSession],
    source_id: int,
    src: SourceDocument,
    document_id: int,
    *,
    first: bool,
) -> None:
    parts = [{"name": p.name, "row_count": p.declared_rows, "rows": p.rows} for p in src.parts]
    files = [{"id": f.file_id, "name": f.name, "size": f.size, "mime": f.mime} for f in src.files]
    async with sm() as s, s.begin():
        row = await s.scalar(
            select(StagingDocument).where(
                StagingDocument.source_id == source_id,
                StagingDocument.external_id == src.external_id,
            )
        )
        if row is None:
            row = StagingDocument(source_id=source_id, external_id=src.external_id)
            s.add(row)
        row.version = src.modified_at
        row.received_at = now()
        row.header, row.parts, row.files = src.header, parts, files
        row.status, row.check_errors = "received", []
        if first:
            # Документ ещё ни разу не загружался: сразу показываем его номер и название,
            # чтобы в отчёте об ошибках было видно, что это за документ.
            doc = await s.get(Document, document_id)
            if doc is not None:
                _apply_header(doc, src.header)


def _apply_header(doc: Document, header: dict[str, Any]) -> None:
    meta = header_meta(header)
    doc.header = header
    doc.number, doc.title, doc.doc_type = meta.number, meta.title, meta.doc_type
    doc.topic, doc.doc_date = meta.topic, meta.doc_date


async def _fail_staging(
    sm: async_sessionmaker[AsyncSession], source_id: int, external_id: str, errors: list[str]
) -> None:
    async with sm() as s, s.begin():
        row = await s.scalar(
            select(StagingDocument).where(
                StagingDocument.source_id == source_id,
                StagingDocument.external_id == external_id,
            )
        )
        if row is not None:
            row.status, row.check_errors = "failed", errors


async def _mark_deleted(
    sm: async_sessionmaker[AsyncSession], document_id: int, reason: str
) -> None:
    async with sm() as s, s.begin():
        doc = await s.get(Document, document_id, with_for_update=True)
        if doc is not None:
            await soft_delete(s, doc, reason)


async def _mark_unchanged(sm: async_sessionmaker[AsyncSession], document_id: int) -> None:
    async with sm() as s, s.begin():
        doc = await s.get(Document, document_id, with_for_update=True)
        if doc is not None:
            doc.error_stage = doc.error_message = None
            doc.attempts = 0
            set_status(doc, "ready")


async def _record_failure(
    sm: async_sessionmaker[AsyncSession], document_id: int, job: ClaimedJob, exc: Exception
) -> None:
    if isinstance(exc, IngestError):
        stage, message = exc.stage, exc.message
    elif isinstance(exc, SourceError):
        stage, message = "fetch", str(exc)
    else:
        stage, message = "store", f"Внутренняя ошибка: {type(exc).__name__}: {exc}"
    final = job.attempts >= job.max_attempts
    async with sm() as s, s.begin():
        doc = await s.get(Document, document_id, with_for_update=True)
        if doc is None:
            return
        doc.error_stage, doc.error_message = stage, message[:2000]
        doc.attempts = job.attempts
        set_status(doc, "error" if final else "queued")
        await add_event(
            s,
            document_id,
            "error",
            doc.version,
            stage=stage,
            message=message[:500],
            attempt=job.attempts,
            final=final,
        )
    log.warning("document %s: %s (%s), попытка %s", document_id, stage, message, job.attempts)
