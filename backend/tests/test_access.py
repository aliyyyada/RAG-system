"""Этап 3: доступ к документам по правам источника.

Главное правило: человек не видит чужой документ ни в списке, ни в фильтрах, ни по прямой
ссылке, ни во фрагментах текста, ни при скачивании файла. Права даёт источник (фейковый,
таблица ACCESS в fakes/fake_source.py), а не роль в нашей системе.

Кто что видит в «Документообороте» после загрузки (готовы 6 документов из 8):
    общие: ИОТ-044, Положение о СУОТ — все, включая гостя;
    T-0001: + приказ 112, приказ 140, регламент ТО;
    T-0003: только общие;  T-0002, T-0004: всё.
"""

import asyncio
import time
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text

from app.config import get_settings
from app.db import get_sessionmaker
from app.ingest import sync
from app.ingest.service import create_source
from app.models import AccessCache, User
from app.sources.http_json import HttpJsonSource
from fakes.fake_source import ARCHIVE, FILES_DIR, FakeSource
from tests.helpers import FAKE_PASSWORD, auth, login_ok, seed_test_users
from tests.test_ingest_sync import docs_by_key, drain, first_load

pytestmark = pytest.mark.db

COMMON = {"iot-044", "polozhenie-suot"}
READY = COMMON | {"prikaz-112", "prikaz-45", "prikaz-140", "reglament-to"}
T0001 = COMMON | {"prikaz-112", "prikaz-140", "reglament-to"}


@pytest.fixture
async def ids(client: httpx.AsyncClient, docflow: int, fake_source: FakeSource) -> dict[str, int]:
    """Загруженный «Документооборот»: ключ документа фейкового источника → наш id."""
    await seed_test_users()
    await first_load(docflow)
    return {k: d.id for k, d in (await docs_by_key(fake_source)).items()}


