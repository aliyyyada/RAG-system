"""API админки источников и документов, ручная загрузка, API для самого источника."""

import httpx
import pytest

from fakes.fake_source import FILES_DIR, FakeSource
from tests.helpers import auth, login_ok, seed_test_users
from tests.test_ingest_sync import drain

pytestmark = pytest.mark.db

ADMIN_ENDPOINTS = (
    ("GET", "/api/admin/sources"),
    ("POST", "/api/admin/sources"),
    ("GET", "/api/admin/sources/1"),
    ("PATCH", "/api/admin/sources/1"),
    ("POST", "/api/admin/sources/1/sync"),
    ("POST", "/api/admin/sources/1/retry-failed"),
    ("POST", "/api/admin/sources/1/callback-token"),
    ("GET", "/api/admin/collections"),
    ("GET", "/api/admin/documents"),
    ("GET", "/api/admin/documents/1"),
    ("GET", "/api/admin/documents/1/chunks"),
    ("GET", "/api/admin/documents/1/files/1/text"),
    ("POST", "/api/admin/documents/1/retry"),
    ("POST", "/api/admin/documents/1/reindex"),
    ("POST", "/api/admin/documents/1/exclude"),
    ("PATCH", "/api/admin/documents/1"),
)


@pytest.fixture
async def admin(client: httpx.AsyncClient, docflow: int) -> dict[str, str]:
    await seed_test_users()
    return auth(await login_ok(client, "T-0004"))


async def _loaded(client: httpx.AsyncClient, admin: dict[str, str]) -> dict:
    r = await client.post("/api/admin/sources/1/sync", json={}, headers=admin)
    assert r.status_code == 200 and r.json() == {"queued": True}, r.text
    await drain()
    r = await client.get("/api/admin/sources/1", headers=admin)
    assert r.status_code == 200, r.text
    return r.json()


async def test_admin_endpoints_closed_for_guest_and_plain_user(
    client: httpx.AsyncClient, docflow: int
) -> None:
    await seed_test_users()
    user = auth(await login_ok(client, "T-0001"))
    for method, url in ADMIN_ENDPOINTS:
        body = {} if method != "GET" else None
        r = await client.request(method, url, json=body)
        assert r.status_code == 401, (method, url, r.status_code)
        r = await client.request(method, url, json=body, headers=user)
        assert r.status_code == 403, (method, url, r.status_code)
    r = await client.post(
        "/api/documents/manual", data={"title": "x"}, files={"file": ("a.pdf", b"%PDF")}
    )
    assert r.status_code == 401
    r = await client.post(
        "/api/documents/manual",
        data={"title": "x"},
        files={"file": ("a.pdf", b"%PDF")},
        headers=user,
    )
    assert r.status_code == 403


async def test_completeness_report(client: httpx.AsyncClient, admin: dict[str, str]) -> None:
    src = await _loaded(client, admin)
    rep = src["report"]
    assert src["available"] is True and src["code"] == "docflow"
    assert (rep["at_source"], rep["known"], rep["ready"], rep["error"], rep["queued"]) == (
        8,
        8,
        6,
        2,
        0,
    )
    assert rep["errors_by_stage"] == {"parse": 1, "completeness": 1}
    assert rep["progress"] == 0.75
    assert rep["runs"][0]["kind"] == "reconcile" and rep["runs"][0]["status"] == "ok"

    r = await client.get("/api/admin/documents?status=error", headers=admin)
    errors = {d["number"]: d for d in r.json()["items"]}
    assert set(errors) == {"97", "Ф-3"}
    assert errors["97"]["error_stage_title"] == "Парсинг"
    assert (
        errors["97"]["caption"]
        == "Приказ № 97 от 03.07.2019 «О проведении внеплановых инструктажей»"
    )
    assert errors["Ф-3"]["error_stage_title"] == "Проверка полноты"

    r = await client.get("/api/admin/collections", headers=admin)
    colls = {c["name"]: c for c in r.json()}
    assert colls["Документооборот"]["documents"] == 8 and colls["Документооборот"]["errors"] == 2
    assert colls["Ручные загрузки"]["documents"] == 0


async def test_document_card_chunks_and_actions(
    client: httpx.AsyncClient, admin: dict[str, str], fake_source: FakeSource
) -> None:
    await _loaded(client, admin)
    r = await client.get("/api/admin/documents?q=О допуске", headers=admin)
    doc = r.json()["items"][0]
    r = await client.get(f"/api/admin/documents/{doc['id']}", headers=admin)
    card = r.json()
    assert card["files"][0]["name"] == "prikaz_112.pdf" and card["files"][0]["pages"] == 2
    assert card["parts_summary"][0]["name"] == "Ознакомлены"
    assert {e["event"] for e in card["events"]} >= {"discovered", "indexed"}

    r = await client.get(f"/api/admin/documents/{doc['id']}/chunks?page_size=200", headers=admin)
    chunks = r.json()
    assert chunks["total"] == card["chunk_count"] > 0
    assert any(
        c["page_from"] == 2 and c["section"] == "4. Порядок допуска" for c in chunks["items"]
    )

    fid = card["files"][0]["id"]
    r = await client.get(f"/api/admin/documents/{doc['id']}/files/{fid}/text", headers=admin)
    assert "--- стр. 2 ---" in r.json()["text"]

    # Исключить из поиска и вернуть.
    r = await client.post(
        f"/api/admin/documents/{doc['id']}/exclude", json={"excluded": True}, headers=admin
    )
    assert r.json()["is_excluded"] is True
    # Признак «общий» у документа источника задаёт источник.
    r = await client.patch(
        f"/api/admin/documents/{doc['id']}", json={"is_common": True}, headers=admin
    )
    assert r.status_code == 409

    # Переиндексировать: документ снова в очереди, после обработки — готов.
    r = await client.post(f"/api/admin/documents/{doc['id']}/reindex", headers=admin)
    assert r.json()["status"] == "queued"
    await drain()
    r = await client.get(f"/api/admin/documents/{doc['id']}", headers=admin)
    assert r.json()["status"] == "ready"
    assert {"reindex", "excluded"} <= {e["event"] for e in r.json()["events"]}


