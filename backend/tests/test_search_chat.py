"""Этап 4: гибридный поиск и чат с источниками.

Главное правило то же, что на этапе 3: человек не видит чужой документ — ни в результатах
поиска, ни в источниках ответа, ни в самом ответе, ни в промпте модели (текст закрытого
документа не должен попасть в LLM даже как «контекст»).

Документы фейкового «Документооборота» (fakes/fake_source.py), кто что видит:
    общие: ИОТ-044 (электросварщик), Положение о СУОТ — все, включая гостя;
    T-0001: + приказ 112 (наряд-допуск, работы на высоте), приказ 140, регламент ТО;
    T-0002: всё, в том числе приказ 45 (отменён; «ответственный — главный инженер Смирнов»).
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text

from app.chat.llm_queue import PRIORITY_GUEST, PRIORITY_USER, LlmQueue
from app.chat.prompt import NO_ANSWER, OFF_TOPIC
from app.config import get_settings
from app.db import get_sessionmaker
from app.models import AuditLog, Chat, Chunk
from app.search.indexer import handle_embed
from app.search.rerank import set_reranker_override
from app.worker.queue import ClaimedJob, JobDeferred
from fakes.fake_ollama import FakeOllamaState
from fakes.fake_source import FakeSource
from tests.helpers import auth, login_ok, seed_test_users
from tests.test_ingest_sync import docs_by_key, first_load

pytestmark = pytest.mark.db

EVAL_FILE = Path(__file__).resolve().parent.parent / "eval" / "questions.json"

# Текст, который есть только в закрытом для T-0001 приказе № 45 (скан, OCR).
SECRET_45 = "Смирнова"


@pytest.fixture
async def ids(client: httpx.AsyncClient, docflow: int, fake_source: FakeSource) -> dict[str, int]:
    await seed_test_users()
    await first_load(docflow)
    return {k: d.id for k, d in (await docs_by_key(fake_source)).items()}


def parse_sse(body: str) -> list[tuple[str, Any]]:
    """Разбор потока SSE: [(событие, данные)], heartbeat-комментарии — как ("ping", None)."""
    events: list[tuple[str, Any]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        if block.startswith(": ping"):
            events.append(("ping", None))
            continue
        name, data = "message", []
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data.append(line[6:])
        events.append((name, json.loads("\n".join(data)) if data else None))
    return events


async def ask(
    client: httpx.AsyncClient, question: str, headers: Any = None, **extra: Any
) -> dict[str, Any]:
    r = await client.post(
        "/api/chat/ask", json={"question": question, **extra}, headers=headers, timeout=60
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    by: dict[str, Any] = {}
    for name, data in events:
        if name in ("meta", "sources", "done", "error"):
            by[name] = data
    by["events"] = events
    by["tokens"] = "".join(d["text"] for n, d in events if n == "token")
    return by


def source_docs(res: dict[str, Any]) -> set[int]:
    return {s["document_id"] for s in res["sources"]["items"]}


def all_prompts(fake: FakeOllamaState) -> str:
    return "\n".join(m["content"] for p in fake.prompts for m in p)


# ---------------------------------------------------------------------------
# Векторизация
# ---------------------------------------------------------------------------


async def test_chunks_are_embedded_after_indexing(ids: dict[str, int]) -> None:
    async with get_sessionmaker()() as s:
        total, embedded = (
            await s.execute(text("SELECT count(*), count(embedding) FROM chunks"))
        ).one()
        dims = await s.scalar(text("SELECT vector_dims(embedding) FROM chunks LIMIT 1"))
    assert total > 0 and embedded == total
    assert dims == 1024


async def test_embedding_waits_while_user_waits_for_answer(ids: dict[str, int]) -> None:
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(text("UPDATE chunks SET embedding = NULL"))
    q = LlmQueue(PRIORITY_USER, 30)
    await q.enter()
    try:
        job = ClaimedJob(1, "chunk_embed", {"document_id": ids["prikaz-112"]}, 1, 5)
        with pytest.raises(JobDeferred):
            await handle_embed(job, get_settings(), get_sessionmaker())
    finally:
        await q.leave()
    await handle_embed(job, get_settings(), get_sessionmaker())
    async with get_sessionmaker()() as s:
        left = await s.scalar(
            select(text("count(*)"))
            .select_from(Chunk)
            .where(Chunk.document_id == ids["prikaz-112"], Chunk.embedding.is_(None))
        )
    assert left == 0


async def test_ollama_down_defers_embedding(ids: dict[str, int], fake_ollama: FakeOllamaState):  # type: ignore[no-untyped-def]
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(text("UPDATE chunks SET embedding = NULL"))
    fake_ollama.unavailable = True
    job = ClaimedJob(1, "chunk_embed", {"document_id": ids["iot-044"]}, 1, 5)
    with pytest.raises(JobDeferred) as exc:
        await handle_embed(job, get_settings(), get_sessionmaker())
    assert exc.value.seconds == 60


# ---------------------------------------------------------------------------
# Поиск: только доступные документы
# ---------------------------------------------------------------------------


async def _search(client: httpx.AsyncClient, query: str, headers: Any = None, **filters: Any):  # type: ignore[no-untyped-def]
    r = await client.post(
        "/api/search", json={"query": query, "filters": filters, "limit": 20}, headers=headers
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_search_finds_only_visible_documents(
    client: httpx.AsyncClient, ids: dict[str, int]
) -> None:
    query = "наряд-допуск работы на высоте ответственный"
    guest = await _search(client, query)
    assert {i["document_id"] for i in guest["items"]} <= {ids["iot-044"], ids["polozhenie-suot"]}

    user = auth(await login_ok(client, "T-0001"))
    res = await _search(client, query, user)
    docs = {i["document_id"] for i in res["items"]}
    assert ids["prikaz-112"] in docs
    assert ids["prikaz-45"] not in docs
    assert SECRET_45 not in json.dumps(res, ensure_ascii=False)
    # Оба вида поиска участвовали.
    assert res["vector_ok"] is True
    assert any(i["vector_rank"] for i in res["items"]) and any(i["text_rank"] for i in res["items"])

    analyst = auth(await login_ok(client, "T-0002"))
    res = await _search(client, query, analyst)
    assert ids["prikaz-45"] in {i["document_id"] for i in res["items"]}


async def test_search_by_document_number_and_morphology(
    client: httpx.AsyncClient, ids: dict[str, int]
) -> None:
    user = auth(await login_ok(client, "T-0002"))
    res = await _search(client, "ИОТ-044", user)
    assert res["items"][0]["document_id"] == ids["iot-044"]
    # «сварщиков» → «сварщик» (русская морфология полнотекстового поиска)
    res = await _search(client, "обязанности электросварщиков", user)
    assert res["items"][0]["document_id"] == ids["iot-044"]
    # Отрывок с подсветкой найденного слова.
    assert any("⟦" in i["snippet"] for i in res["items"])


async def test_search_filters(client: httpx.AsyncClient, ids: dict[str, int]) -> None:
    user = auth(await login_ok(client, "T-0002"))
    res = await _search(client, "работы на высоте", user, doc_type="Регламент")
    assert {i["document_id"] for i in res["items"]} <= {ids["reglament-to"]}

    res = await _search(client, "работы на высоте наряд", user)
    assert ids["prikaz-45"] in {i["document_id"] for i in res["items"]}
    res = await _search(client, "работы на высоте наряд", user, only_active=True)
    assert ids["prikaz-45"] not in {i["document_id"] for i in res["items"]}

    res = await _search(client, "работы на высоте наряд", user, date_from="2024-01-01")
    assert ids["prikaz-45"] not in {i["document_id"] for i in res["items"]}

    res = await _search(client, "охрана труда", user, source="manual")
    assert res["items"] == []

    r = await client.get("/api/documents/filters", headers=user)
    assert [c["label"] for c in r.json()["collections"]] == ["Документооборот"]


async def test_search_without_vectors_falls_back_to_full_text(
    client: httpx.AsyncClient, ids: dict[str, int], fake_ollama: FakeOllamaState
) -> None:
    fake_ollama.unavailable = True
    user = auth(await login_ok(client, "T-0001"))
    res = await _search(client, "наряд-допуск", user)
    assert res["vector_ok"] is False
    assert ids["prikaz-112"] in {i["document_id"] for i in res["items"]}


# ---------------------------------------------------------------------------
# Чат: ответ потоком, источники, права
# ---------------------------------------------------------------------------


async def test_answer_streams_with_sources_and_citations(
    client: httpx.AsyncClient, ids: dict[str, int], fake_ollama: FakeOllamaState
) -> None:
    user = auth(await login_ok(client, "T-0001"))
    res = await ask(client, "Кто подписывает наряд-допуск на работы на высоте?", user)
    names = [n for n, _ in res["events"]]
    assert names[0] == "meta" and "token" in names and names[-1] == "done"
    assert names.index("sources") < names.index("token")

    done = res["done"]
    assert res["tokens"] == done["answer"]
    assert "подписывает ответственный руководитель" in done["answer"]
    assert done["cited"], done
    cited = [s for s in done["sources"] if s["cited"]]
    assert cited[0]["document_id"] == ids["prikaz-112"]
    assert cited[0]["chunk_id"] > 0 and "стр." in cited[0]["place"]
    assert cited[0]["caption"].startswith("Приказ № 112")

    # Закрытый для T-0001 приказ 45 не попал ни в источники, ни в промпт модели.
    assert ids["prikaz-45"] not in source_docs(res)
    assert SECRET_45 not in all_prompts(fake_ollama)
    assert len(fake_ollama.prompts) == 1


async def test_guest_gets_answers_only_from_common_documents(
    client: httpx.AsyncClient, ids: dict[str, int], fake_ollama: FakeOllamaState
) -> None:
    res = await ask(client, "Какие СИЗ выдаются электросварщику?")
    assert source_docs(res) <= {ids["iot-044"], ids["polozhenie-suot"]}
    assert res["done"]["cited"]
    assert res["meta"]["chat_id"] is None  # гостевой чат на сервере не хранится

    fake_ollama.prompts.clear()
    res = await ask(client, "Кто подписывает наряд-допуск на работы на высоте?")
    assert source_docs(res) <= {ids["iot-044"], ids["polozhenie-suot"]}
    assert "наряду-допуску" not in all_prompts(fake_ollama)
    assert SECRET_45 not in all_prompts(fake_ollama)

    async with get_sessionmaker()() as s:
        rows = (await s.scalars(select(AuditLog).where(AuditLog.action == "chat.question"))).all()
    assert len(rows) == 2 and rows[0].actor_user_id is None and rows[0].ip
    assert rows[0].details["question"] == "Какие СИЗ выдаются электросварщику?"
    assert rows[0].details["cited_documents"] == [ids["iot-044"]]


async def test_off_topic_and_unknown_questions(
    client: httpx.AsyncClient, ids: dict[str, int]
) -> None:
    user = auth(await login_ok(client, "T-0001"))
    res = await ask(client, "2+2", user)
    assert res["done"]["answer"] in (OFF_TOPIC, NO_ANSWER)
    assert res["done"]["cited"] == []

    res = await ask(client, "Какой рецепт борща с пампушками?", user)
    assert res["done"]["answer"] == NO_ANSWER
    assert res["done"]["cited"] == []


async def test_heartbeat_while_model_is_silent(
    client: httpx.AsyncClient, ids: dict[str, int], fake_ollama: FakeOllamaState
) -> None:
    fake_ollama.first_token_delay = 4.5
    res = await ask(client, "Какие СИЗ выдаются электросварщику?")
    names = [n for n, _ in res["events"]]
    gen = names.index("status", names.index("sources"))
    first_token = names.index("token")
    assert "ping" in names[gen:first_token], names


async def test_model_unavailable_still_shows_found_fragments(
    client: httpx.AsyncClient, ids: dict[str, int], fake_ollama: FakeOllamaState
) -> None:
    user = auth(await login_ok(client, "T-0001"))
    fake_ollama.unavailable = True
    res = await ask(client, "Кто подписывает наряд-допуск?", user)
    assert res["error"]["code"] == "llm_unavailable"
    assert ids["prikaz-112"] in source_docs(res)  # нашли по словам, хотя векторов нет
    chat = (await client.get(f"/api/chats/{res['meta']['chat_id']}", headers=user)).json()
    assert chat["messages"][-1]["status"] == "error"


async def test_unavailable_source_warning(
    client: httpx.AsyncClient, ids: dict[str, int], fake_source: FakeSource
) -> None:
    user = auth(await login_ok(client, "T-0001"))
    fake_source.unavailable = True
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(text("DELETE FROM access_cache"))
    res = await ask(client, "Кто подписывает наряд-допуск? Какие СИЗ у электросварщика?", user)
    assert res["meta"]["unavailable_sources"][0]["message"] == (
        "Документы из системы «Документооборот» сейчас недоступны"
    )
    # Общие документы остаются, документы «по правам» источника скрыты (fail-closed).
    assert source_docs(res) <= {ids["iot-044"], ids["polozhenie-suot"]}


async def test_guest_rate_limit(client: httpx.AsyncClient, ids: dict[str, int]) -> None:
    limit = get_settings().guest_questions_per_minute
    for _ in range(limit):
        await ask(client, "Нормы выдачи СИЗ")
    r = await client.post("/api/chat/ask", json={"question": "Нормы выдачи СИЗ"})
    assert r.status_code == 429 and r.json()["detail"]["code"] == "rate_limited"
    # Вошедшего пользователя ограничение для гостей не касается.
    user = auth(await login_ok(client, "T-0001"))
    await ask(client, "Нормы выдачи СИЗ", user)


async def test_guest_history_comes_from_browser(
    client: httpx.AsyncClient, ids: dict[str, int], fake_ollama: FakeOllamaState
) -> None:
    history = [
        {"role": "user", "content": "Что должен проверить электросварщик перед работой?"},
        {"role": "assistant", "content": "Исправность сварочного аппарата [1]"},
    ]
    await ask(client, "А после окончания работы?", history=history)
    msgs = fake_ollama.prompts[-1]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]


# ---------------------------------------------------------------------------
# История чатов
# ---------------------------------------------------------------------------


async def test_chat_history_and_follow_up(
    client: httpx.AsyncClient, ids: dict[str, int], fake_ollama: FakeOllamaState
) -> None:
    user = auth(await login_ok(client, "T-0001"))
    first = await ask(client, "Кто подписывает наряд-допуск на работы на высоте?", user)
    chat_id = first["meta"]["chat_id"]
    assert chat_id and first["meta"]["message_id"]

    # Уточнение в том же чате: модель получает предыдущий вопрос и ответ.
    second = await ask(client, "А на какой срок он выдаётся?", user, chat_id=chat_id)
    assert second["meta"]["chat_id"] == chat_id
    roles = [m["role"] for m in fake_ollama.prompts[-1]]
    assert roles == ["system", "user", "assistant", "user"]
    assert "15 календарных дней" in second["done"]["answer"]

    chats = (await client.get("/api/chats", headers=user)).json()
    assert [c["id"] for c in chats] == [chat_id]
    assert chats[0]["title"].startswith("Кто подписывает наряд-допуск")
    found = (await client.get("/api/chats", params={"q": "срок"}, headers=user)).json()
    assert [c["id"] for c in found] == [chat_id]

    chat = (await client.get(f"/api/chats/{chat_id}", headers=user)).json()
    assert [m["role"] for m in chat["messages"]] == ["user", "assistant", "user", "assistant"]
    assert chat["messages"][1]["status"] == "done"
    assert chat["messages"][1]["sources"][0]["available"] is True
    assert chat["history_days"] == 90

    mid = chat["messages"][1]["id"]
    r = await client.post(
        f"/api/chats/{chat_id}/messages/{mid}/feedback", json={"value": -1}, headers=user
    )
    assert r.status_code == 204

    # Чужой чат не виден и не продолжается.
    other = auth(await login_ok(client, "T-0002"))
    assert (await client.get(f"/api/chats/{chat_id}", headers=other)).status_code == 404
    r = await client.post(
        "/api/chat/ask", json={"question": "Срок?", "chat_id": chat_id}, headers=other
    )
    assert r.status_code == 404
    assert (await client.get("/api/chats", headers=other)).json() == []
    # Гостю история недоступна.
    assert (await client.get("/api/chats")).status_code == 401

    r = await client.patch(f"/api/chats/{chat_id}", json={"title": "Наряд"}, headers=user)
    assert r.json()["title"] == "Наряд"
    assert (await client.delete(f"/api/chats/{chat_id}", headers=user)).status_code == 204
    assert (await client.get("/api/chats", headers=user)).json() == []


async def test_history_hides_sources_after_access_is_revoked(
    client: httpx.AsyncClient, ids: dict[str, int], fake_source: FakeSource
) -> None:
    user = auth(await login_ok(client, "T-0001"))
    res = await ask(client, "Кто подписывает наряд-допуск на работы на высоте?", user)
    chat_id = res["meta"]["chat_id"]

    fake_source.access["T-0001"] = set()
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(text("DELETE FROM access_cache"))
    chat = (await client.get(f"/api/chats/{chat_id}", headers=user)).json()
    src = chat["messages"][1]["sources"]
    hidden = [s for s in src if not s["available"]]
    assert hidden and all("snippet" not in s and "document_id" not in s for s in hidden)


async def test_old_chats_are_removed(client: httpx.AsyncClient, ids: dict[str, int]) -> None:
    from app.chat.history import cleanup_old_chats

    user = auth(await login_ok(client, "T-0001"))
    res = await ask(client, "Какие СИЗ выдаются электросварщику?", user)
    async with get_sessionmaker()() as s, s.begin():
        chat = await s.get(Chat, res["meta"]["chat_id"])
        assert chat is not None
        chat.updated_at = datetime.now(UTC) - timedelta(days=91)
    async with get_sessionmaker()() as s, s.begin():
        assert await cleanup_old_chats(s, 90) == 1


# ---------------------------------------------------------------------------
# Реранкер и очередь к модели
# ---------------------------------------------------------------------------


class OverlapReranker:
    """Фейковый реранкер: доля слов вопроса, найденных во фрагменте."""

    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def score(self, query: str, texts: list[str]) -> list[float]:
        from fakes.fake_ollama import stems

        self.calls += 1
        q = set(stems(query))
        return [len(q & set(stems(t))) / max(len(q), 1) for t in texts]


async def test_reranker_orders_and_gates(
    client: httpx.AsyncClient, ids: dict[str, int], fake_ollama: FakeOllamaState
) -> None:
    rr = OverlapReranker()
    set_reranker_override(rr)
    try:
        user = auth(await login_ok(client, "T-0001"))
        res = await _search(client, "периодичность обучения стропальщики группа", user)
        assert res["reranked"] is True and rr.calls == 1
        scores = [i["rerank"] for i in res["items"]]
        assert scores == sorted(scores, reverse=True)
        assert res["items"][0]["document_id"] == ids["prikaz-112"]

        # Ни один фрагмент не прошёл порог → честное «нет ответа» без обращения к модели.
        fake_ollama.prompts.clear()
        res = await ask(client, "Сколько стоит абонемент в бассейн?", user)
        assert res["done"]["answer"] == NO_ANSWER and res["done"]["no_answer"] is True
        assert fake_ollama.prompts == []
    finally:
        set_reranker_override(None, active=False)


async def test_llm_queue_positions(database_url: str) -> None:
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(text("TRUNCATE llm_queue"))
    a = LlmQueue(PRIORITY_USER, 30)
    guest = LlmQueue(PRIORITY_GUEST, 30)
    b = LlmQueue(PRIORITY_USER, 30)
    for q in (a, guest, b):
        await q.enter()
    try:
        assert await a.try_start() == 0  # первый — сразу к модели
        # Гость пришёл раньше b, но вошедшие пользователи идут первыми.
        assert await b.try_start() == 1
        assert await guest.try_start() == 2
        await a.leave()
        assert await guest.try_start() == 1
        assert await b.try_start() == 0
        # Брошенная запись (процесс упал) перестаёт держать очередь.
        async with get_sessionmaker()() as s, s.begin():
            await s.execute(
                text(
                    "UPDATE llm_queue SET heartbeat_at = now() - interval '1 minute' WHERE id = :id"
                ),
                {"id": b.id},
            )
        assert await guest.try_start() == 0
    finally:
        for q in (a, guest, b):
            await q.leave()


# ---------------------------------------------------------------------------
# Качество поиска на тестовом наборе вопросов
# ---------------------------------------------------------------------------


async def test_recall_on_eval_set(ids: dict[str, int]) -> None:
    """recall@5 на 34 вопросах по фейковым документам. Порог — защита от регрессий:
    если правка поиска его роняет, тест падает. Фейковые эмбеддинги проще bge-m3, поэтому
    на заводе с настоящей моделью цифры должны быть не хуже."""
    from app.search.evaluate import evaluate, load_questions

    data = load_questions(EVAL_FILE)
    assert len(data["questions"]) >= 30
    async with get_sessionmaker()() as s:
        report = await evaluate(s, data, get_settings())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    assert report["recall"][5] >= 0.9, report["misses"]
    assert report["mrr"] >= 0.75