async def _keys(client: httpx.AsyncClient, ids: dict[str, int], headers: Any = None, **q: Any):  # type: ignore[no-untyped-def]
    r = await client.get("/api/documents", params=q, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    by_id = {v: k for k, v in ids.items()}
    return {by_id.get(d["id"], f"#{d['id']}") for d in body["items"]}, body


async def _file_id(client: httpx.AsyncClient, doc_id: int, headers: Any) -> int:
    r = await client.get(f"/api/documents/{doc_id}", headers=headers)
    assert r.status_code == 200, r.text
    return int(r.json()["files"][0]["id"])


async def _assert_closed(
    client: httpx.AsyncClient, doc_id: int, headers: Any, code: str, file_id: int | None = None
) -> None:
    """Документ закрыт везде: карточка, фрагменты, файл."""
    urls = [f"/api/documents/{doc_id}", f"/api/documents/{doc_id}/chunks"]
    if file_id is not None:
        urls.append(f"/api/documents/{doc_id}/files/{file_id}")
    for url in urls:
        r = await client.get(url, headers=headers)
        assert r.status_code in (403, 503), (url, r.status_code, r.text)
        assert r.json()["detail"]["code"] == code, (url, r.text)
        # В ответе об отказе нет ни названия, ни текста документа.
        assert "высот" not in r.text


# ---------------------------------------------------------------------------
# Кто что видит
# ---------------------------------------------------------------------------


async def test_guest_sees_only_common(client: httpx.AsyncClient, ids: dict[str, int]) -> None:
    keys, body = await _keys(client, ids)
    assert keys == COMMON
    assert body["total"] == 2 and body["total_in_system"] == 6
    assert body["unavailable_sources"] == []

    r = await client.get("/api/documents/filters")
    assert r.json()["doc_types"] == ["Инструкция", "Положение"]

    admin = auth(await login_ok(client, "T-0004"))
    file_112 = await _file_id(client, ids["prikaz-112"], admin)
    await _assert_closed(client, ids["prikaz-112"], None, "login_required", file_112)

    # Общий документ гостю открыт полностью, включая файл.
    file_iot = await _file_id(client, ids["iot-044"], None)
    r = await client.get(f"/api/documents/{ids['iot-044']}/files/{file_iot}")
    assert r.status_code == 200
    assert r.content == (FILES_DIR / "iot_044.docx").read_bytes()
    assert "attachment" in r.headers["content-disposition"]
    r = await client.get(f"/api/documents/{ids['iot-044']}/chunks")
    assert r.status_code == 200 and r.json()["total"] > 0


async def test_user_sees_only_documents_allowed_by_source(
    client: httpx.AsyncClient, ids: dict[str, int]
) -> None:
    user = auth(await login_ok(client, "T-0001"))
    keys, body = await _keys(client, ids, user)
    assert keys == T0001
    assert body["total"] == 5 and body["total_in_system"] == 6

    # Поиск по названию не находит закрытый документ: «О работах на высоте» (приказ 45).
    keys, _ = await _keys(client, ids, user, q="высот")
    assert keys == {"prikaz-112"}
    keys, _ = await _keys(client, ids, user, q="45")
    assert keys == set()

    admin = auth(await login_ok(client, "T-0004"))
    file_45 = await _file_id(client, ids["prikaz-45"], admin)
    await _assert_closed(client, ids["prikaz-45"], user, "no_access", file_45)
    r = await client.get(f"/api/documents/{ids['prikaz-45']}", headers=user)
    assert r.json()["detail"]["message"] == (
        "Источник «Документооборот» не выдал вам права на этот документ"
    )

    # Разрешённый документ открывается целиком: реквизиты, текст, файл.
    r = await client.get(f"/api/documents/{ids['prikaz-112']}", headers=user)
    card = r.json()
    assert card["caption"].startswith("Приказ № 112 от 14.03.2024")
    assert (
        card["source_name"] == "Документооборот" and card["legal_status"] == "Действует, изменён"
    )  # этап 5: приказ № 140 изменяет № 112
    assert {"label": "Подразделение", "value": "Служба ОТ и ПБ"} in card["fields"]
    r = await client.get(f"/api/documents/{ids['prikaz-112']}/chunks", headers=user)
    assert any("наряд" in c["text"].lower() for c in r.json()["items"])
    r = await client.get(
        f"/api/documents/{ids['prikaz-112']}/files/{card['files'][0]['id']}", headers=user
    )
    assert r.status_code == 200 and r.content[:4] == b"%PDF"

    # Чужой файл не скачать, подставив его id к своему документу.
    r = await client.get(f"/api/documents/{ids['prikaz-112']}/files/{file_45}", headers=user)
    assert r.status_code == 404

    # Фильтры строятся только из доступных документов.
    r = await client.get("/api/documents/filters", headers=user)
    f = r.json()
    assert f["doc_types"] == ["Инструкция", "Положение", "Приказ", "Регламент"]
    assert f["sources"] == [{"value": "1", "label": "Документооборот"}]


async def test_no_access_to_source_means_only_common(
    client: httpx.AsyncClient, ids: dict[str, int]
) -> None:
    user = auth(await login_ok(client, "T-0003"))
    keys, body = await _keys(client, ids, user)
    assert keys == COMMON and body["unavailable_sources"] == []
    await _assert_closed(client, ids["prikaz-112"], user, "no_access")


async def test_document_rights_come_from_source_not_from_role(
    client: httpx.AsyncClient, ids: dict[str, int], fake_source: FakeSource
) -> None:
    analyst = auth(await login_ok(client, "T-0002"))
    keys, _ = await _keys(client, ids, analyst)
    assert keys == READY

    # Администратор системы видит документы по правам источника, как все.
    fake_source.access["T-0004"] = {"prikaz-140"}
    admin = auth(await login_ok(client, "T-0004"))
    keys, _ = await _keys(client, ids, admin)
    assert keys == COMMON | {"prikaz-140"}
    # А в админке (отладка разбора, вопрос 15) видит реестр целиком.
    r = await client.get("/api/admin/documents", headers=admin)
    assert r.json()["total"] == 8


async def test_external_user_rights_by_personnel_number(
    client: httpx.AsyncClient, ids: dict[str, int]
) -> None:
    ivanova = auth(await login_ok(client, "10234", FAKE_PASSWORD))
    keys, _ = await _keys(client, ids, ivanova)
    # Приказ 97 и журнал Ф-3 ей разрешены, но не загружены (ошибка разбора) — их нет.
    assert keys == COMMON | {"prikaz-112", "prikaz-45", "prikaz-140"}

    petrov = auth(await login_ok(client, "20871", FAKE_PASSWORD))
    keys, _ = await _keys(client, ids, petrov)
    assert keys == COMMON | {"prikaz-112", "reglament-to"}


async def test_without_documents_view_only_common(
    client: httpx.AsyncClient, ids: dict[str, int], engine: Any
) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM role_permissions WHERE permission = 'documents.view'"))
    user = auth(await login_ok(client, "T-0001"))
    keys, _ = await _keys(client, ids, user)
    assert keys == COMMON
    await _assert_closed(client, ids["prikaz-112"], user, "no_access")


async def test_guest_mode_off_closes_documents(
    client: httpx.AsyncClient, ids: dict[str, int]
) -> None:
    s = get_settings().model_copy(update={"guest_mode_enabled": False})
    client.app.dependency_overrides[get_settings] = lambda: s  # type: ignore[attr-defined]
    try:
        r = await client.get("/api/documents")
        assert r.status_code == 401
        r = await client.get(f"/api/documents/{ids['iot-044']}")
        assert r.status_code == 401
    finally:
        client.app.dependency_overrides.pop(get_settings)  # type: ignore[attr-defined]


async def test_excluded_and_deleted_documents_disappear(
    client: httpx.AsyncClient, ids: dict[str, int], docflow: int, fake_source: FakeSource
) -> None:
    admin = auth(await login_ok(client, "T-0004"))
    user = auth(await login_ok(client, "T-0001"))
    r = await client.post(
        f"/api/admin/documents/{ids['prikaz-112']}/exclude", json={"excluded": True}, headers=admin
    )
    assert r.status_code == 200
    fake_source.delete("prikaz-140")
    await sync.run_incremental(docflow, get_settings(), get_sessionmaker())
    await drain()

    keys, _ = await _keys(client, ids, user)
    assert keys == COMMON | {"reglament-to"}
    for key in ("prikaz-112", "prikaz-140"):
        r = await client.get(f"/api/documents/{ids[key]}", headers=user)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Кэш прав
# ---------------------------------------------------------------------------


async def _expire_cache() -> None:
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(text("UPDATE access_cache SET expires_at = now() - interval '1 second'"))


async def test_cache_is_used_and_reset(
    client: httpx.AsyncClient, ids: dict[str, int], fake_source: FakeSource
) -> None:
    user = auth(await login_ok(client, "T-0001"))
    calls = fake_source.access_calls
    await _keys(client, ids, user)
    await _keys(client, ids, user)
    await client.get(f"/api/documents/{ids['prikaz-112']}", headers=user)
    assert fake_source.access_calls == calls + 1  # один запрос к источнику на три обращения

    # В источнике выдали права на приказ 45 — пока кэш жив, мы его не видим.
    fake_source.access["T-0001"] = T0001 | {"prikaz-45"}
    keys, _ = await _keys(client, ids, user)
    assert "prikaz-45" not in keys
    await _expire_cache()
    keys, _ = await _keys(client, ids, user)
    assert "prikaz-45" in keys
    assert fake_source.access_calls == calls + 2

    # Права отозвали — сбрасываем кэш кнопкой в админке.
    fake_source.access["T-0001"] = T0001
    admin = auth(await login_ok(client, "T-0004"))
    async with get_sessionmaker()() as s:
        uid = await s.scalar(select(User.id).where(User.personnel_number == "T-0001"))
    r = await client.post("/api/admin/access-cache/reset", json={"user_id": uid}, headers=admin)
    assert r.status_code == 200 and r.json()["reset"] == 1
    keys, _ = await _keys(client, ids, user)
    assert "prikaz-45" not in keys

    # Вход сбрасывает кэш этого человека.
    fake_source.access["T-0001"] = T0001 | {"prikaz-45"}
    user = auth(await login_ok(client, "T-0001"))
    keys, _ = await _keys(client, ids, user)
    assert "prikaz-45" in keys

    # Сброс для всех.
    r = await client.post("/api/admin/access-cache/reset", json={}, headers=admin)
    assert r.status_code == 200 and r.json()["reset"] >= 1
    async with get_sessionmaker()() as s:
        assert (await s.scalars(select(AccessCache))).all() == []


# ---------------------------------------------------------------------------
# Источник не ответил: fail-closed
# ---------------------------------------------------------------------------


async def test_source_unavailable_hides_its_documents(
    client: httpx.AsyncClient, ids: dict[str, int], fake_source: FakeSource
) -> None:
    user = auth(await login_ok(client, "T-0001"))
    fake_source.unavailable = True
    keys, body = await _keys(client, ids, user)
    # Общие документы видны: их признак уже в нашем реестре, спрашивать источник не нужно.
    assert keys == COMMON
    assert body["unavailable_sources"] == [
        {
            "id": 1,
            "name": "Документооборот",
            "message": "Документы из системы «Документооборот» сейчас недоступны",
        }
    ]
    await _assert_closed(client, ids["prikaz-112"], user, "source_unavailable")
    r = await client.get(f"/api/documents/{ids['prikaz-112']}", headers=user)
    assert r.status_code == 503

    # Неудачу помним полминуты: упавший источник не тормозит каждый запрос.
    calls = fake_source.access_calls
    await _keys(client, ids, user)
    assert fake_source.access_calls == calls

    fake_source.unavailable = False
    await _expire_cache()
    keys, body = await _keys(client, ids, user)
    assert keys == T0001 and body["unavailable_sources"] == []


async def test_source_timeout_hides_its_documents(
    client: httpx.AsyncClient, ids: dict[str, int], fake_source: FakeSource
) -> None:
    s = get_settings().model_copy(update={"access_timeout_seconds": 0.3})
    client.app.dependency_overrides[get_settings] = lambda: s  # type: ignore[attr-defined]
    try:
        user = auth(await login_ok(client, "T-0001"))
        fake_source.access_delay = 2
        started = time.monotonic()
        keys, body = await _keys(client, ids, user)
        assert time.monotonic() - started < 1.5
        assert keys == COMMON and [u["name"] for u in body["unavailable_sources"]] == [
            "Документооборот"
        ]
        async with get_sessionmaker()() as session:
            row = await session.scalar(select(AccessCache))
        assert row is not None and row.ok is False and "0.3" in (row.error or "")
    finally:
        client.app.dependency_overrides.pop(get_settings)  # type: ignore[attr-defined]


@pytest.fixture
async def archive(ids: dict[str, int]) -> dict[str, int]:
    """Второй источник «Архив ОТ»: два документа без файлов."""
    async with get_sessionmaker()() as s, s.begin():
        src = await create_source(
            s, code="archive", name="Архив ОТ", base_url="https://fake/archive"
        )
        source_id = src.id
    await first_load(source_id)
    loaded = await docs_by_key(ARCHIVE)
    assert {k for k, d in loaded.items() if d.status == "ready"} == set(ARCHIVE_KEYS)
    return {**ids, **{k: d.id for k, d in loaded.items()}}


ARCHIVE_KEYS = ("archive-ri-12", "archive-akt-5")


async def test_two_sources_one_down(client: httpx.AsyncClient, archive: dict[str, int]) -> None:
    user = auth(await login_ok(client, "T-0001"))
    keys, body = await _keys(client, archive, user)
    assert keys == T0001 | {"archive-ri-12"}
    keys, _ = await _keys(client, archive, user, source="2")
    assert keys == {"archive-ri-12"}

    ARCHIVE.unavailable = True
    await _expire_cache()
    keys, body = await _keys(client, archive, user)
    assert keys == T0001
    assert [u["name"] for u in body["unavailable_sources"]] == ["Архив ОТ"]
    await _assert_closed(client, archive["archive-ri-12"], user, "source_unavailable")


async def test_sources_are_asked_in_parallel(
    client: httpx.AsyncClient, archive: dict[str, int], fake_source: FakeSource
) -> None:
    user = auth(await login_ok(client, "T-0001"))
    fake_source.access_delay = ARCHIVE.access_delay = 0.6
    started = time.monotonic()
    keys, body = await _keys(client, archive, user)
    elapsed = time.monotonic() - started
    assert keys == T0001 | {"archive-ri-12"} and body["unavailable_sources"] == []
    assert elapsed < 1.1, f"источники опрошены последовательно: {elapsed:.2f} с"


async def test_concurrent_requests_of_one_user(
    client: httpx.AsyncClient, ids: dict[str, int]
) -> None:
    user = auth(await login_ok(client, "T-0001"))
    results = await asyncio.gather(*(_keys(client, ids, user) for _ in range(5)))
    assert all(keys == T0001 for keys, _ in results)


async def test_adapter_unknown_person_has_no_rights(fake_source: FakeSource) -> None:
    src = HttpJsonSource("https://fake/source", get_settings(), transport=_transport())
    try:
        assert await src.user_document_ids("99999") == set()
        assert await src.user_document_ids("31455") == set()
        assert await src.user_document_ids("20871") == {
            fake_source.id_of("prikaz-112"),
            fake_source.id_of("reglament-to"),
        }
    finally:
        await src.aclose()


def _transport() -> httpx.AsyncBaseTransport:
    from fakes.fake_service import app as fake_app

    return httpx.ASGITransport(app=fake_app)


# ---------------------------------------------------------------------------
# Ручные загрузки: доступ задаёт администратор
# ---------------------------------------------------------------------------


async def test_manual_upload_access_by_people_and_groups(
    client: httpx.AsyncClient, ids: dict[str, int]
) -> None:
    admin = auth(await login_ok(client, "T-0004"))
    r = await client.post(
        "/api/documents/manual",
        data={"title": "СТП 08-2023 Обучение по охране труда", "number": "СТП 08-2023"},
        files={"file": ("stp.pdf", (FILES_DIR / "prikaz_112.pdf").read_bytes())},
        headers=admin,
    )
    assert r.status_code == 201, r.text
    doc = r.json()["id"]
    await drain()

    async def sees(login: str) -> bool:
        h = auth(await login_ok(client, login))
        keys, _ = await _keys(client, {"manual": doc}, h, source="manual")
        r = await client.get(f"/api/documents/{doc}", headers=h)
        assert (r.status_code == 200) == ("manual" in keys), r.text
        return "manual" in keys

    # Загрузивший видит свой документ сразу; остальные — нет.
    assert await sees("T-0004")
    assert not await sees("T-0001") and not await sees("T-0002")
    assert (await client.get(f"/api/documents/{doc}")).status_code == 403

    r = await client.get(f"/api/admin/documents/{doc}/access", headers=admin)
    acc = r.json()
    assert [u["personnel_number"] for u in acc["users"]] == ["T-0004"]
    group = next(g for g in acc["all_groups"] if g["name"] == "Тестовая группа аналитиков")

    r = await client.put(
        f"/api/admin/documents/{doc}/access",
        json={"personnel_numbers": ["T-0001", "55555"], "group_ids": [group["id"]]},
        headers=admin,
    )
    assert r.status_code == 200, r.text
    assert {u["personnel_number"] for u in r.json()["users"]} == {"T-0001", "55555"}
    assert r.json()["groups"] == [group]
    assert await sees("T-0001")  # лично
    assert await sees("T-0002")  # через группу аналитиков
    assert not await sees("T-0003")
    assert not await sees("T-0004")  # себя администратор из списка убрал

    # Человек 55555 ещё не входил — заведён заранее, без пароля и ролей.
    async with get_sessionmaker()() as s:
        pre = await s.scalar(select(User).where(User.personnel_number == "55555"))
    assert pre is not None and pre.account_type == "external" and pre.password_hash is None

    # Документам источника права задаёт источник.
    r = await client.put(
        f"/api/admin/documents/{ids['prikaz-112']}/access",
        json={"personnel_numbers": ["T-0001"]},
        headers=admin,
    )
    assert r.status_code == 409
    r = await client.put(
        f"/api/admin/documents/{doc}/access", json={"group_ids": [999]}, headers=admin
    )
    assert r.status_code == 422

    # Простой пользователь права менять не может.
    user = auth(await login_ok(client, "T-0001"))
    r = await client.put(f"/api/admin/documents/{doc}/access", json={}, headers=user)
    assert r.status_code == 403
