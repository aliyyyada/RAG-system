"""Вход, блокировка, сессии и refresh-токены."""

import logging
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.auth.providers import get_auth_provider
from app.auth.providers.external_check import ExternalCheckProvider
from app.config import get_settings
from app.models import AuthSession
from tests.helpers import FAKE_PASSWORD, TEST_PASSWORD, auth, login, login_ok, seed_test_users

pytestmark = pytest.mark.db


async def test_local_test_user_login_sets_secure_refresh_cookie(client: httpx.AsyncClient) -> None:
    await seed_test_users()
    r = await login(client, "T-0001", TEST_PASSWORD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 15 * 60
    assert body["me"]["user"]["personnel_number"] == "T-0001"
    assert body["me"]["user"]["account_type"] == "test"
    assert body["me"]["permissions"] == ["documents.view", "search.common"]
    assert body["me"]["can_admin"] is False
    assert "password" not in r.text.lower()

    cookie = r.headers["set-cookie"]
    assert cookie.startswith("rag_refresh=")
    for attr in ("HttpOnly", "Secure", "SameSite=strict", "Path=/api/auth"):
        assert attr.lower() in cookie.lower(), cookie

    me = await client.get("/api/auth/me", headers=auth(body["access_token"]))
    assert me.json()["user"]["full_name"] == "Тест: обычный пользователь"


async def test_external_user_created_on_first_login_without_password(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    r = await login(client, " 10234 ", FAKE_PASSWORD)
    assert r.status_code == 200, r.text
    user = r.json()["me"]["user"]
    assert user["personnel_number"] == "10234"
    assert user["full_name"] == "Иванова Анна Сергеевна"
    assert user["department"] == "Служба ОТ и ПБ"
    assert user["account_type"] == "external"
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT password_hash FROM users WHERE personnel_number = '10234'")
            )
        ).one()
    assert row.password_hash is None  # пароль внешнего пользователя у нас не хранится


async def test_wrong_password_counts_attempts_then_locks(client: httpx.AsyncClient) -> None:
    for remaining in (4, 3, 2, 1):
        r = await login(client, "10234", "wrong-password")
        assert r.status_code == 401
        assert r.json()["detail"] == {
            "code": "invalid_credentials",
            "message": "Неверный табельный номер или пароль",
            "remaining_attempts": remaining,
            "lockout_minutes": 15,
        }
    r = await login(client, "10234", "wrong-password")
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "locked"
    # Даже правильный пароль не пускает, пока действует блокировка.
    r = await login(client, "10234", FAKE_PASSWORD)
    assert r.status_code == 429


async def test_lockout_expires(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    for _ in range(5):
        await login(client, "10234", "wrong")
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE login_lockouts SET locked_until = now() - interval '1 second'")
        )
    r = await login(client, "10234", "wrong")
    assert r.json()["detail"]["remaining_attempts"] == 4  # отсчёт начался заново
    r = await login(client, "10234", FAKE_PASSWORD)
    assert r.status_code == 200


async def test_success_resets_failed_attempts(client: httpx.AsyncClient) -> None:
    await login(client, "10234", "wrong")
    await login(client, "10234", "wrong")
    assert (await login(client, "10234", FAKE_PASSWORD)).status_code == 200
    r = await login(client, "10234", "wrong")
    assert r.json()["detail"]["remaining_attempts"] == 4


async def test_unknown_user_is_indistinguishable(client: httpx.AsyncClient) -> None:
    r = await login(client, "99999", "whatever")
    assert r.status_code == 401
    assert r.json()["detail"]["message"] == "Неверный табельный номер или пароль"


