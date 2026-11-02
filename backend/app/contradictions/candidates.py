"""Кандидаты в противоречия — без модели, только SQL и векторы.

Модель на GTX 1070 тратит ~5–8 секунд на пару, поэтому в неё уходят не все пары
фрагментов (их миллионы), а только «подозрительные»:

1. **Нормативные фрагменты.** В фрагменте есть требование: «не более», «не реже»,
   «обязан», «запрещается», «раз в…», «срок». Описательный текст («Положение определяет
   порядок…») и списки подписей противоречить не могут.
2. **Похожие по смыслу** (vector). Для каждого нормативного фрагмента документа берём
   CONTRA_NEIGHBORS ближайших нормативных фрагментов ДРУГИХ документов по вектору bge-m3,
   не ближе порога CONTRA_MIN_SIMILARITY. Пример: п. 3.1 Положения о СУОТ («проверка знаний
   … не реже одного раза в год») и таблица п. 4.4 приказа № 112 («1 раз в 3 года»).
3. **Связанные графом** (graph). Фрагменты других документов, где упомянута та же сущность
   (вид работ, оборудование, участок, должность, подразделение), и фрагменты документов,
   связанных с этим ссылкой («во исполнение», «в соответствии с»). Их берём с более мягким
   порогом близости: связь в графе уже говорит, что тексты об одном.

Отменённые документы в кандидаты не попадают: противоречие с ними не активно, а время
видеокарты дорого. Если документ перестанет быть отменённым, его проверят заново
(app/graph/status.py сбрасывает documents.contra_version).

Права здесь не учитываются: это фоновая задача, которая видит всё. Фильтр по правам —
при показе (app/api/contradictions.py): пара видна, только если доступны оба документа.
"""

import hashlib
import re
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.graph.extract import strip_lists
from app.graph.status import CANCELLED

# Признаки требования. Основы слов, без учёта регистра.
_NORMATIVE = re.compile(
    r"\bдолж(?:ен|на|ны|но)\b|обязан|запрещ|не\s+допуска|допуска(?:ет|ют)ся|разреша"
    r"|не\s+(?:более|менее|реже|позднее|чаще|ранее|выше|ниже)\b|в\s+течение\b|\bраз(?:а)?\s+в\b"
    r"|ежесменн|ежедневн|еженедельн|ежемесячн|ежекварт|ежегодн|периодичн|\bсрок\w*"
    r"|выда[её]тся|проводит(?:ся|ь)|проводятся|осуществля\w*ся|необходимо|следует",
    re.IGNORECASE,
)
# Типы сущностей, по которым пара «связана графом». Люди и сроки не годятся: «Иванова А. С.»
# упомянута в десятках документов о разном, а «1 раз в год» — вообще не тема.
GRAPH_ENTITY_TYPES = ("work", "equipment", "site", "position", "department", "norm")
MIN_CHARS = 60


def prepare(body: str) -> str:
    """Текст фрагмента для проверки: без списков подписей и лишних пробелов."""
    return re.sub(r"[ \t]+", " ", strip_lists(body)).strip()


def is_normative(body: str) -> bool:
    s = prepare(body)
    return len(s) >= MIN_CHARS and _NORMATIVE.search(s) is not None


def text_key(body: str) -> str:
    """Ключ кэша: хэш текста без различий в пробелах и регистре."""
    norm = re.sub(r"\s+", " ", prepare(body)).strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


@dataclass
class Candidate:
    """Пара фрагментов, которую стоит показать модели. a/b упорядочены по ключу текста."""

    chunk_a: int
    chunk_b: int
    document_a: int
    document_b: int
    key_a: str
    key_b: str
    text_a: str
    text_b: str
    origin: str  # vector | graph
    similarity: float


def make_pair(
    c1: tuple[int, int, str], c2: tuple[int, int, str], origin: str, similarity: float
) -> Candidate | None:
    """(chunk_id, document_id, text) × 2 → пара в каноническом порядке (или None)."""
    k1, k2 = text_key(c1[2]), text_key(c2[2])
    if k1 == k2 or c1[1] == c2[1]:
        return None  # одинаковый текст (копия пункта) — не противоречие; один документ — тоже
    if k2 < k1:
        c1, c2, k1, k2 = c2, c1, k2, k1
    return Candidate(
        chunk_a=c1[0],
        chunk_b=c2[0],
        document_a=c1[1],
        document_b=c2[1],
        key_a=k1,
        key_b=k2,
        text_a=prepare(c1[2]),
        text_b=prepare(c2[2]),
        origin=origin,
        similarity=round(similarity, 4),
    )


