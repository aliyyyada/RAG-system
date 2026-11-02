"""Этап 6: противоречия между документами.

Документы фейкового «Документооборота» (fakes/fake_source.py) и что о них знает проверка:
    Положение о СУОТ (общий), п. 3.1: обучение и проверка знаний по работам на высоте
        «не реже одного раза в год»;
    приказ № 112, п. 4.4 (таблица): периодичность обучения «1 раз в 3 года»
        → ПРЯМОЕ ПРОТИВОРЕЧИЕ, активное;
    приказ № 112, п. 4.2: наряд-допуск «не более 15 календарных дней»;
    приказ № 140: «наряд-допуск … не более 10 календарных дней» и он ИЗМЕНЯЕТ приказ № 112
        → расхождение есть, но не активно (это изменение, а не противоречие);
    приказ № 45 (отменён): «не более 10 дней» → в модель не отправляется вовсе.
Кто что видит: T-0001 — 112, 140, регламент ТО и общие; T-0002 (аналитик, есть право
«Просмотр противоречий») и T-0004 (админ) — всё; гость — только общие.
"""

import json
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from app.auth import permissions as perms
from app.chat.llm_queue import PRIORITY_USER, LlmQueue
from app.config import get_settings
from app.contradictions.candidates import find_candidates, is_normative, make_pair, text_key
from app.contradictions.judge import exact_quote
from app.contradictions.scan import handle_scan
from app.core.flags import CONTRA_ENABLED, set_flag
from app.db import get_sessionmaker
from app.ingest import sync
from app.models import Contradiction, ContradictionCheck, Document, Role, RolePermission, User
from app.worker.queue import ClaimedJob, JobDeferred
from fakes.fake_ollama import FakeOllamaState, judge_pair
from fakes.fake_source import FakeSource
from tests.helpers import auth, login_ok
from tests.test_graph import enable_llm, ids  # noqa: F401 — фикстура ids
from tests.test_ingest_sync import docs_by_key, drain

db = pytest.mark.db

# ---------------------------------------------------------------------------
# Без БД
# ---------------------------------------------------------------------------


def test_normative_fragments() -> None:
    assert is_normative(
        "4.2. Допуск к работам на высоте осуществляется по наряду-допуску. Наряд-допуск "
        "выдаётся на срок не более 15 календарных дней."
    )
    assert is_normative(
        "3.1. Обучение и проверка знаний по охране труда проводятся не реже одного раза в год."
    )
    # Описание и списки подписей требований не содержат.
    assert not is_normative(
        "1.1. Положение определяет порядок функционирования системы управления охраной труда."
    )
    assert not is_normative(
        "## Ознакомлены\n| Таб. № | ФИО | Дата ознакомления |\n|---|---|---|\n| 1 | Иванов | 1 |"
    )
    assert not is_normative("Срок — 5 дней.")  # слишком коротко


def test_pair_is_canonical_and_cache_key_ignores_spaces() -> None:
    a = (1, 10, "Наряд-допуск выдаётся на срок не более 15 календарных дней, пункт 4.2.")
    b = (2, 20, "Наряд-допуск выдаётся на срок не более 10 календарных дней, пункт 4.2.")
    p1, p2 = make_pair(a, b, "vector", 0.5), make_pair(b, a, "vector", 0.5)
    assert p1 is not None and p2 is not None
    assert (p1.chunk_a, p1.chunk_b) == (p2.chunk_a, p2.chunk_b) and p1.key_a < p1.key_b
    assert text_key("Наряд  выдаётся\nна срок") == text_key("наряд выдаётся на срок")
    assert make_pair(a, (3, 10, b[2]), "vector", 0.5) is None  # один документ
    assert make_pair(a, (3, 20, a[2]), "vector", 0.5) is None  # одинаковый текст


def test_quotes_must_be_exact() -> None:
    body = "| 2 | Мастера, бригадиры | 1 раз в 3 года |"
    assert exact_quote("1 раз  в 3 года", body) == "1 раз в 3 года"
    assert exact_quote("один раз в три года", body) == ""  # перефразировано — не подсвечиваем


def test_fake_judge() -> None:
    a = "Обучение по работам на высоте для групп 1 и 2 — не реже одного раза в год."
    b = "| 2 | Мастера | 1 раз в 3 года |. Периодичность обучения по работам на высоте."
    assert judge_pair(a, b)["verdict"] == "contradiction"
    assert judge_pair(a, "Работы на высоте — по наряду-допуску.")["verdict"] == "none"


