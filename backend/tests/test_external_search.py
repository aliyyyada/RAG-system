"""API смыслового поиска для внешних систем: POST /api/integration/v1/search."""

import json
from collections.abc import Iterator

import httpx
import pytest
from sqlalchemy import select

from app.api.external_search import reset_rights_cache
from app.config import get_settings
from app.db import get_sessionmaker
from app.models import AuditLog
from fakes.fake_source import FakeSource
from tests.test_ingest_sync import docs_by_key, first_load

pytestmark = pytest.mark.db
KEY = "test-external-key-0123456789abcdef"
URL = "/api/integration/v1/search"


@pytest.fixture
def api_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    monkeypatch.setenv("EXTERNAL_SEARCH_API_KEY", KEY)
    get_settings.cache_clear()
    reset_rights_cache()
    yield {"X-API-Key": KEY}
    get_settings.cache_clear()
    reset_rights_cache()


@pytest.fixture
async def guids(docflow: int, fake_source: FakeSource) -> dict[str, str]:
    await first_load(docflow)
    return {k: str(d.external_id) for k, d in (await docs_by_key(fake_source)).items()}


async def test_returns_only_allowed_guids(
    client: httpx.AsyncClient, api_key: dict[str, str], guids: dict[str, str]
) -> None:
    q = "наряд-допуск работы на высоте ответственный"
    r = await client.post(URL, json={"id_userboss": "T-0001", "query": q}, headers=api_key)
    assert r.status_code == 200, r.text
    res = r.json()
    print(json.dumps(res, ensure_ascii=False))
    assert guids["prikaz-112"] in res["ids"]
    assert guids["prikaz-45"] not in res["ids"]
    assert len(res["ids"]) == len(set(res["ids"]))
    allowed_t1 = {guids[k] for k in ("prikaz-112", "prikaz-140", "reglament-to")}
    assert set(res["ids"]) <= allowed_t1

    r = await client.post(URL, json={"id_userboss": "T-0002", "query": q}, headers=api_key)
    # Аналитику доступно всё: находит то же и больше (приказ 45 — скан, без tesseract
    # он не индексируется, поэтому проверяем через надмножество).
    t2 = set(r.json()["ids"])
    assert set(res["ids"]) <= t2 and len(t2) > len(res["ids"])

    r = await client.post(URL, json={"id_userboss": "T-0003", "query": q}, headers=api_key)
    assert r.json()["ids"] == []

    async with get_sessionmaker()() as s:
        rows = (
            await s.scalars(select(AuditLog).where(AuditLog.action == "integration.search"))
        ).all()
    assert len(rows) == 3 and rows[0].target_id == "T-0001"


async def test_key_and_fail_closed(
    client: httpx.AsyncClient,
    api_key: dict[str, str],
    guids: dict[str, str],
    fake_source: FakeSource,
) -> None:
    body = {"id_userboss": "T-0002", "query": "наряд"}
    assert (await client.post(URL, json=body)).status_code == 401
    assert (await client.post(URL, json=body, headers={"X-API-Key": "x"})).status_code == 401
    fake_source.unavailable = True
    r = await client.post(URL, json=body, headers=api_key)
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["code"] == "rights_unavailable"


async def test_disabled_without_key(client: httpx.AsyncClient, guids: dict[str, str]) -> None:
    get_settings.cache_clear()
    r = await client.post(URL, json={"id_userboss": "1", "query": "x"}, headers={"X-API-Key": "a"})
    assert r.status_code == 503