async def test_provider_unavailable_does_not_count_as_failure(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    overrides = client.app.dependency_overrides  # type: ignore[attr-defined]
    working = overrides[get_auth_provider]
    overrides[get_auth_provider] = lambda: ExternalCheckProvider(
        get_settings(), transport=httpx.MockTransport(broken)
    )
    secret = "Sup3r-Secret-Value!"
    with caplog.at_level(logging.DEBUG):
        r = await login(client, "10234", secret)
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "provider_unavailable"
    assert secret not in caplog.text  # пароль не попадает в логи

    overrides[get_auth_provider] = working
    r = await login(client, "10234", "wrong")
    assert r.json()["detail"]["remaining_attempts"] == 4  # сбой сервиса не съел попытку


async def test_password_never_logged_on_failed_login(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "Another-Secret-123"
    with caplog.at_level(logging.DEBUG):
        await login(client, "10234", secret)
        await login(client, "T-0001", secret)
    assert secret not in caplog.text


async def test_password_not_in_validation_error(client: httpx.AsyncClient) -> None:
    secret = "Leaky-Secret-456"
    r = await client.post("/api/auth/login", json={"login": "", "password": secret})
    assert r.status_code == 422
    assert secret not in r.text


async def test_refresh_rotates_token_and_old_one_works_only_briefly(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await seed_test_users()
    r = await login(client, "T-0001", TEST_PASSWORD)
    first = client.cookies.get("rag_refresh", path="/api/auth")
    assert first

    r = await client.post("/api/auth/refresh")
    assert r.status_code == 200, r.text
    second = client.cookies.get("rag_refresh", path="/api/auth")
    assert second and second != first
    me = await client.get("/api/auth/me", headers=auth(r.json()["access_token"]))
    assert me.json()["user"]["personnel_number"] == "T-0001"

    # Вторая вкладка пришла со старым токеном в течение минуты — пускаем, cookie не трогаем.
    client.cookies.clear()
    client.cookies.set("rag_refresh", first, path="/api/auth")
    r = await client.post("/api/auth/refresh")
    assert r.status_code == 200
    assert "set-cookie" not in r.headers

    # Через час старый токен — признак кражи: сессия закрывается целиком.
    async with engine.begin() as conn:
        await conn.execute(
            update(AuthSession).values(rotated_at=datetime.now(UTC) - timedelta(hours=1))
        )
    r = await client.post("/api/auth/refresh")
    assert r.status_code == 401
    client.cookies.clear()
    client.cookies.set("rag_refresh", second, path="/api/auth")
    assert (await client.post("/api/auth/refresh")).status_code == 401


async def test_refresh_without_cookie(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/auth/refresh")
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "no_session"


async def test_logout_revokes_session_immediately(client: httpx.AsyncClient) -> None:
    await seed_test_users()
    r = await login(client, "T-0001", TEST_PASSWORD)
    token = r.json()["access_token"]
    assert (await client.post("/api/auth/logout")).status_code == 204
    # access-токен ещё не истёк, но сессия закрыта — запрос отклоняется сразу.
    r = await client.get("/api/auth/me", headers=auth(token))
    assert r.status_code == 401
    assert (await client.post("/api/auth/refresh")).status_code == 401


async def test_logout_all_closes_every_device(client: httpx.AsyncClient) -> None:
    await seed_test_users()
    t1 = await login_ok(client, "T-0001")
    t2 = await login_ok(client, "T-0001")
    assert (await client.post("/api/auth/logout-all", headers=auth(t1))).status_code == 204
    assert (await client.get("/api/auth/me", headers=auth(t2))).status_code == 401


async def test_tampered_or_expired_token_rejected(client: httpx.AsyncClient) -> None:
    await seed_test_users()
    token = await login_ok(client, "T-0001")
    r = await client.get("/api/auth/me", headers=auth(token[:-2] + "xx"))
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "token_invalid"

    secret = get_settings().jwt_secret.get_secret_value()
    payload = jwt.decode(token, secret, algorithms=["HS256"])
    payload["exp"] = int((datetime.now(UTC) - timedelta(seconds=5)).timestamp())
    stale = jwt.encode(payload, secret, algorithm="HS256")
    assert (await client.get("/api/auth/me", headers=auth(stale))).status_code == 401


async def test_test_users_rejected_when_disabled(client: httpx.AsyncClient) -> None:
    await seed_test_users()
    token = await login_ok(client, "T-0001")
    off = get_settings().model_copy(update={"enable_test_users": False})
    client.app.dependency_overrides[get_settings] = lambda: off  # type: ignore[attr-defined]
    r = await login(client, "T-0001", TEST_PASSWORD)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "disabled"
    # Уже выданная сессия тестовой учётки тоже перестаёт работать.
    assert (await client.get("/api/auth/me", headers=auth(token))).status_code == 401


async def test_prod_allows_only_emergency_local_account(client: httpx.AsyncClient) -> None:
    from app.cli import create_local_user

    await create_local_user("admin", "Аварийный администратор", True, True, "emergency-pass-1")
    await create_local_user("local2", "Просто локальная", False, False, "local-password-2")
    prod = get_settings().model_copy(update={"app_env": "prod", "enable_test_users": False})
    client.app.dependency_overrides[get_settings] = lambda: prod  # type: ignore[attr-defined]
    assert (await login(client, "local2", "local-password-2")).status_code == 403
    r = await login(client, "admin", "emergency-pass-1")
    assert r.status_code == 200
    assert r.json()["me"]["is_admin"] is True


async def test_emergency_login_is_audited(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    from app.cli import create_local_user

    await create_local_user("admin", "Аварийный администратор", True, True, "emergency-pass-1")
    assert (await login(client, "admin", "emergency-pass-1")).status_code == 200
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT action, details FROM audit_log ORDER BY id"))).all()
    assert ("auth.local_login", {"emergency": True}) in [(r.action, r.details) for r in rows]


async def test_login_journal(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    await login(client, "10234", "wrong")
    await login(client, "10234", FAKE_PASSWORD)
    await client.post("/api/auth/logout")
    async with engine.connect() as conn:
        sql = text("SELECT login, result FROM login_events ORDER BY id")
        rows = (await conn.execute(sql)).all()
    assert [tuple(r) for r in rows] == [
        ("10234", "invalid_credentials"),
        ("10234", "success"),
        ("10234", "logout"),
    ]
