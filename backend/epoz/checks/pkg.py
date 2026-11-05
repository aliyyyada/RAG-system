"""Файлы пакета проверки: разбор в документы (без векторов и графа), доступ к тексту и таблицам.

Документ пакета создаётся с `check_run_id`: общие запросы (поиск, граф, противоречия, списки)
его не видят. Для проверки он не векторизуется — это экономит видеокарту; критерии работают
с текстом и таблицами напрямую, а цитаты берут из фрагментов с номерами страниц.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.ingest.chunker import chunk_blocks
from app.ingest.files import MIME
from app.ingest.parse_pool import parse_file_async
from app.models import Chunk, Collection, Document, DocumentFile
from epoz.checks.textutil import md_tables, norm_ws
from epoz.models import CheckFile, CheckRun

COLLECTION_NAME = "Проверки ЕПоЗ (временные)"


def clean_text(text: str) -> str:
    """Распознанный текст без служебных строк: «--- стр. 2 ---» и маркеров заголовков «##»."""
    lines = []
    for line in text.splitlines():
        if re.fullmatch(r"---\s*стр\.\s*\d+\s*---", line.strip()):
            continue
        lines.append(re.sub(r"^#{1,6}\s+", "", line))
    return "\n".join(lines).strip()


@dataclass
class ChunkRef:
    id: int
    ord: int
    text: str
    page: int | None


@dataclass
class FileData:
    """Файл пакета в удобном для правил виде."""

    id: int
    name: str
    role: str
    role_detected: str | None
    is_winner: bool
    document_id: int | None
    parse_status: str
    parse_error: str | None
    ocr_pages: int
    pages: int | None
    text: str = ""
    chunks: list[ChunkRef] = field(default_factory=list)
    tables: list[list[list[str]]] = field(default_factory=list)
    extracted: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.parse_status == "done" and bool(self.text.strip())

    @property
    def scanned(self) -> bool:
        return self.ocr_pages > 0

    def page_of(self, needle: str) -> int | None:
        n = norm_ws(needle).lower()[:60]
        for c in self.chunks:
            if n and n in norm_ws(c.text).lower():
                return c.page
        return None

    def quote(self, needle: str, width: int = 170) -> dict[str, Any]:
        """Цитата вокруг `needle` с файлом и страницей (если она известна)."""
        low = self.text.lower()
        pos = low.find(norm_ws(needle).lower()[:60])
        if pos < 0:
            pos = 0
        return {
            "file": self.name,
            "page": self.page_of(needle),
            "text": norm_ws(self.text[max(0, pos - 20) : pos + width]),
        }


async def _collection(session: AsyncSession) -> Collection:
    coll = await session.scalar(select(Collection).where(Collection.name == COLLECTION_NAME))
    if coll is None:
        coll = Collection(
            name=COLLECTION_NAME, kind="topic", description="Служебная: пакеты проверок ЕПоЗ"
        )
        session.add(coll)
        await session.flush()
    return coll


async def ingest_file(
    session: AsyncSession, settings: Settings, run: CheckRun, cf: CheckFile
) -> None:
    """Разбирает файл пакета (PDF, DOCX, DOC, скан через OCR) в документ проверки."""
    path = Path(cf.path)
    try:
        kind, parsed = await parse_file_async(path, cf.name, settings)
    except Exception as exc:  # noqa: BLE001 — файл битый: проверка продолжится без него
        cf.parse_status = "error"
        cf.parse_error = (getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}")[:500]
        return
    coll = await _collection(session)
    now = datetime.now(UTC)
    doc = Document(
        collection_id=coll.id,
        title=cf.name[:500],
        check_run_id=run.id,
        header={"check_role": cf.role},
        status="ready",
        version=now,
        indexed_version=now,
        links_version=now,
        contra_version=now,
    )
    session.add(doc)
    await session.flush()
    sha = hashlib.sha256(await asyncio.to_thread(path.read_bytes)).hexdigest()
    frow = DocumentFile(
        document_id=doc.id,
        ord=0,
        name=cf.name[:500],
        mime=MIME[kind],
        size=cf.size,
        sha256=sha,
        pages=parsed.pages,
        ocr_pages=parsed.ocr_pages,
        warnings=parsed.warnings,
        text=parsed.text,
    )
    session.add(frow)
    await session.flush()
    drafts = chunk_blocks(
        parsed.blocks, target=settings.chunk_target_chars, maximum=settings.chunk_max_chars
    )
    for i, c in enumerate(drafts):
        session.add(
            Chunk(
                document_id=doc.id,
                file_id=frow.id,
                ord=i,
                kind=c.kind,
                text=c.text,
                section=c.section,
                point=c.point,
                page_from=c.page_from,
                page_to=c.page_to,
                token_estimate=c.token_estimate,
                graph_state="skipped",
                graph_note="пакет проверки",
            )
        )
    doc.chunk_count = len(drafts)
    cf.document_id = doc.id
    cf.parse_status = "done"
    cf.pages = parsed.pages
    cf.ocr_pages = parsed.ocr_pages


async def load_files(session: AsyncSession, run: CheckRun) -> list[FileData]:
    rows = list(
        await session.scalars(
            select(CheckFile).where(CheckFile.run_id == run.id).order_by(CheckFile.ord)
        )
    )
    out: list[FileData] = []
    for cf in rows:
        fd = FileData(
            id=cf.id,
            name=cf.name,
            role=cf.role,
            role_detected=cf.role_detected,
            is_winner=cf.is_winner,
            document_id=cf.document_id,
            parse_status=cf.parse_status,
            parse_error=cf.parse_error,
            ocr_pages=cf.ocr_pages,
            pages=cf.pages,
            extracted=dict(cf.extracted or {}),
        )
        if cf.document_id is not None:
            chunks = list(
                await session.scalars(
                    select(Chunk).where(Chunk.document_id == cf.document_id).order_by(Chunk.ord)
                )
            )
            fd.chunks = [ChunkRef(c.id, c.ord, c.text, c.page_from) for c in chunks]
            # Полный распознанный текст файла: в нём есть заголовки («Коммерческое предложение № 1»),
            # которые в текст фрагментов не входят (они хранятся как «раздел» фрагмента).
            full = await session.scalar(
                select(DocumentFile.text).where(DocumentFile.document_id == cf.document_id)
            )
            fd.text = clean_text(full or "") or "\n\n".join(c.text for c in chunks)
            fd.tables = md_tables(fd.text)
        out.append(fd)
    return out
