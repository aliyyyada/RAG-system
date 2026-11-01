"""Оценка качества поиска: recall@k и MRR на наборе вопросов с ожидаемыми документами.

Файл с вопросами (пример — backend/eval/questions.json):

    {"as_user": "T-0002",
     "questions": [{"question": "Кто подписывает наряд-допуск?", "expected_numbers": ["112"]}]}

recall@5 = доля ожидаемых документов, попавших в первые 5 найденных документов (по номеру
документа), усреднённая по вопросам. MRR — средняя обратная позиция первого правильного.
Ищем от имени `as_user` — с его правами, как настоящий пользователь.

Запуск: `python -m app.cli eval-search --file eval/questions.json` — до и после изменения
поиска (другой размер фрагмента, реранкер, веса), чтобы видеть, стало лучше или хуже.

Этап 5: у вопроса может быть `"kind": "graph"` — вопрос о связях («что отменило приказ 45»).
`--mode all` сравнивает режимы chunks / graph / mixed: общий recall и recall отдельно
по обычным вопросам и по вопросам о связях.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.service import resolve_access
from app.auth.rbac import principal_for_user
from app.config import Settings
from app.models import Document, User
from app.search.hybrid import SearchFilters
from app.search.service import retrieve

KS = (1, 3, 5, 10)


@dataclass
class QuestionResult:
    question: str
    expected: list[str]
    found: list[str]
    recall: dict[int, float]
    rank: int | None
    kind: str = "text"


def _summary(results: list[QuestionResult]) -> dict[str, Any]:
    n = len(results) or 1
    return {
        "questions": len(results),
        "recall": {k: round(sum(r.recall[k] for r in results) / n, 3) for k in KS},
        "mrr": round(sum(1 / r.rank for r in results if r.rank) / n, 3),
    }


async def evaluate(
    session: AsyncSession, data: dict[str, Any], settings: Settings, mode: str | None = None
) -> dict[str, Any]:
    login = data.get("as_user")
    user = await session.scalar(select(User).where(User.personnel_number == login))
    if user is None:
        raise LookupError(f"Нет пользователя {login!r} (создайте его или поменяйте as_user)")
    principal = await principal_for_user(session, user, None)
    access = await resolve_access(session, principal, settings)
    numbers = {i: n for i, n in (await session.execute(select(Document.id, Document.number))).all()}

    results: list[QuestionResult] = []
    for item in data["questions"]:
        r = await retrieve(
            session, access, item["question"], SearchFilters(), settings, top_k=max(KS), mode=mode
        )
        docs: list[str] = []
        for c in r.ranked:
            num = numbers.get(c.document_id) or f"#{c.document_id}"
            if num not in docs:
                docs.append(num)
        expected = [str(x) for x in item["expected_numbers"]]
        recall = {k: len(set(expected) & set(docs[:k])) / len(expected) for k in KS}
        rank = next((i for i, d in enumerate(docs, start=1) if d in expected), None)
        results.append(
            QuestionResult(
                item["question"], expected, docs[:10], recall, rank, item.get("kind", "text")
            )
        )

    return {
        "mode": mode or settings.search_mode,
        **_summary(results),
        "by_kind": {
            kind: _summary([r for r in results if r.kind == kind])
            for kind in sorted({r.kind for r in results})
        },
        "misses": [
            {"question": r.question, "expected": r.expected, "found": r.found}
            for r in results
            if r.recall[5] < 1
        ],
    }


def load_questions(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
