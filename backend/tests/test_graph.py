"""Этап 5: граф знаний — связи документов по правилам, извлечение сущностей моделью,
графовый поиск. И главное — права: граф не показывает чужое ни в связях, ни в сущностях,
ни в поиске, ни в промпте модели.

Документы фейкового «Документооборота» (fakes/fake_source.py) и что о них знает граф:
    приказ № 112: «Приказ № 45 … признать утратившим силу» → 112 отменяет 45;
                  «Ответственным за организацию работ на высоте назначить … Иванову А. С.»;
    приказ № 140: «О внесении изменений в приказ № 112» → 140 изменяет 112;
    приказ № 45 (скан): «Ответственным за работы на высоте назначить … Смирнова О. Л.».
Кто что видит: T-0001 — 112, 140, регламент ТО и общие (НЕ видит 45); T-0002 — всё;
гость — только общие (ИОТ-044, Положение о СУОТ).
"""

import json
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text

from app.chat.llm_queue import PRIORITY_USER, LlmQueue
from app.config import get_settings
from app.core.flags import GRAPH_LLM_ENABLED, set_flag
from app.db import get_sessionmaker
from app.graph.extract import (
    Extraction,
    handle_extract,
    normalize_name,
    skip_reason,
    store_extraction,
    strip_lists,
)
from app.graph.references import find_references
from app.graph.status import AMENDED, CANCELLED
from app.ingest import sync
from app.models import Chunk, Document, DocumentLink, Entity, Relation
from app.search.evaluate import evaluate, load_questions
from app.search.hybrid import SearchFilters
from app.worker.queue import ClaimedJob, JobDeferred
from fakes.fake_ollama import FakeOllamaState
from fakes.fake_source import FakeSource, guid
from tests.helpers import auth, login_ok, seed_test_users
from tests.test_ingest_sync import docs_by_key, drain, first_load
from tests.test_search_chat import EVAL_FILE, ask, source_docs

# ---------------------------------------------------------------------------
# Разбор ссылок (без БД)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sentence", "expected"),
    [
        (
            "1.2. Приказ № 45 от 10.01.2022 «О работах на высоте» признать утратившим силу.",
            [("cancels", "Приказ № 45 от 10.01.2022")],
        ),
        (
            "Отменить распоряжение № 7-р от 01.02.2023.",
            [("cancels", "Распоряжение № 7-р от 01.02.2023")],
        ),
        (
            "О внесении изменений в приказ № 112 от 14.03.2024",
            [("amends", "Приказ № 112 от 14.03.2024")],
        ),
        ("Пункт 4.2 приказа № 112 изложить в новой редакции.", [("amends", "Приказ № 112")]),
        (
            "Во исполнение постановления № 15 от 05.06.2023 приказываю:",
            [("executes", "Постановление № 15 от 05.06.2023")],
        ),
        (
            "Работы выполнять в соответствии с ИОТ-044 и Положением «О СУОТ» № П-01.",
            [("refers", "ИОТ-044"), ("refers", "Положение № П-01")],
        ),
        # Два документа в одном предложении: признак относится к ближайшему.
        (
            "Во исполнение приказа № 5 от 01.01.2024 отменить приказ № 7 от 02.02.2020.",
            [("executes", "Приказ № 5 от 01.01.2024"), ("cancels", "Приказ № 7 от 02.02.2020")],
        ),
        # Без номера — не ссылка; «№» в шапке таблицы — тоже.
        ("Во исполнение Правил по охране труда при работе на высоте", []),
        ("| Таб. № | ФИО | Дата |", []),
        ("приказ 2024 года", []),
    ],
)
def test_references_are_classified(sentence: str, expected: list[tuple[str, str]]) -> None:
    assert [(r.kind, r.label) for r in find_references(sentence)] == expected


def test_reference_in_question_needs_no_number_sign() -> None:
    refs = find_references("что отменило приказ 45?", strict=False)
    assert [(r.doc_type, r.number_norm) for r in refs] == [("Приказ", "45")]
    assert find_references("что отменило приказ 45?") == []  # в тексте документа «№» обязателен


