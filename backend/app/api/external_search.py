"""API смыслового поиска для внешних систем (программа заказчика с поиском по ОРД).

    POST /api/integration/v1/search
    X-API-Key: <ключ из .env, EXTERNAL_SEARCH_API_KEY>
    {"id_userboss": "123456", "query": "работы на высоте наряд-допуск", "limit": 50}

    200 → {"ids": ["7C1D…", "A9F0…"], "unavailable_sources": [], "reranked": false,
           "fulltext_only": false}

Что происходит внутри:
1. Проверяем ключ. Ключ не задан в .env — API выключено (503).
2. Спрашиваем у каждого включённого источника, какие документы видит этот ID_UserBoss
   (для ОРД это процедура с @PrKor='Документы (доступ)' — тот же запрос, что и для
   пользователей нашего сайта). Ответ помним ACCESS_CACHE_MINUTES минут в памяти процесса.
   Источник не ответил — его документы не ищем; не ответил ни один — 503 (fail-closed).
3. Гибридный поиск (вектор bge-m3 «по смыслу» + полнотекстовый с русской морфологией)
   только среди разрешённых документов: условие прав стоит прямо в SQL-запросе.
4. Реранкер (если включён), затем фрагменты сворачиваем в документы: место документа =
   место его лучшего фрагмента. Отдаём ID_GUID документов в источнике (Document.external_id).
"""

import asyncio
import hmac
import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from sqlalchemy import (
    ColumnElement,
    Float,
    String,
    and_,
    any_,
    bindparam,
    func,
    literal,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.filters import alive
from app.auth.deps import SessionDep
from app.auth.service import write_audit
from app.config import Settings, get_settings
from app.models import EMBEDDING_DIM, Chunk, Document, Source, Vector
from app.search.hybrid import _has_iterative_scan, rrf_fuse, tsquery_text
from app.search.rerank import get_reranker
from app.search.service import embed_query
from app.sources import open_source
from app.sources.base import SourceError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/integration/v1", tags=["интеграция: поиск для внешних систем"])
_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


# ---------- Запрос и ответ ----------


class ExternalSearchRequest(BaseModel):
    id_userboss: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[0-9A-Za-z_.\-]+$",
        description="ID_UserBoss пользователя: по нему источник отдаёт список доступных документов",
    )
    query: str = Field(min_length=1, max_length=1000, description="Текст запроса")
    limit: int | None = Field(
        default=None, ge=1, le=500, description="Сколько документов вернуть максимум"
    )


class ExternalSearchResponse(BaseModel):
    ids: list[str] = Field(description="ID_GUID документов, лучшие первыми")
    unavailable_sources: list[str] = Field(
        default_factory=list,
        description="Источники, которые не ответили о правах: их документов в выдаче нет",
    )
    reranked: bool = Field(description="Порядок уточнён реранкером")
    fulltext_only: bool = Field(
        description="Векторный поиск недоступен (Ollama не ответила): искали только по словам"
    )


# ---------- Ключ доступа ----------


def _check_key(key: Annotated[str | None, Depends(_key_header)]) -> None:
    expected = get_settings().external_search_api_key.get_secret_value()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail={"code": "disabled", "message": "API поиска выключено (нет ключа в настройках)"},
        )
    if not key or not hmac.compare_digest(key.encode(), expected.encode()):
        raise HTTPException(
            status_code=401, detail={"code": "bad_key", "message": "Неверный ключ API"}
        )


KeyAuth = Annotated[None, Depends(_check_key)]


# ---------- Права: какие документы видит ID_UserBoss ----------

# (id источника, ID_UserBoss) → (до какого момента верим ответу, guid'ы документов).
# Кэш в памяти процесса: после перезапуска backend просто спросим источник ещё раз.
_rights_cache: dict[tuple[int, str], tuple[float, frozenset[str]]] = {}
_CACHE_MAX = 5000


def reset_rights_cache() -> None:
    _rights_cache.clear()


async def _allowed_ids(source: Source, user: str, settings: Settings) -> frozenset[str] | None:
    """guid'ы документов источника, доступные человеку; None — источник не ответил."""
    key = (source.id, user)
    now = time.monotonic()
    hit = _rights_cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    adapter = None
    try:
        adapter = open_source(source, settings)
        ids = await asyncio.wait_for(
            adapter.user_document_ids(user), settings.access_timeout_seconds
        )
    except TimeoutError:
        log.warning(
            "Источник %s: нет ответа о правах за %s с", source.code, settings.access_timeout_seconds
        )
        return None
    except SourceError as exc:
        log.warning("Источник %s не ответил о правах: %s", source.code, exc)
        return None
    except Exception:  # noqa: BLE001 — любая ошибка адаптера = источник недоступен
        log.exception("Источник %s: неожиданная ошибка при запросе прав", source.code)
        return None
    finally:
        if adapter is not None:
            await adapter.aclose()
    value = frozenset(ids)
    if settings.access_cache_minutes > 0:
        if len(_rights_cache) >= _CACHE_MAX:
            for k in [k for k, (exp, _) in _rights_cache.items() if exp <= now]:
                del _rights_cache[k]
            if len(_rights_cache) >= _CACHE_MAX:
                _rights_cache.clear()
        _rights_cache[key] = (now + settings.access_cache_minutes * 60, value)
    return value