# Документ-кандидат «живой» и не отменён (:cancelled = «Отменён»).
_VECTOR_SQL = text(
    """
    SELECT c.id, c.document_id, c.text, 1 - (c.embedding <=> s.embedding) AS sim
    FROM chunks s
    CROSS JOIN LATERAL (
        SELECT c.id, c.document_id, c.text, c.embedding
        FROM chunks c JOIN documents d ON d.id = c.document_id
        WHERE c.document_id <> s.document_id AND c.embedding IS NOT NULL
          AND d.deleted_at IS NULL AND NOT d.is_excluded AND d.indexed_version IS NOT NULL
          AND d.legal_status IS DISTINCT FROM :cancelled
        ORDER BY c.embedding <=> s.embedding
        LIMIT :pool
    ) c
    WHERE s.id = :chunk_id
    ORDER BY sim DESC
    """
)

# Фрагменты, связанные с данным графом: общая сущность нужного типа или связь документов.
_GRAPH_SQL = text(
    """
    WITH related AS (
        SELECT m2.chunk_id AS id
        FROM entity_mentions m1
        JOIN entities e ON e.id = m1.entity_id AND e.type = ANY(:types)
        JOIN entity_mentions m2 ON m2.entity_id = m1.entity_id
                               AND m2.document_id <> m1.document_id
        WHERE m1.chunk_id = :chunk_id
        UNION
        SELECT c.id FROM document_links l
        JOIN chunks c ON c.document_id = CASE WHEN l.from_document_id = :doc
                                              THEN l.target_document_id
                                              ELSE l.from_document_id END
        WHERE l.kind IN ('refers', 'executes')
          AND (l.from_document_id = :doc OR l.target_document_id = :doc)
          AND l.target_document_id IS NOT NULL
    )
    SELECT c.id, c.document_id, c.text, 1 - (c.embedding <=> s.embedding) AS sim
    FROM related r
    JOIN chunks c ON c.id = r.id
    JOIN documents d ON d.id = c.document_id
    JOIN chunks s ON s.id = :chunk_id
    WHERE c.document_id <> :doc AND c.embedding IS NOT NULL
      AND d.deleted_at IS NULL AND NOT d.is_excluded AND d.indexed_version IS NOT NULL
      AND d.legal_status IS DISTINCT FROM :cancelled
    ORDER BY sim DESC
    LIMIT :pool
    """
)


async def find_candidates(
    session: AsyncSession, document_id: int, settings: Settings
) -> list[Candidate]:
    """Все пары-кандидаты для документа: его нормативные фрагменты × фрагменты других."""
    own = (
        await session.execute(
            text(
                "SELECT id, document_id, text FROM chunks "
                "WHERE document_id = :d AND embedding IS NOT NULL ORDER BY ord"
            ),
            {"d": document_id},
        )
    ).all()
    # HNSW ищет ближайших среди всех фрагментов, условие на документ отбрасывает лишних.
    await session.execute(text("SET LOCAL hnsw.ef_search = 100"))
    pairs: dict[tuple[int, int], Candidate] = {}
    for chunk_id, doc_id, body in own:
        if not is_normative(body):
            continue
        me = (int(chunk_id), int(doc_id), str(body))
        taken = 0
        rows = (
            await session.execute(
                _VECTOR_SQL,
                {
                    "chunk_id": chunk_id,
                    "pool": settings.contra_neighbors * 4,
                    "cancelled": CANCELLED,
                },
            )
        ).all()
        for cid, did, other, sim in rows:
            if taken >= settings.contra_neighbors or sim < settings.contra_min_similarity:
                break
            if not is_normative(other):
                continue
            pair = make_pair(me, (int(cid), int(did), str(other)), "vector", float(sim))
            if pair is not None:
                pairs.setdefault((pair.chunk_a, pair.chunk_b), pair)
                taken += 1
        if settings.contra_graph_neighbors <= 0:
            continue
        rows = (
            await session.execute(
                _GRAPH_SQL,
                {
                    "chunk_id": chunk_id,
                    "doc": document_id,
                    "types": list(GRAPH_ENTITY_TYPES),
                    "pool": settings.contra_graph_neighbors * 4,
                    "cancelled": CANCELLED,
                },
            )
        ).all()
        taken = 0
        for cid, did, other, sim in rows:
            if (
                taken >= settings.contra_graph_neighbors
                or sim < settings.contra_graph_min_similarity
            ):
                break
            if not is_normative(other):
                continue
            pair = make_pair(me, (int(cid), int(did), str(other)), "graph", float(sim))
            if pair is not None and (pair.chunk_a, pair.chunk_b) not in pairs:
                pairs[(pair.chunk_a, pair.chunk_b)] = pair
                taken += 1
    return list(pairs.values())