# ---------------------------------------------------------------------------
# Фикстуры с БД
# ---------------------------------------------------------------------------


async def enable_contra(enabled: bool = True) -> None:
    async with get_sessionmaker()() as s, s.begin():
        await set_flag(s, CONTRA_ENABLED, enabled, "test")
        if enabled:
            from app.contradictions.scan import backfill_tick

            await backfill_tick(s)


@pytest.fixture
async def found(ids: dict[str, int]) -> dict[str, int]:  # noqa: F811
    """Документы загружены, противоречия найдены (фейковой) моделью."""
    await enable_contra()
    await drain(500)
    return ids


async def grant(personnel_number: str, *codes: str) -> None:
    """Выдать человеку роль с правами (как администратор в админке)."""
    async with get_sessionmaker()() as s, s.begin():
        role = Role(
            name=f"Тест {personnel_number} {len(codes)}",
            permission_rows=[RolePermission(permission=c) for c in codes],
        )
        s.add(role)
        user = await s.scalar(
            select(User)
            .where(User.personnel_number == personnel_number)
            .options(selectinload(User.roles))
        )
        assert user is not None
        user.roles.append(role)


async def listing(client: httpx.AsyncClient, token: str, **params: Any) -> dict[str, Any]:
    r = await client.get("/api/contradictions", params=params, headers=auth(token))
    assert r.status_code == 200, r.text
    return r.json()


def pair_docs(item: dict[str, Any]) -> set[int]:
    return {s["document"]["id"] for s in item["sides"]}


def contra_docs(fake: FakeOllamaState) -> list[str]:
    """Документы А и Б во всех промптах проверки."""
    out = []
    for p in fake.contra_prompts:
        user = p[-1]["content"]
        out += [line for line in user.splitlines() if line.startswith("Документ ")]
    return out


# ---------------------------------------------------------------------------
# Поиск и кэш
# ---------------------------------------------------------------------------


@db
async def test_off_until_admin_enables(ids: dict[str, int], fake_ollama: FakeOllamaState) -> None:  # noqa: F811
    assert fake_ollama.contra_calls == 0
    async with get_sessionmaker()() as s:
        assert await s.scalar(text("SELECT count(*) FROM contradiction_checks")) == 0


@db
async def test_scan_finds_contradictions(
    found: dict[str, int], fake_ollama: FakeOllamaState
) -> None:
    async with get_sessionmaker()() as s:
        rows = (
            await s.execute(
                select(Contradiction, ContradictionCheck).join(
                    ContradictionCheck, ContradictionCheck.id == Contradiction.check_id
                )
            )
        ).all()
        lag = await s.scalar(
            text(
                "SELECT count(*) FROM documents WHERE indexed_version IS NOT NULL "
                "AND deleted_at IS NULL AND contra_version IS DISTINCT FROM indexed_version"
            )
        )
    assert lag == 0
    pairs = {frozenset((c.document_a_id, c.document_b_id)): ch for c, ch in rows}
    suot = pairs[frozenset((found["polozhenie-suot"], found["prikaz-112"]))]
    assert suot.verdict == "contradiction"
    assert {suot.quote_a, suot.quote_b} == {"не реже одного раза в год", "1 раз в 3 года"}
    assert suot.review_status == "new" and suot.topic
    assert frozenset((found["prikaz-140"], found["prikaz-112"])) in pairs
    # Отменённый приказ № 45 в модель не отправлялся: противоречие с ним всё равно не активно.
    assert not any("№ 45 " in line for line in contra_docs(fake_ollama))
    assert all(found["prikaz-45"] not in k for k in pairs)


@db
async def test_checked_pairs_are_cached(
    client: httpx.AsyncClient, found: dict[str, int], fake_ollama: FakeOllamaState
) -> None:
    calls = fake_ollama.contra_calls
    assert calls > 0
    admin = await login_ok(client, "T-0004")
    r = await client.post("/api/admin/contradictions/rescan", headers=auth(admin))
    assert r.json()["queued_documents"] > 0
    await drain(500)
    assert fake_ollama.contra_calls == calls  # все пары взяты из кэша
    async with get_sessionmaker()() as s:
        assert await s.scalar(text("SELECT count(*) FROM contradictions")) == 2


