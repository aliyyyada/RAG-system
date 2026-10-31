import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app


async def test_liveness() -> None:
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://t") as c:
        r = await c.get("/api/health")
    assert r.json() == {"status": "ok"}


@pytest.mark.db
async def test_readiness_with_database_and_no_ollama(database_url: str) -> None:
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://t") as c:
        r = await c.get("/api/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["database"] is True
    assert body["ollama"]["ok"] is False  # в тестах Ollama нет — это не делает систему «не готовой»
