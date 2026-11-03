"""Справочник ОКПД2: разбор Excel, загрузка редакции, поиск кода.

Файл (например, с classifikators.ru) выглядит так: сверху несколько строк с названием
классификатора, редакцией («ОК 034-2014 (КПЕС 2008) с изменением № 149/2026. Введено 01.11.2026»)
и числом записей, затем таблица с колонками «№ / Код / Название». У кодов в конце бывает пробел.

Загрузка идёт одной транзакцией: либо вся редакция, либо ничего. Прежняя действующая редакция
остаётся в базе (для истории проверок), но перестаёт быть действующей.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.search.hybrid import tsquery_text
from epoz.models import Okpd2, Okpd2Edition

CODE_RE = re.compile(r"^\d{2}(?:\.\d{1,3}){0,3}$")
# Число знаков кода (без точек) → уровень: 2 — класс, … 9 — полный код.
LEVELS = {2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 9: 6}
MIN_RECORDS = 500  # в настоящем справочнике более 20 тысяч записей


class Okpd2Error(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Okpd2File:
    def __init__(self) -> None:
        self.title = ""
        self.note = ""
        self.effective_date: date | None = None
        self.declared_total: int | None = None
        self.rows: list[tuple[str, str]] = []
        self.skipped = 0
        self.duplicates = 0


def normalize_code(value: str) -> str:
    """«ОКПД2 26.20.11.110 » → «26.20.11.110»."""
    m = re.search(r"\d{2}(?:\.\d{1,3}){0,3}", value or "")
    return m.group(0) if m else ""


def parse_okpd2_xlsx(path: Path) -> Okpd2File:
    try:
        # Временный файл загрузки без расширения: openpyxl по имени такой файл не откроет,
        # поэтому передаём файловый объект.
        fh = path.open("rb")
        wb = load_workbook(fh, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise Okpd2Error(
            "bad_file", f"Файл не открывается как Excel (.xlsx): {type(exc).__name__}"
        ) from exc
    try:
        ws = wb.worksheets[0]
        out = Okpd2File()
        meta: list[str] = []
        header_seen = False
        seen: set[str] = set()
        for row in ws.iter_rows(values_only=True):
            cells = [("" if c is None else str(c).strip()) for c in row[:4]]
            while len(cells) < 3:
                cells.append("")
            if not header_seen:
                if cells[1] == "Код" and cells[2].lower().startswith("назван"):
                    header_seen = True
                elif cells[0]:
                    meta.append(cells[0])
                continue
            code, name = cells[1], cells[2]
            if not code and not name:
                continue
            if not CODE_RE.match(code) or not name:
                out.skipped += 1
                continue
            if code in seen:
                out.duplicates += 1
                continue
            seen.add(code)
            out.rows.append((code, re.sub(r"\s+", " ", name)))
    finally:
        wb.close()
        fh.close()
    if not header_seen:
        raise Okpd2Error(
            "no_header", "Не найдены колонки «Код» и «Название»: это не справочник ОКПД2"
        )
    out.title = meta[0] if meta else "ОКПД2"
    for line in meta[1:]:
        if "ОК 034" in line or "КПЕС" in line:
            out.note = line
            out.title = re.split(r"\.\s*Введено", line)[0].strip(" .")
            m = re.search(r"Введено\s+(\d{2})\.(\d{2})\.(\d{4})", line)
            if m:
                out.effective_date = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        m = re.search(r"Всего записей\D+(\d+)", line)
        if m:
            out.declared_total = int(m.group(1))
    return out


def _parents(codes: list[str]) -> dict[str, str | None]:
    """Родитель кода — самый длинный более короткий код, чьи цифры начинают этот код."""
    by_digits = {c.replace(".", ""): c for c in codes}
    out: dict[str, str | None] = {}
    for c in codes:
        d = c.replace(".", "")
        parent = None
        for n in range(len(d) - 1, 1, -1):
            if d[:n] in by_digits:
                parent = by_digits[d[:n]]
                break
        out[c] = parent
    return out


async def import_okpd2(
    session: AsyncSession, path: Path, by: str | None, *, activate: bool = True
) -> dict[str, Any]:
    """Загружает редакцию справочника из Excel. Вызывающий делает commit."""
    parsed = await asyncio.to_thread(parse_okpd2_xlsx, path)
    if len(parsed.rows) < MIN_RECORDS:
        raise Okpd2Error(
            "too_few",
            f"В файле найдено только {len(parsed.rows)} кодов: ожидается полный справочник (тысячи записей)",
        )
    warnings: list[str] = []
    if (
        parsed.declared_total
        and abs(parsed.declared_total - len(parsed.rows)) > parsed.declared_total * 0.01
    ):
        warnings.append(
            f"В шапке файла указано {parsed.declared_total} записей, найдено {len(parsed.rows)}"
        )
    if parsed.skipped:
        warnings.append(f"Пропущено строк с неверным кодом или пустым названием: {parsed.skipped}")
    if parsed.duplicates:
        warnings.append(f"Пропущено повторов кода: {parsed.duplicates}")
    same = await session.scalar(
        select(Okpd2Edition).where(
            Okpd2Edition.title == parsed.title, Okpd2Edition.records == len(parsed.rows)
        )
    )
    if same is not None:
        if activate and not same.is_active:
            await session.execute(
                update(Okpd2Edition).where(Okpd2Edition.is_active).values(is_active=False)
            )
            same.is_active = True
        return {
            "edition_id": same.id,
            "title": same.title,
            "records": same.records,
            "status": "unchanged",
            "warnings": warnings,
        }

    ed = Okpd2Edition(
        title=parsed.title,
        note=parsed.note,
        effective_date=parsed.effective_date,
        records=len(parsed.rows),
        declared_total=parsed.declared_total,
        imported_by=by,
        is_active=False,
    )
    session.add(ed)
    await session.flush()
    parents = _parents([c for c, _ in parsed.rows])
    batch: list[dict[str, Any]] = []
    by_level: dict[int, int] = {}
    for code, name in parsed.rows:
        level = LEVELS.get(len(code.replace(".", "")), 6)
        by_level[level] = by_level.get(level, 0) + 1
        batch.append(
            {
                "edition_id": ed.id,
                "code": code,
                "name": name,
                "level": level,
                "parent_code": parents[code],
            }
        )
        if len(batch) >= 3000:
            await session.execute(insert(Okpd2), batch)
            batch = []
    if batch:
        await session.execute(insert(Okpd2), batch)
    if activate:
        await session.execute(
            update(Okpd2Edition).where(Okpd2Edition.is_active).values(is_active=False)
        )
        ed.is_active = True
    await session.flush()
    return {
        "edition_id": ed.id,
        "title": ed.title,
        "effective_date": parsed.effective_date.isoformat() if parsed.effective_date else None,
        "records": ed.records,
        "by_level": by_level,
        "status": "imported",
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Поиск
# ---------------------------------------------------------------------------


async def active_edition(session: AsyncSession) -> Okpd2Edition | None:
    return await session.scalar(select(Okpd2Edition).where(Okpd2Edition.is_active))


async def get_code(
    session: AsyncSession, code: str, *, edition_id: int | None = None
) -> Okpd2 | None:
    code = normalize_code(code)
    if not code:
        return None
    if edition_id is None:
        ed = await active_edition(session)
        if ed is None:
            return None
        edition_id = ed.id
    return await session.scalar(
        select(Okpd2).where(Okpd2.edition_id == edition_id, Okpd2.code == code)
    )


async def chain(session: AsyncSession, code: str) -> list[Okpd2]:
    """Код и его предки от класса до кода: так видно, что означает каждый уровень."""
    row = await get_code(session, code)
    out: list[Okpd2] = []
    while row is not None:
        out.append(row)
        row = (
            await session.scalar(
                select(Okpd2).where(
                    Okpd2.edition_id == row.edition_id, Okpd2.code == row.parent_code
                )
            )
            if row.parent_code
            else None
        )
    return list(reversed(out))


async def search(
    session: AsyncSession, query: str, *, limit: int = 10, full_only: bool = True
) -> list[tuple[Okpd2, float]]:
    """Коды, в названии которых есть слова запроса (русская морфология), лучшие первыми."""
    ed = await active_edition(session)
    tsq_text = tsquery_text(query)
    if ed is None or not tsq_text:
        return []
    tsq = func.to_tsquery("russian", tsq_text)
    rank = func.ts_rank_cd(Okpd2.name_tsv, tsq)
    stmt = (
        select(Okpd2, rank)
        .where(Okpd2.edition_id == ed.id, Okpd2.name_tsv.op("@@")(tsq))
        .order_by(rank.desc(), Okpd2.code)
        .limit(limit)
    )
    if full_only:
        stmt = stmt.where(Okpd2.level >= 5)
    return [(o, float(r)) for o, r in (await session.execute(stmt)).all()]


def describe(edition: Okpd2Edition) -> dict[str, Any]:
    return {
        "id": edition.id,
        "title": edition.title,
        "effective_date": edition.effective_date.isoformat() if edition.effective_date else None,
        "records": edition.records,
        "imported_by": edition.imported_by,
        "imported_at": edition.imported_at.isoformat()
        if isinstance(edition.imported_at, datetime)
        else None,
    }
