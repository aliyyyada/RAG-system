"""Получение прав на документы у источников и кэш прав.

Пример. Иванова (10234) открывает «Документы»:
1. Смотрим access_cache: ответ «Документооборота» получен 2 минуты назад — свежий,
   источник не спрашиваем. Ответ «Архива ОТ» устарел — спрашиваем
   `GET /access/users/10234` (все устаревшие источники — параллельно, с таймаутом).
2. Архив ответил — переписываем его список id в access_cache_ids.
   Не ответил — запоминаем это на ACCESS_RETRY_SECONDS и документы архива не показываем,
   а пользователь видит «Документы из системы «Архив ОТ» сейчас недоступны» (fail-closed).
3. Результат — DocumentAccess: по нему filters.visible() строит условие для SQL.

Кэш сбрасывается при входе человека (reset_cache(user_id)) и кнопкой в админке.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import permissions as perms
from app.auth.rbac import Principal
from app.config import Settings
from app.db import get_sessionmaker
from app.models import AccessCache, Source
from app.sources import open_source
from app.sources.base import SourceError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class UnavailableSource:
    id: int
    name: str


@dataclass(frozen=True)
class DocumentAccess:
    """Что человеку можно видеть из документов.

    common      — общие документы (у гостя только они);
    user_id     — чей кэш прав источников и чьи выдачи ручных загрузок учитывать
                  (None — гость или у человека нет права documents.view);
    sources     — источники, чей ответ о правах сейчас есть в кэше (только по ним
                  показываем документы «по правам»);
    unavailable — источники, которые не ответили: их документы скрыты, нужно предупреждение.
    """

    common: bool
    user_id: int | None = None
    sources: frozenset[int] = frozenset()
    unavailable: tuple[UnavailableSource, ...] = field(default_factory=tuple)

    @property
    def any(self) -> bool:
        return self.common or self.user_id is not None


@dataclass(frozen=True)
class _Fetched:
    source_id: int
    ids: set[str] | None
    error: str | None


def _now() -> datetime:
    return datetime.now(UTC)


async def _fetch(source: Source, personnel_number: str, settings: Settings) -> _Fetched:
    adapter = open_source(source, settings)
    try:
        ids = await asyncio.wait_for(
            adapter.user_document_ids(personnel_number), settings.access_timeout_seconds
        )
        return _Fetched(source.id, ids, None)
    except TimeoutError:
        error = f"нет ответа за {settings.access_timeout_seconds:g} с"
    except SourceError as exc:
        error = str(exc) or type(exc).__name__
    except Exception as exc:  # noqa: BLE001 — любая ошибка адаптера = источник недоступен
        log.exception("Права из источника %s: неожиданная ошибка", source.code)
        error = type(exc).__name__
    finally:
        await adapter.aclose()
    # Табельный номер в лог не пишем лишний раз: достаточно источника и причины.
    log.warning("Источник %s не ответил на запрос прав: %s", source.code, error)
    return _Fetched(source.id, None, error)


async def _store(user_id: int, results: list[_Fetched], settings: Settings) -> None:
    """Записывает ответы источников в кэш отдельной короткой транзакцией."""
    now = _now()
    async with get_sessionmaker()() as s, s.begin():
        # Два одновременных запроса одного человека не должны перемешать списки id.
        await s.execute(text("SELECT pg_advisory_xact_lock(3, :u)"), {"u": user_id})
        for r in results:
            ok = r.ids is not None
            ttl = (
                timedelta(minutes=settings.access_cache_minutes)
                if ok
                else timedelta(seconds=settings.access_retry_seconds)
            )
            values = {
                "user_id": user_id,
                "source_id": r.source_id,
                "fetched_at": now,
                "expires_at": now + ttl,
                "ok": ok,
                "id_count": len(r.ids or ()),
                "error": r.error,
            }
            await s.execute(
                insert(AccessCache)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["user_id", "source_id"],
                    set_={k: v for k, v in values.items() if k not in ("user_id", "source_id")},
                )
            )
            await s.execute(
                text("DELETE FROM access_cache_ids WHERE user_id = :u AND source_id = :s"),
                {"u": user_id, "s": r.source_id},
            )
            if r.ids:
                await s.execute(
                    text(
                        "INSERT INTO access_cache_ids (user_id, source_id, external_id) "
                        "SELECT :u, :s, unnest(CAST(:ids AS varchar[])) ON CONFLICT DO NOTHING"
                    ),
                    {"u": user_id, "s": r.source_id, "ids": sorted(r.ids)},
                )


async def resolve_access(
    session: AsyncSession, principal: Principal, settings: Settings
) -> DocumentAccess:
    """Права человека на документы; при необходимости опрашивает источники."""
    common = principal.has(perms.SEARCH_COMMON) or principal.has(perms.DOCUMENTS_VIEW)
    user = principal.user
    if user is None or not principal.has(perms.DOCUMENTS_VIEW):
        return DocumentAccess(common=common)

    sources = list(await session.scalars(select(Source).where(Source.enabled).order_by(Source.id)))
    cached = {
        c.source_id: c
        for c in await session.scalars(select(AccessCache).where(AccessCache.user_id == user.id))
    }
    now = _now()
    stale = [s for s in sources if (c := cached.get(s.id)) is None or c.expires_at <= now]
    ok_ids = {s.id for s in sources if s not in stale and cached[s.id].ok}
    if stale:
        results = await asyncio.gather(*(_fetch(s, user.personnel_number, settings) for s in stale))
        await _store(user.id, list(results), settings)
        ok_ids |= {r.source_id for r in results if r.ids is not None}
    unavailable = tuple(UnavailableSource(s.id, s.name) for s in sources if s.id not in ok_ids)
    return DocumentAccess(
        common=True, user_id=user.id, sources=frozenset(ok_ids), unavailable=unavailable
    )


async def reset_cache(session: AsyncSession, user_id: int | None = None) -> int:
    """Сбрасывает кэш прав одного человека или всех. Возвращает число сброшенных записей."""
    stmt = delete(AccessCache)
    if user_id is not None:
        stmt = stmt.where(AccessCache.user_id == user_id)
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0)