def test_signature_lists_are_not_sent_to_model() -> None:
    header = (
        "# Приказ № 112 от 14.03.2024 «О допуске персонала к работам на высоте»\n"
        "Тема: Охрана труда\nПодразделение: Служба ОТ и ПБ\nСтатус: Действует\n"
        "## Ознакомлены\n| Таб. № | ФИО | Должность | Дата ознакомления |\n|---|---|---|---|\n"
        + "\n".join(
            f"| 2087{i} | Петров В. Н. ({i}) | Мастер участка | 15.03.2024 |" for i in range(40)
        )
    )
    body = strip_lists(header)
    assert "Петров" not in body and "Ознакомлены" not in body
    assert "Подразделение: Служба ОТ и ПБ" in body
    assert skip_reason(body, min_chars=120) is None
    only_list = "## Ознакомлены\n| Таб. № | ФИО |\n|---|---|\n| 1 | Иванов |"
    assert skip_reason(strip_lists(only_list), min_chars=120) == "список ознакомления"
    assert skip_reason("4.3. Резерв.", min_chars=120) == "короткий фрагмент"
    assert skip_reason("| 1 | 2 | 3 |\n" * 30, min_chars=120) == "мало текста (цифры и разметка)"


def test_entity_names_are_normalized() -> None:
    assert normalize_name("Иванова  А.С.") == normalize_name("иванова а. с.")
    assert normalize_name("«Литейный цех №2»") == "литейный цех № 2"


# ---------------------------------------------------------------------------
# Фикстуры с БД
# ---------------------------------------------------------------------------

db = pytest.mark.db


@pytest.fixture
async def ids(client: httpx.AsyncClient, docflow: int, fake_source: FakeSource) -> dict[str, int]:
    await seed_test_users()
    await first_load(docflow)
    return {k: d.id for k, d in (await docs_by_key(fake_source)).items()}


async def enable_llm(enabled: bool = True) -> None:
    async with get_sessionmaker()() as s, s.begin():
        await set_flag(s, GRAPH_LLM_ENABLED, enabled, "test")
        if enabled:
            from app.graph.extract import backfill_tick

            await backfill_tick(s)


@pytest.fixture
async def graph(ids: dict[str, int]) -> dict[str, int]:
    """Документы загружены и сущности извлечены (фейковой) моделью."""
    await enable_llm()
    await drain(500)
    return ids


async def links() -> list[DocumentLink]:
    async with get_sessionmaker()() as s:
        return list((await s.scalars(select(DocumentLink).order_by(DocumentLink.id))).all())


async def status_of(doc_id: int) -> str | None:
    async with get_sessionmaker()() as s:
        doc = await s.get(Document, doc_id)
        assert doc is not None
        return doc.legal_status


# ---------------------------------------------------------------------------
# Шаг 1: связи по правилам и статус
# ---------------------------------------------------------------------------


@db
async def test_rule_links_and_status_after_load(ids: dict[str, int]) -> None:
    got = {(lk.from_document_id, lk.kind, lk.target_document_id) for lk in await links()}
    assert (ids["prikaz-112"], "cancels", ids["prikaz-45"]) in got
    assert (ids["prikaz-140"], "amends", ids["prikaz-112"]) in got
    # Сам на себя документ не ссылается («Приказ № 112» в его собственной шапке).
    assert all(src != tgt for src, _, tgt in got)
    assert await status_of(ids["prikaz-45"]) == CANCELLED
    assert await status_of(ids["prikaz-112"]) == AMENDED
    assert await status_of(ids["prikaz-140"]) is None  # у источника статуса нет, связей нет
    async with get_sessionmaker()() as s:
        lag = await s.scalar(
            text(
                "SELECT count(*) FROM documents WHERE indexed_version IS NOT NULL "
                "AND links_version IS DISTINCT FROM indexed_version"
            )
        )
    assert lag == 0