@db
async def test_review_survives_reindex(
    client: httpx.AsyncClient,
    found: dict[str, int],
    docflow: int,
    fake_source: FakeSource,
    fake_ollama: FakeOllamaState,
) -> None:
    admin = await login_ok(client, "T-0004")
    item = (await listing(client, admin))["items"][0]
    r = await client.post(
        f"/api/contradictions/{item['id']}/review",
        json={"status": "confirmed", "note": "Передано в службу ОТ"},
        headers=auth(admin),
    )
    assert r.status_code == 200, r.text
    assert r.json()["review_status"] == "confirmed" and r.json()["reviewed_by"]

    # Положение переиндексировано (источник поменял дату, текст тот же): фрагменты новые,
    # противоречие находится снова — из кэша, без модели, и остаётся подтверждённым.
    calls = fake_ollama.contra_calls
    fake_source.touch("polozhenie-suot")
    await sync.run_incremental(docflow, get_settings(), get_sessionmaker())
    await drain(500)
    assert fake_ollama.contra_calls == calls
    data = await listing(client, admin, status="confirmed")
    assert data["counts"] == {"new": 0, "confirmed": 1, "rejected": 0}
    assert data["items"][0]["id"] != item["id"]  # новые фрагменты — новая запись
    assert data["items"][0]["review_note"] == "Передано в службу ОТ"
    async with get_sessionmaker()() as s:
        audit = (
            await s.scalars(
                text("SELECT details FROM audit_log WHERE action = 'contradictions.review'")
            )
        ).all()
    assert audit and audit[0]["status"] == "confirmed"


@db
async def test_amended_and_cancelled_are_not_active(
    client: httpx.AsyncClient, found: dict[str, int], docflow: int, fake_source: FakeSource
) -> None:
    analyst = await login_ok(client, "T-0002")
    active = await listing(client, analyst)
    assert [pair_docs(i) for i in active["items"]] == [
        {found["polozhenie-suot"], found["prikaz-112"]}
    ]
    item = active["items"][0]
    assert item["active"] and item["verdict_label"] == "Прямое противоречие"
    # Сначала более новый документ (приказ № 112 от 2024 г.), цитаты для подсветки.
    assert item["sides"][0]["document"]["id"] == found["prikaz-112"]
    assert item["sides"][0]["quote"] in item["sides"][0]["excerpt"]
    assert item["sides"][1]["place"] == "п. 3.1"

    inactive = await listing(client, analyst, active="inactive")
    assert [pair_docs(i) for i in inactive["items"]] == [{found["prikaz-140"], found["prikaz-112"]}]
    reason = inactive["items"][0]["inactive_reason"]
    assert reason.startswith("Приказ № 140 от 20.05.2024") and " изменяет Приказ № 112" in reason
    assert (await client.get("/api/contradictions/count", headers=auth(analyst))).json() == {
        "new": 1
    }

    # Положение отменено источником — противоречие с ним больше не активно.
    fake_source.touch("polozhenie-suot", status="Отменён")
    await sync.run_incremental(docflow, get_settings(), get_sessionmaker())
    await drain(500)
    assert (await listing(client, analyst))["items"] == []
    gone = await listing(client, analyst, active="all")
    reasons = {i["inactive_reason"] for i in gone["items"]}
    assert any(r and "Положение" in r and "отменён" in r for r in reasons), reasons


@db
async def test_scan_yields_to_users_and_resumes(
    ids: dict[str, int],  # noqa: F811
    fake_ollama: FakeOllamaState,
) -> None:
    await enable_contra()
    settings = get_settings().model_copy(update={"contra_pairs_per_job": 1})
    job = ClaimedJob(1, "contra_scan", {"document_id": ids["prikaz-112"]}, 1, 5)
    q = LlmQueue(PRIORITY_USER, 30)
    await q.enter()
    try:
        with pytest.raises(JobDeferred) as waiting:
            await handle_scan(job, settings, get_sessionmaker())
        assert waiting.value.seconds == 5  # пользователь ждёт ответ — модель не трогаем
    finally:
        await q.leave()
    assert fake_ollama.contra_calls == 0
    with pytest.raises(JobDeferred) as more:
        await handle_scan(job, settings, get_sessionmaker())
    assert more.value.seconds == 0 and fake_ollama.contra_calls == 1  # одна пара и уступил
    # Следующие заходы продолжают с того же места, проверенная пара — из кэша.
    for _ in range(10):
        try:
            await handle_scan(job, settings, get_sessionmaker())
            break
        except JobDeferred as d:
            assert d.seconds == 0
    async with get_sessionmaker()() as s:
        doc = await s.get(Document, ids["prikaz-112"])
        checks = await s.scalar(text("SELECT count(*) FROM contradiction_checks"))
    assert doc is not None and doc.contra_version == doc.indexed_version
    assert checks == fake_ollama.contra_calls


