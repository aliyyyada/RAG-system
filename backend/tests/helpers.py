"""Помощники для тестов входа и прав."""

from httpx import AsyncClient, Response

from app.cli import TEST_PASSWORD, seed_test_users
from fakes.fake_service import FAKE_PASSWORD

__all__ = ["FAKE_PASSWORD", "TEST_PASSWORD", "auth", "login", "login_ok", "seed_test_users"]


async def login(client: AsyncClient, user: str, password: str) -> Response:
    return await client.post("/api/auth/login", json={"login": user, "password": password})


async def login_ok(client: AsyncClient, user: str, password: str = TEST_PASSWORD) -> str:
    r = await login(client, user, password)
    assert r.status_code == 200, r.text
    # У каждого «пользователя» в тесте своя сессия: cookie одного не должна влиять на другого.
    client.cookies.clear()
    return r.json()["access_token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