@db
async def test_link_waits_for_target_that_arrives_later(
    client: httpx.AsyncClient, docflow: int, fake_source: FakeSource
) -> None:
    """Приказ № 112 пришёл раньше приказа № 45: ссылка «неразрешённая», а когда приказ № 45
    появится, она привяжется к нему и статус № 45 станет «Отменён»."""
    k45 = guid("prikaz-45")
    saved = fake_source.docs.pop(k45)
    await first_load(docflow)
    cancel = [lk for lk in await links() if lk.kind == "cancels"]
    assert len(cancel) == 1 and cancel[0].target_document_id is None
    assert cancel[0].target_label == "Приказ № 45 от 10.01.2022"

    saved["header"]["status"] = "Действует"  # источник не знает, что приказ отменён
    fake_source.docs[k45] = saved
    await sync.run_reconcile(docflow, get_settings(), get_sessionmaker())
    await drain()
    docs = await docs_by_key(fake_source)
    cancel = [lk for lk in await links() if lk.kind == "cancels"]
    assert cancel[0].target_document_id == docs["prikaz-45"].id
    assert docs["prikaz-45"].legal_status == CANCELLED  # по связи, вопреки шапке источника


@db
async def test_deleted_amending_document_no_longer_counts(
    ids: dict[str, int], docflow: int, fake_source: FakeSource
) -> None:
    fake_source.delete("prikaz-140")
    await sync.run_incremental(docflow, get_settings(), get_sessionmaker())
    await drain()
    assert await status_of(ids["prikaz-112"]) == "Действует"  # снова как в шапке источника


@db
async def test_only_active_filter_uses_computed_status(
    client: httpx.AsyncClient, ids: dict[str, int], docflow: int, fake_source: FakeSource
) -> None:
    fake_source.touch("prikaz-45", status="Действует")
    await sync.run_incremental(docflow, get_settings(), get_sessionmaker())
    await drain()
    assert await status_of(ids["prikaz-45"]) == CANCELLED
    token = await login_ok(client, "T-0002")
    body = {"query": "работы на высоте наряд-допуск", "filters": {"only_active": True}, "limit": 30}
    r = await client.post("/api/search", json=body, headers=auth(token))
    found = {i["document_id"] for i in r.json()["items"]}
    assert ids["prikaz-112"] in found and ids["prikaz-45"] not in found


@db
async def test_document_links_respect_access(
    client: httpx.AsyncClient, ids: dict[str, int]
) -> None:
    analyst = await login_ok(client, "T-0002")
    r = await client.get(f"/api/documents/{ids['prikaz-112']}/graph", headers=auth(analyst))
    assert r.status_code == 200, r.text
    g = r.json()
    assert g["legal_status"] == AMENDED
    cancels = [x for x in g["outgoing"] if x["kind"] == "cancels"]
    assert cancels[0]["document"]["id"] == ids["prikaz-45"]
    assert cancels[0]["document"]["legal_status"] == CANCELLED
    assert [x["document"]["id"] for x in g["incoming"]] == [ids["prikaz-140"]]
    assert g["incoming"][0]["kind_label"] == "изменён документом"

    # T-0001 приказ № 45 не видит: в связях приказа № 112 — только реквизиты из текста
    # самого приказа № 112 (их он и так читает), без ссылки на документ и его статуса.
    user = await login_ok(client, "T-0001")
    r = await client.get(f"/api/documents/{ids['prikaz-112']}/graph", headers=auth(user))
    cancels = [x for x in r.json()["outgoing"] if x["kind"] == "cancels"]
    assert cancels[0]["document"] is None
    assert cancels[0]["label"] == "Приказ № 45 от 10.01.2022"
    assert CANCELLED not in json.dumps(r.json()["outgoing"], ensure_ascii=False)
    r = await client.get(f"/api/documents/{ids['prikaz-45']}/graph", headers=auth(user))
    assert r.status_code == 403
    r = await client.get(f"/api/documents/{ids['prikaz-112']}/graph")  # гость
    assert r.status_code == 403
    # В списке документов статус вычисленный.
    r = await client.get("/api/documents", headers=auth(user))
    by_id = {d["id"]: d for d in r.json()["items"]}
    assert by_id[ids["prikaz-112"]]["legal_status"] == AMENDED