@db
async def test_bad_model_answer_is_error_and_can_be_retried(
    client: httpx.AsyncClient,
    ids: dict[str, int],  # noqa: F811
    fake_ollama: FakeOllamaState,
) -> None:
    fake_ollama.contra_bad_json = True
    await enable_contra()
    await drain(500)
    async with get_sessionmaker()() as s:
        errors = await s.scalar(
            text("SELECT count(*) FROM contradiction_checks WHERE verdict = 'error'")
        )
        assert await s.scalar(text("SELECT count(*) FROM contradictions")) == 0
    assert errors and errors > 0
    fake_ollama.contra_bad_json = False
    admin = await login_ok(client, "T-0004")
    st = (await client.get("/api/admin/contradictions", headers=auth(admin))).json()
    assert st["checks"]["error"] == errors
    r = await client.post("/api/admin/contradictions/retry-errors", headers=auth(admin))
    assert r.json()["pairs"] == errors
    await drain(500)
    async with get_sessionmaker()() as s:
        assert (
            await s.scalar(
                text("SELECT count(*) FROM contradiction_checks WHERE verdict = 'error'")
            )
            == 0
        )
        assert await s.scalar(text("SELECT count(*) FROM contradictions")) == 2


@db
async def test_graph_linked_candidates(ids: dict[str, int]) -> None:  # noqa: F811
    """Пары, связанные графом (общая сущность «работы на высоте»), находятся даже тогда,
    когда векторный порог их бы не пропустил."""
    await enable_llm()
    await drain(500)
    strict = get_settings().model_copy(
        update={"contra_min_similarity": 0.99, "contra_graph_min_similarity": 0.2}
    )
    async with get_sessionmaker()() as s, s.begin():
        pairs = await find_candidates(s, ids["polozhenie-suot"], strict)
    got = {(p.origin, frozenset((p.document_a, p.document_b))) for p in pairs}
    assert ("graph", frozenset((ids["polozhenie-suot"], ids["prikaz-112"]))) in got
    assert all(origin == "graph" for origin, _ in got)


# ---------------------------------------------------------------------------
# Права
# ---------------------------------------------------------------------------


@db
async def test_needs_permission_and_login(client: httpx.AsyncClient, found: dict[str, int]) -> None:
    r = await client.get("/api/contradictions")  # гость
    assert r.status_code == 401
    user = await login_ok(client, "T-0001")  # роль «Пользователь»: права нет
    assert (await client.get("/api/contradictions", headers=auth(user))).status_code == 403
    assert (await client.get("/api/contradictions/count", headers=auth(user))).status_code == 403
    assert (await client.get("/api/admin/contradictions", headers=auth(user))).status_code == 403
    analyst = await login_ok(client, "T-0002")
    item = (await listing(client, analyst))["items"][0]
    # Просматривать может, подтверждать — нет (нужно contradictions.manage).
    r = await client.post(
        f"/api/contradictions/{item['id']}/review",
        json={"status": "rejected"},
        headers=auth(analyst),
    )
    assert r.status_code == 403