async def test_retry_all_failed(
    client: httpx.AsyncClient, admin: dict[str, str], fake_source: FakeSource
) -> None:
    await _loaded(client, admin)
    # Источник дослал недостающие строки журнала.
    journal = fake_source.docs[fake_source.id_of("journal-f3")]
    journal["parts"][0]["row_count"] = len(journal["parts"][0]["rows"])
    r = await client.post("/api/admin/sources/1/retry-failed", headers=admin)
    assert r.json() == {"count": 2}
    await drain()
    rep = (await client.get("/api/admin/sources/1", headers=admin)).json()["report"]
    assert (rep["ready"], rep["error"]) == (7, 1)


async def test_create_and_update_source(client: httpx.AsyncClient, admin: dict[str, str]) -> None:
    body = {"code": "archive", "name": "Архив", "base_url": "http://archive/api"}
    r = await client.post("/api/admin/sources", json=body, headers=admin)
    assert r.status_code == 422  # только https
    body["base_url"] = "https://archive/api"
    r = await client.post("/api/admin/sources", json=body, headers=admin)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    r = await client.post("/api/admin/sources", json=body, headers=admin)
    assert r.status_code == 409

    r = await client.patch(
        f"/api/admin/sources/{sid}",
        json={"sync_interval_minutes": 30, "reconcile_hour": 3, "name": "Архив ОТК"},
        headers=admin,
    )
    assert r.status_code == 200
    assert (r.json()["sync_interval_minutes"], r.json()["reconcile_hour"]) == (30, 3)
    colls = (await client.get("/api/admin/collections", headers=admin)).json()
    assert "Архив ОТК" in {c["name"] for c in colls}


async def test_manual_upload(client: httpx.AsyncClient, admin: dict[str, str]) -> None:
    data = (FILES_DIR / "iot_044.docx").read_bytes()
    r = await client.post(
        "/api/documents/manual",
        data={"title": "Тестовая инструкция", "number": "ИОТ-1", "is_common": "true"},
        files={"file": ("инструкция.docx", data)},
        headers=admin,
    )
    assert r.status_code == 201, r.text
    doc_id = r.json()["id"]
    await drain()
    card = (await client.get(f"/api/admin/documents/{doc_id}", headers=admin)).json()
    assert card["status"] == "ready" and card["manual"] is True and card["is_common"] is True
    assert card["collection_name"] == "Ручные загрузки"
    assert card["chunk_count"] > 0

    # У ручной загрузки признак «общий» ставит администратор.
    r = await client.patch(
        f"/api/admin/documents/{doc_id}", json={"is_common": False}, headers=admin
    )
    assert r.status_code == 200 and r.json()["is_common"] is False

    r = await client.post(
        "/api/documents/manual",
        data={"title": "Картинка"},
        files={"file": ("x.pdf", b"GIF89a....")},
        headers=admin,
    )
    assert r.status_code == 422 and r.json()["detail"]["code"] == "bad_type"


async def test_integration_api_for_source(
    client: httpx.AsyncClient, admin: dict[str, str], fake_source: FakeSource
) -> None:
    await _loaded(client, admin)
    url = "/api/integration/v1/sources/docflow/documents"
    assert (await client.get(url)).status_code == 401

    token = (await client.post("/api/admin/sources/1/callback-token", headers=admin)).json()[
        "token"
    ]
    src = auth(token)
    r = await client.get(url, headers=src)
    assert r.status_code == 200 and len(r.json()["items"]) == 8
    assert (await client.get(url, headers=auth(token + "x"))).status_code == 401
    assert (
        await client.get("/api/integration/v1/sources/other/documents", headers=src)
    ).status_code == 401

    ids = [fake_source.id_of("prikaz-112"), fake_source.id_of("prikaz-97"), "no-such-id"]
    r = await client.post(url + "/lookup", json={"ids": ids}, headers=src)
    out = r.json()
    assert out[0]["status"] == "ready" and out[0]["indexed_version"] is not None
    assert out[1]["status"] == "error" and "пустой" in out[1]["error"]
    assert out[2]["status"] == "unknown"

    # Новый токен отменяет старый.
    await client.post("/api/admin/sources/1/callback-token", headers=admin)
    assert (await client.get(url, headers=src)).status_code == 401