def _scope(allowed: dict[int, frozenset[str]]) -> ColumnElement[bool]:
    """Условие SQL: документ живой и его guid есть в списке разрешённых у его источника.

    Список передаётся одним параметром-массивом (`external_id = ANY(:ids)`), а не тысячами
    отдельных параметров в IN (...): у драйвера asyncpg предел 32767 параметров на запрос.
    """
    parts = [
        and_(
            Document.source_id == sid,
            Document.external_id
            == any_(bindparam(f"allowed_{sid}", sorted(ids), type_=ARRAY(String))),
        )
        for sid, ids in allowed.items()
        if ids
    ]
    return and_(alive(), or_(*parts))


# ---------- Поиск ----------


async def _vector_hits(
    session: AsyncSession, scope: ColumnElement[bool], vector: list[float], limit: int
) -> list[tuple[int, float]]:
    await session.execute(text("SET LOCAL hnsw.ef_search = 100"))
    if await _has_iterative_scan(session):
        await session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    distance = Chunk.embedding.op("<=>", return_type=Float())(
        literal(vector, Vector(EMBEDDING_DIM))
    )
    rows = await session.execute(
        select(Chunk.id, distance.label("dist"))
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.embedding.is_not(None), scope)
        .order_by(distance)
        .limit(limit)
    )
    return [(int(cid), float(dist)) for cid, dist in rows.all()]


async def _text_hits(
    session: AsyncSession, scope: ColumnElement[bool], query: str, limit: int
) -> list[int]:
    tsq_text = tsquery_text(query)
    if not tsq_text:
        return []
    tsq = func.to_tsquery("russian", tsq_text)
    rows = await session.execute(
        select(Chunk.id)
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.tsv.op("@@")(tsq), scope)
        .order_by(func.ts_rank_cd(Chunk.tsv, tsq).desc(), Chunk.id)
        .limit(limit)
    )
    return [int(cid) for cid in rows.scalars()]


@router.post(
    "/search",
    response_model=ExternalSearchResponse,
    summary="Смысловой поиск документов от имени пользователя (по ID_UserBoss)",
)
async def external_search(
    body: ExternalSearchRequest, request: Request, session: SessionDep, _: KeyAuth
) -> ExternalSearchResponse:
    settings = get_settings()
    user = body.id_userboss
    limit = body.limit or settings.external_search_limit

    # 1. Права: все включённые источники параллельно, каждый со своим таймаутом.
    sources = list(await session.scalars(select(Source).where(Source.enabled).order_by(Source.id)))
    answers = await asyncio.gather(*(_allowed_ids(s, user, settings) for s in sources))
    allowed = {s.id: ids for s, ids in zip(sources, answers, strict=True) if ids is not None}
    unavailable = [s.name for s, ids in zip(sources, answers, strict=True) if ids is None]
    if not allowed:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "rights_unavailable",
                "message": "Источник прав доступа не ответил, поиск невозможен",
            },
        )

    ids: list[str] = []
    reranked = False
    vector = None
    if any(allowed.values()):
        scope = _scope(allowed)
        # Кандидатов берём с запасом: у одного документа обычно несколько фрагментов.
        candidates = min(max(settings.search_candidates, limit * 3), 300)

        # 2. Гибридный поиск. Нет вектора (Ollama недоступна) — ищем только по словам.
        vectors = await embed_query([body.query], settings)
        vector = vectors[0] if vectors else None
        vec_hits = (
            await _vector_hits(session, scope, vector, candidates) if vector is not None else []
        )
        txt_hits = await _text_hits(session, scope, body.query, candidates)
        ranked = rrf_fuse(vec_hits, txt_hits, settings.search_rrf_k)

        # 3. Фрагмент → guid документа (и текст, если нужен реранкеру).
        reranker = get_reranker(settings)
        top = ranked[: settings.rerank_top_n] if reranker is not None else ranked
        rows = (
            await session.execute(
                select(Chunk.id, Chunk.text, Document.external_id)
                .join(Document, Document.id == Chunk.document_id)
                .where(Chunk.id.in_([c.chunk_id for c in top]), scope)
            )
        ).all()
        guid = {int(cid): str(ext) for cid, _, ext in rows}
        order = [c.chunk_id for c in top if c.chunk_id in guid]
        if reranker is not None and order:
            chunk_text = {int(cid): str(t) for cid, t, _ in rows}
            try:
                scores = await reranker.score(body.query, [chunk_text[c] for c in order])
                pairs = sorted(zip(order, scores, strict=True), key=lambda p: -p[1])
                order = [c for c, sc in pairs if sc >= settings.rerank_min_score]
                reranked = True
            except Exception:  # noqa: BLE001 — реранкер не должен ронять поиск
                log.exception("Ошибка реранкера, используем порядок RRF")

        # 4. Фрагменты → документы, без повторов, лучшие первыми.
        for cid in order:
            g = guid[cid]
            if g not in ids:
                ids.append(g)
                if len(ids) >= limit:
                    break

    # Аудит: кто (ID_UserBoss), что спросил, какие документы ушли в ответ.
    await write_audit(
        session,
        "integration.search",
        target_type="id_userboss",
        target_id=user,
        details={"query": body.query[:500], "found": len(ids), "ids": ids[:50]},
        ip=request.client.host if request.client else None,
    )
    await session.commit()

    return ExternalSearchResponse(
        ids=ids,
        unavailable_sources=unavailable,
        reranked=reranked,
        fulltext_only=vector is None and any(allowed.values()),
    )