@db
async def test_pair_needs_both_documents(
    client: httpx.AsyncClient, found: dict[str, int], fake_source: FakeSource
) -> None:
    """Пара видна, только если доступны оба документа: ни в списке, ни в счётчике, ни по
    ссылке, ни через «Подтвердить»."""
    await grant("T-0003", perms.CONTRADICTIONS_VIEW, perms.CONTRADICTIONS_MANAGE)
    analyst = await login_ok(client, "T-0002")
    everything = await listing(client, analyst, active="all")
    by_docs = {frozenset(pair_docs(i)): i["id"] for i in everything["items"]}
    amend_id = by_docs[frozenset((found["prikaz-140"], found["prikaz-112"]))]
    suot_id = by_docs[frozenset((found["polozhenie-suot"], found["prikaz-112"]))]

    # T-0003 источник не выдал ни одного документа: видны только общие — пар нет.
    t3 = await login_ok(client, "T-0003")
    assert (await listing(client, t3, active="all"))["items"] == []
    assert (await client.get("/api/contradictions/count", headers=auth(t3))).json() == {"new": 0}
    r = await client.get(f"/api/contradictions/{suot_id}", headers=auth(t3))
    assert r.status_code == 404

    # Источник открыл ему приказ № 112: видна пара «Положение ↔ 112», но не «140 ↔ 112».
    fake_source.access["T-0003"] = {"prikaz-112"}
    t3 = await login_ok(client, "T-0003")  # вход сбрасывает кэш прав
    data = await listing(client, t3, active="all")
    assert [i["id"] for i in data["items"]] == [suot_id]
    assert "Приказ № 140" not in json.dumps(data, ensure_ascii=False)
    assert (
        await client.get(f"/api/contradictions/{amend_id}", headers=auth(t3))
    ).status_code == 404
    r = await client.post(
        f"/api/contradictions/{amend_id}/review", json={"status": "rejected"}, headers=auth(t3)
    )
    assert r.status_code == 404
    r = await client.get(
        "/api/contradictions",
        params={"document_id": found["prikaz-140"], "active": "all"},
        headers=auth(t3),
    )
    assert r.json()["items"] == []


@db
async def test_source_down_hides_its_pairs(
    client: httpx.AsyncClient, found: dict[str, int], fake_source: FakeSource
) -> None:
    analyst = await login_ok(client, "T-0002")  # вход сбросил кэш прав
    fake_source.unavailable = True
    data = await listing(client, analyst, active="all")
    # Приказы «Документооборота» скрыты (fail-closed), значит и все пары с ними.
    assert data["items"] == []
    assert data["unavailable_sources"][0]["message"].startswith("Документы из системы")


@db
async def test_admin_status_and_toggle(
    client: httpx.AsyncClient,
    ids: dict[str, int],  # noqa: F811
    fake_ollama: FakeOllamaState,
) -> None:
    admin = await login_ok(client, "T-0004")
    st = (await client.get("/api/admin/contradictions", headers=auth(admin))).json()
    assert st["enabled"] is False
    assert st["documents"]["pending"] == st["documents"]["indexed"] > 0
    # Ни одной проверки: прогноз по допущениям 6 пар на документ и 6 с на пару.
    assert st["seconds_basis"] == "assumption" and st["forecast_seconds"] > 0

    r = await client.post(
        "/api/admin/contradictions/enabled", json={"enabled": True}, headers=auth(admin)
    )
    assert r.json()["queued_documents"] > 0
    await drain(500)
    st = (await client.get("/api/admin/contradictions", headers=auth(admin))).json()
    assert st["documents"]["pending"] == 0 and st["forecast_seconds"] == 0
    assert st["seconds_basis"] == "measured"
    assert st["checks"]["contradiction"] == 2 and st["found"] == 2
    assert st["documents"]["cancelled"] == 1  # приказ № 45

    r = await client.post(
        "/api/admin/contradictions/enabled", json={"enabled": False}, headers=auth(admin)
    )
    assert r.json() == {"enabled": False, "queued_documents": 0}
    async with get_sessionmaker()() as s:
        actions = set((await s.scalars(text("SELECT action FROM audit_log"))).all())
    assert "contradictions.toggle" in actions


@db
async def test_document_filter(
    client: httpx.AsyncClient, found: dict[str, int], fake_source: FakeSource
) -> None:
    analyst = await login_ok(client, "T-0002")
    data = await listing(client, analyst, document_id=found["polozhenie-suot"])
    assert len(data["items"]) == 1
    assert "Охрана труда" in data["topics"]
    data = await listing(client, analyst, document_id=found["reglament-to"], active="all")
    assert data["items"] == []
    docs = await docs_by_key(fake_source)
    assert docs["prikaz-45"].contra_pairs == 0  # отменён — пропущен


def test_excerpt_shows_table_row_with_column_names() -> None:
    from app.api.contradictions import excerpt

    body = (
        "4.4. Периодичность обучения по группам безопасности:\n"
        "| Группа | Кто | Периодичность |\n|---|---|---|\n"
        "| 1 | Работники в составе бригады | 1 раз в 3 года |\n"
    )
    assert excerpt(body, "1 раз в 3 года") == (
        "4.4. Периодичность обучения по группам безопасности: Группа: 1 · "
        "Кто: Работники в составе бригады · Периодичность: 1 раз в 3 года"
    )