@db
async def test_cancelled_by_hidden_document_shows_note_only(
    client: httpx.AsyncClient, ids: dict[str, int], docflow: int, fake_source: FakeSource
) -> None:
    """Человек видит приказ № 45, но не видит отменивший его приказ № 112: статус «Отменён»
    он видит (чтобы не работать по отменённому приказу), а каким документом — нет."""
    fake_source.touch("prikaz-45", status="Действует")
    await sync.run_incremental(docflow, get_settings(), get_sessionmaker())
    await drain()
    fake_source.access["T-0003"] = {"prikaz-45"}
    token = await login_ok(client, "T-0003")  # вход сбрасывает кэш прав
    r = await client.get(f"/api/documents/{ids['prikaz-45']}/graph", headers=auth(token))
    g = r.json()
    assert g["legal_status"] == CANCELLED
    assert g["incoming"] == []
    assert g["status_note"] == "Отменён документом, который вам недоступен"
    assert "112" not in json.dumps(g, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Шаг 2: извлечение моделью
# ---------------------------------------------------------------------------


@db
async def test_llm_extraction_is_off_until_admin_enables(
    ids: dict[str, int], fake_ollama: FakeOllamaState
) -> None:
    assert fake_ollama.extract_calls == 0
    async with get_sessionmaker()() as s:
        assert await s.scalar(select(text("count(*)")).select_from(Entity)) == 0
        pending = await s.scalar(text("SELECT count(*) FROM chunks WHERE graph_state IS NULL"))
    assert pending and pending > 0


@db
async def test_llm_extraction_builds_graph(
    graph: dict[str, int], fake_ollama: FakeOllamaState
) -> None:
    async with get_sessionmaker()() as s:
        states = dict(
            (await s.execute(text("SELECT graph_state, count(*) FROM chunks GROUP BY 1"))).all()
        )
        ents = {(e.type, e.name) for e in (await s.scalars(select(Entity))).all()}
        rels = (
            await s.execute(
                text(
                    "SELECT a.name, r.predicate, b.name FROM relations r "
                    "JOIN entities a ON a.id = r.subject_id JOIN entities b ON b.id = r.object_id"
                )
            )
        ).all()
    assert None not in states  # всё обработано
    assert states.get("done", 0) > 0 and states.get("skipped", 0) > 0
    assert ("person", "Иванова А. С.") in ents
    assert ("work", "работы на высоте") in ents
    assert ("department", "литейный цех № 2") in ents
    assert ("Иванова А. С.", "responsible_for", "работы на высоте") in rels
    assert ("Смирнов О. Л.", "responsible_for", "работы на высоте") in rels
    # Списки подписей («Ознакомлены», 46 строк) в модель не отправлялись.
    sent = "\n".join(m["content"] for p in fake_ollama.extract_prompts for m in p)
    assert "Дата ознакомления" not in sent


@db
async def test_extraction_yields_to_users_and_resumes(ids: dict[str, int]) -> None:
    await enable_llm()
    settings = get_settings().model_copy(update={"graph_chunks_per_job": 2})
    job = ClaimedJob(1, "graph_extract", {"document_id": ids["prikaz-112"]}, 1, 5)
    q = LlmQueue(PRIORITY_USER, 30)
    await q.enter()
    try:
        with pytest.raises(JobDeferred) as waiting:
            await handle_extract(job, settings, get_sessionmaker())
        assert waiting.value.seconds == 5  # пользователь ждёт ответ — модель не трогаем
    finally:
        await q.leave()
    with pytest.raises(JobDeferred) as more:
        await handle_extract(job, settings, get_sessionmaker())
    assert more.value.seconds == 0  # обработал 2 фрагмента и уступил очередь
    async with get_sessionmaker()() as s:
        done = await s.scalar(
            text("SELECT count(*) FROM chunks WHERE document_id = :d AND graph_state = 'done'"),
            {"d": ids["prikaz-112"]},
        )
    assert done == 2  # прогресс сохранён: после перезапуска продолжит с третьего


@db
async def test_bad_model_answer_marks_chunk_error_and_can_be_retried(
    client: httpx.AsyncClient, ids: dict[str, int], fake_ollama: FakeOllamaState
) -> None:
    fake_ollama.bad_json = True
    await enable_llm()
    await drain(500)
    async with get_sessionmaker()() as s:
        errors = await s.scalar(text("SELECT count(*) FROM chunks WHERE graph_state = 'error'"))
    assert errors and errors > 0
    fake_ollama.bad_json = False
    admin = await login_ok(client, "T-0004")
    r = await client.post("/api/admin/graph/retry-errors", headers=auth(admin))
    assert r.json()["chunks"] == errors
    await enable_llm()
    await drain(500)
    async with get_sessionmaker()() as s:
        errors = await s.scalar(text("SELECT count(*) FROM chunks WHERE graph_state = 'error'"))
    assert errors == 0


@db
async def test_reindex_drops_old_graph_of_document(
    graph: dict[str, int], docflow: int, fake_source: FakeSource
) -> None:
    async with get_sessionmaker()() as s:
        before = await s.scalar(
            select(text("count(*)"))
            .select_from(Relation)
            .where(Relation.document_id == graph["prikaz-45"])
        )
    assert before and before > 0
    fake_source.drop("prikaz-45")
    await sync.run_reconcile(docflow, get_settings(), get_sessionmaker())
    await drain(500)
    async with get_sessionmaker()() as s:
        smirnov = await s.scalar(select(Entity).where(Entity.name == "Смирнов О. Л."))
        # Документ скрыт из поиска; сущность остаётся в БД, но видна только через
        # упоминания в живых доступных документах (проверено в тестах прав ниже).
        alive_mentions = await s.scalar(
            text(
                "SELECT count(*) FROM entity_mentions m JOIN documents d ON d.id = m.document_id "
                "WHERE m.entity_id = :e AND d.deleted_at IS NULL"
            ),
            {"e": smirnov.id if smirnov else 0},
        )
    assert alive_mentions == 0


@db
async def test_store_extraction_ignores_relations_to_unknown_entities(ids: dict[str, int]) -> None:
    async with get_sessionmaker()() as s, s.begin():
        chunk = await s.scalar(select(Chunk).where(Chunk.document_id == ids["prikaz-112"]).limit(1))
        assert chunk is not None
        data = Extraction.model_validate(
            {
                "entities": [
                    {"type": "person", "name": "Тест Т. Т."},
                    {"type": "alien", "name": "x"},
                ],
                "relations": [
                    {
                        "subject": "Тест Т. Т.",
                        "predicate": "responsible_for",
                        "object": "Нет такой",
                    },
                    {"subject": "Тест Т. Т.", "predicate": "hacks", "object": "Тест Т. Т."},
                ],
            }
        )
        n_ent, n_rel = await store_extraction(s, chunk.id, chunk.document_id, data, chunk.text)
    assert (n_ent, n_rel) == (1, 0)


# ---------------------------------------------------------------------------
# Права в графе: сущности
# ---------------------------------------------------------------------------


@db
async def test_entities_respect_access(client: httpx.AsyncClient, graph: dict[str, int]) -> None:
    async with get_sessionmaker()() as s:
        smirnov = await s.scalar(select(Entity).where(Entity.name == "Смирнов О. Л."))
        work = await s.scalar(select(Entity).where(Entity.name == "работы на высоте"))
    assert smirnov is not None and work is not None

    analyst = await login_ok(client, "T-0002")
    r = await client.get("/api/graph/entities", params={"q": "Смирнов"}, headers=auth(analyst))
    assert [e["name"] for e in r.json()["items"]] == ["Смирнов О. Л."]

    # T-0001 приказ № 45 не видит — а Смирнов упомянут только там.
    user = await login_ok(client, "T-0001")
    r = await client.get("/api/graph/entities", params={"q": "Смирнов"}, headers=auth(user))
    assert r.json() == {"items": [], "total": 0}
    r = await client.get(f"/api/graph/entities/{smirnov.id}", headers=auth(user))
    assert r.status_code == 404
    # «Работы на высоте» он видит, но без связей и документов из приказа № 45.
    r = await client.get(f"/api/graph/entities/{work.id}", headers=auth(user))
    assert r.status_code == 200
    view = json.dumps(r.json(), ensure_ascii=False)
    assert "Смирнов" not in view and "№ 45" not in view
    assert "Иванова А. С." in view
    # Гость — только сущности общих документов.
    r = await client.get("/api/graph/entities", params={"limit": 200})
    names = {e["name"] for e in r.json()["items"]}
    assert "сварочный аппарат" in names
    assert "работы на высоте" not in names and "Иванова А. С." not in names
    r = await client.get(f"/api/graph/entities/{work.id}")
    assert r.status_code == 404
    # Сущности в карточке документа — только для доступного документа.
    r = await client.get(f"/api/documents/{graph['prikaz-112']}/graph", headers=auth(user))
    assert "Иванова А. С." in {e["name"] for e in r.json()["entities"]}


# ---------------------------------------------------------------------------
# Графовый поиск и ответ
# ---------------------------------------------------------------------------


async def search(
    client: httpx.AsyncClient, token: str | None, query: str, mode: str, **filters: Any
) -> dict[str, Any]:
    r = await client.post(
        "/api/search",
        json={"query": query, "mode": mode, "filters": filters, "limit": 20},
        headers=auth(token) if token else None,
    )
    assert r.status_code == 200, r.text
    return r.json()


@db
async def test_graph_search_finds_what_cancelled_a_document(
    client: httpx.AsyncClient, graph: dict[str, int]
) -> None:
    analyst = await login_ok(client, "T-0002")
    res = await search(client, analyst, "Что отменило приказ 45?", "graph")
    assert res["mode"] == "graph"
    assert res["items"][0]["document_id"] == graph["prikaz-112"]
    assert "утратившим силу" in res["items"][0]["snippet"]
    assert res["graph"]["documents"] == [graph["prikaz-45"]]


@db
async def test_graph_search_never_leaks_hidden_documents(
    client: httpx.AsyncClient, graph: dict[str, int], fake_ollama: FakeOllamaState
) -> None:
    user = await login_ok(client, "T-0001")
    for mode in ("graph", "mixed"):
        res = await search(client, user, "Кто ответственный за работы на высоте?", mode)
        found = {i["document_id"] for i in res["items"]}
        assert graph["prikaz-45"] not in found
        assert graph["prikaz-112"] in found
        # «Приказ 45» в вопросе: документ T-0001 не виден — граф его «не знает».
        res = await search(client, user, "Что отменило приказ 45?", mode)
        assert res["graph"]["documents"] == []
        assert graph["prikaz-45"] not in {i["document_id"] for i in res["items"]}
    # Гость: граф только по общим документам.
    res = await search(client, None, "Кто ответственный за работы на высоте?", "graph")
    assert res["items"] == [] and res["graph"]["entities"] == []

    # Ответ модели: ни фрагментов, ни справки графа из приказа № 45.
    res = await ask(client, "Кто ответственный за работы на высоте?", auth(user), mode="mixed")
    assert graph["prikaz-45"] not in source_docs(res)
    prompts = "\n".join(m["content"] for p in fake_ollama.prompts for m in p)
    assert "Смирнов" not in prompts and "Иванову" in prompts


@db
async def test_answer_gets_graph_facts(
    client: httpx.AsyncClient, graph: dict[str, int], fake_ollama: FakeOllamaState
) -> None:
    analyst = await login_ok(client, "T-0002")
    res = await ask(client, "Что отменило приказ 45?", auth(analyst), mode="graph")
    assert graph["prikaz-112"] in source_docs(res)
    system = fake_ollama.prompts[-1][0]["content"]
    assert "Связи документов и сведения графа знаний" in system
    assert "Приказ № 112 от 14.03.2024 отменяет Приказ № 45 от 10.01.2022 [" in system
    assert res["done"]["mode"] == "graph"


@db
async def test_graph_mode_respects_filters(
    client: httpx.AsyncClient, graph: dict[str, int]
) -> None:
    analyst = await login_ok(client, "T-0002")
    res = await search(
        client, analyst, "Кто ответственный за работы на высоте?", "graph", only_active=True
    )
    found = {i["document_id"] for i in res["items"]}
    assert graph["prikaz-112"] in found and graph["prikaz-45"] not in found


@db
async def test_modes_compared_on_eval_set(graph: dict[str, int]) -> None:
    """Сравнение режимов на тестовом наборе (docs/graph.md, раздел «Качество»). Граф
    должен помогать на вопросах о связях и не портить остальные."""
    data = load_questions(EVAL_FILE)
    reports = {}
    async with get_sessionmaker()() as s:
        for mode in ("chunks", "graph", "mixed"):
            reports[mode] = await evaluate(s, data, get_settings(), mode)
    table = {
        m: {"all@5": r["recall"][5], "graph@5": r["by_kind"]["graph"]["recall"][5], "mrr": r["mrr"]}
        for m, r in reports.items()
    }
    print(json.dumps(table, ensure_ascii=False, indent=2))
    print(json.dumps(reports["mixed"]["misses"], ensure_ascii=False, indent=2))
    assert reports["mixed"]["recall"][5] >= 0.9, reports["mixed"]["misses"]
    assert (
        reports["mixed"]["by_kind"]["graph"]["recall"][5]
        >= reports["chunks"]["by_kind"]["graph"]["recall"][5]
    )
    assert (
        reports["mixed"]["by_kind"]["graph"]["mrr"] >= reports["chunks"]["by_kind"]["graph"]["mrr"]
    )


# ---------------------------------------------------------------------------
# Админка графа
# ---------------------------------------------------------------------------


@db
async def test_admin_graph_page(client: httpx.AsyncClient, ids: dict[str, int]) -> None:
    user = await login_ok(client, "T-0001")
    assert (await client.get("/api/admin/graph", headers=auth(user))).status_code == 403
    assert (
        await client.post("/api/admin/graph/llm", json={"enabled": True}, headers=auth(user))
    ).status_code == 403

    admin = await login_ok(client, "T-0004")
    st = (await client.get("/api/admin/graph", headers=auth(admin))).json()
    assert st["llm_enabled"] is False
    assert st["links"]["by_kind"]["cancels"] == 1 and st["links"]["by_kind"]["amends"] >= 1
    assert st["links"]["documents_done"] == st["links"]["documents_indexed"]
    # Ни замера, ни обработанных фрагментов: прогноз по допущению 12 с на фрагмент.
    assert st["llm"]["seconds_basis"] == "assumption" and st["llm"]["forecast_seconds"] > 0

    # Замер на выборке — в фоне, результат в прогнозе.
    r = await client.post("/api/admin/graph/estimate", json={"sample": 5}, headers=auth(admin))
    assert r.status_code == 202
    await drain()
    st = (await client.get("/api/admin/graph", headers=auth(admin))).json()
    est = st["last_estimate"]
    assert est["sample"] == 5 and est["avg_seconds"] is not None
    assert st["llm"]["seconds_basis"] == "estimate"
    assert est["examples"] and "entities" in est["examples"][0]  # выборка случайная

    r = await client.post("/api/admin/graph/llm", json={"enabled": True}, headers=auth(admin))
    assert r.json()["queued_documents"] > 0
    await drain(500)
    st = (await client.get("/api/admin/graph", headers=auth(admin))).json()
    assert st["llm"]["chunks"]["pending"] == 0 and st["entities"]["total"] > 0
    assert st["llm"]["seconds_basis"] == "measured"

    r = await client.post("/api/admin/graph/rebuild-links", headers=auth(admin))
    assert r.json()["queued_documents"] == st["links"]["documents_indexed"]
    await drain()
    assert len([lk for lk in await links() if lk.kind == "cancels"]) == 1
    async with get_sessionmaker()() as s:
        actions = set((await s.scalars(text("SELECT action FROM audit_log"))).all())
    assert {"graph.llm_toggle", "graph.estimate", "graph.rebuild_links"} <= actions


@db
async def test_mixed_is_default_and_chunks_mode_still_works(
    client: httpx.AsyncClient, ids: dict[str, int]
) -> None:
    analyst = await login_ok(client, "T-0002")
    res = await search(client, analyst, "На какой срок выдаётся наряд-допуск?", "chunks")
    assert res["mode"] == "chunks" and res["items"]
    r = await client.post("/api/search", json={"query": "наряд-допуск"}, headers=auth(analyst))
    assert r.json()["mode"] == "mixed"
    assert SearchFilters().conditions() == []
