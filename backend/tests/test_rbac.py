"""RBAC и админка: кто что видит и кто что может выдать."""

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.auth.permissions import all_codes
from app.cli import create_admin
from app.config import get_settings
from tests.helpers import FAKE_PASSWORD, auth, login, login_ok, seed_test_users

pytestmark = pytest.mark.db

ADMIN_ENDPOINTS = (
    ("GET", "/api/admin/users"),
    ("GET", "/api/admin/users/1"),
    ("POST", "/api/admin/users"),
    ("POST", "/api/admin/users/local"),
    ("PATCH", "/api/admin/users/1"),
    ("POST", "/api/admin/users/1/unlock"),
    ("POST", "/api/admin/users/1/revoke-sessions"),
    ("GET", "/api/admin/groups"),
    ("POST", "/api/admin/groups"),
    ("PATCH", "/api/admin/groups/1"),
    ("DELETE", "/api/admin/groups/1"),
    ("GET", "/api/admin/roles"),
    ("POST", "/api/admin/roles"),
    ("PATCH", "/api/admin/roles/1"),
    ("DELETE", "/api/admin/roles/1"),
    ("GET", "/api/admin/permissions"),
)


async def _roles(client: httpx.AsyncClient, token: str) -> dict[str, dict]:
    r = await client.get("/api/admin/roles", headers=auth(token))
    assert r.status_code == 200, r.text
    return {x["name"]: x for x in r.json()}


async def _admin(client: httpx.AsyncClient) -> str:
    await seed_test_users()
    return await login_ok(client, "T-0004")


# ---------------------------------------------------------------------------
# Гость и обычный пользователь не попадают в админку
# ---------------------------------------------------------------------------


async def test_guest_sees_only_common_search(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["user"] is None
    assert r.json()["permissions"] == ["search.common"]
    assert r.json()["can_admin"] is False


async def test_guest_mode_disabled_gives_nothing(client: httpx.AsyncClient) -> None:
    off = get_settings().model_copy(update={"guest_mode_enabled": False})
    client.app.dependency_overrides[get_settings] = lambda: off  # type: ignore[attr-defined]
    r = await client.get("/api/auth/me")
    assert r.json()["permissions"] == []
    assert r.json()["guest_mode_enabled"] is False


@pytest.mark.parametrize(("method", "url"), ADMIN_ENDPOINTS)
async def test_guest_gets_401_on_admin_api(client: httpx.AsyncClient, method: str, url: str):
    r = await client.request(method, url, json={})
    assert r.status_code == 401


@pytest.mark.parametrize(("method", "url"), ADMIN_ENDPOINTS)
async def test_plain_user_gets_403_on_admin_api(client: httpx.AsyncClient, method: str, url: str):
    await seed_test_users()
    token = await login_ok(client, "T-0001")
    r = await client.request(method, url, json={}, headers=auth(token))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "forbidden"


async def test_admin_has_every_permission(client: httpx.AsyncClient) -> None:
    token = await _admin(client)
    me = (await client.get("/api/auth/me", headers=auth(token))).json()
    assert me["is_admin"] is True
    assert set(me["permissions"]) == all_codes()
    assert me["can_admin"] is True


# ---------------------------------------------------------------------------
# Роли
# ---------------------------------------------------------------------------


async def test_builtin_roles_listed_in_mockup_order(client: httpx.AsyncClient) -> None:
    token = await _admin(client)
    roles = list((await client.get("/api/admin/roles", headers=auth(token))).json())
    names = [r["name"] for r in roles]
    assert names[0] == "Гость" and names[1] == "Пользователь" and names[-1] == "Администратор"
    assert set(roles[-1]["permissions"]) == all_codes()


async def test_guest_cannot_get_more_than_common_documents(client: httpx.AsyncClient) -> None:
    token = await _admin(client)
    guest = (await _roles(client, token))["Гость"]
    r = await client.patch(
        f"/api/admin/roles/{guest['id']}",
        json={"permissions": ["search.common", "documents.view"]},
        headers=auth(token),
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "guest_limit"
    # Сузить можно: гостю совсем запрещён поиск.
    r = await client.patch(
        f"/api/admin/roles/{guest['id']}", json={"permissions": []}, headers=auth(token)
    )
    assert r.status_code == 200
    client.cookies.clear()
    assert (await client.get("/api/auth/me")).json()["permissions"] == []


async def test_guest_permissions_capped_even_if_db_is_edited(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO role_permissions SELECT id, 'admin.users' FROM roles "
                "WHERE builtin_key = 'guest'"
            )
        )
    assert (await client.get("/api/auth/me")).json()["permissions"] == ["search.common"]


async def test_builtin_roles_protected(client: httpx.AsyncClient) -> None:
    token = await _admin(client)
    roles = await _roles(client, token)
    for name in ("Гость", "Пользователь", "Администратор"):
        r = await client.delete(f"/api/admin/roles/{roles[name]['id']}", headers=auth(token))
        assert r.status_code == 422
    r = await client.patch(
        f"/api/admin/roles/{roles['Администратор']['id']}",
        json={"permissions": ["search.common"]},
        headers=auth(token),
    )
    assert r.status_code == 422
    r = await client.patch(
        f"/api/admin/roles/{roles['Пользователь']['id']}",
        json={"name": "Сотрудник"},
        headers=auth(token),
    )
    assert r.status_code == 422


async def test_unknown_permission_rejected(client: httpx.AsyncClient) -> None:
    token = await _admin(client)
    r = await client.post(
        "/api/admin/roles",
        json={"name": "Хакер", "permissions": ["root.everything"]},
        headers=auth(token),
    )
    assert r.status_code == 422


async def test_role_crud_and_duplicate_name(client: httpx.AsyncClient) -> None:
    token = await _admin(client)
    r = await client.post(
        "/api/admin/roles",
        json={"name": "Специалист ОТ", "permissions": ["contradictions.view", "search.common"]},
        headers=auth(token),
    )
    assert r.status_code == 201
    role = r.json()
    assert role["permissions"] == ["search.common", "contradictions.view"]  # порядок каталога
    dup = await client.post("/api/admin/roles", json={"name": "Специалист ОТ"}, headers=auth(token))
    assert dup.status_code == 409
    r = await client.patch(
        f"/api/admin/roles/{role['id']}",
        json={"permissions": ["skill.epoz_check.use"]},
        headers=auth(token),
    )
    assert r.json()["permissions"] == ["skill.epoz_check.use"]
    r = await client.delete(f"/api/admin/roles/{role['id']}", headers=auth(token))
    assert r.status_code == 204


async def test_permission_catalog_has_skills(client: httpx.AsyncClient) -> None:
    token = await _admin(client)
    r = await client.get("/api/admin/permissions", headers=auth(token))
    codes = {p["code"]: p for p in r.json()}
    assert codes["skill.epoz_check.use"]["coming_soon"] is True
    assert codes["search.common"]["guest_allowed"] is True
    assert codes["admin.users"]["guest_allowed"] is False


# ---------------------------------------------------------------------------
# Права = свои роли + роли групп, изменения действуют сразу
# ---------------------------------------------------------------------------


async def test_analyst_gets_permissions_through_group(client: httpx.AsyncClient) -> None:
    await seed_test_users()
    token = await login_ok(client, "T-0002")
    me = (await client.get("/api/auth/me", headers=auth(token))).json()
    assert "contradictions.view" in me["permissions"]
    assert "audit.view" in me["permissions"]
    assert me["can_admin"] is True  # audit.view открывает раздел администрирования
    assert "Аналитик" in me["roles"]
    # Но управлять людьми аналитик не может.
    assert (await client.get("/api/admin/users", headers=auth(token))).status_code == 403


async def test_role_change_applies_without_relogin(client: httpx.AsyncClient) -> None:
    admin = await _admin(client)
    user_token = await login_ok(client, "T-0001")
    assert (await client.get("/api/admin/users", headers=auth(user_token))).status_code == 403

    r = await client.post(
        "/api/admin/roles",
        json={"name": "Кадровик", "permissions": ["admin.users"]},
        headers=auth(admin),
    )
    role_id = r.json()["id"]
    users = (await client.get("/api/admin/users?q=T-0001", headers=auth(admin))).json()
    uid = users["items"][0]["id"]
    r = await client.patch(
        f"/api/admin/users/{uid}", json={"role_ids": [role_id]}, headers=auth(admin)
    )
    assert r.status_code == 200, r.text
    assert "admin.users" in r.json()["effective_permissions"]
    # Тот же access-токен — права уже новые.
    assert (await client.get("/api/admin/users", headers=auth(user_token))).status_code == 200


async def test_preassigned_roles_apply_on_first_external_login(client: httpx.AsyncClient) -> None:
    admin = await _admin(client)
    roles = await _roles(client, admin)
    r = await client.post(
        "/api/admin/users",
        json={"personnel_number": "20871", "role_ids": [roles["Администратор"]["id"]]},
        headers=auth(admin),
    )
    assert r.status_code == 200, r.text
    assert r.json()["account_type"] == "external"
    r = await login(client, "20871", FAKE_PASSWORD)
    assert r.json()["me"]["is_admin"] is True
    assert r.json()["me"]["user"]["full_name"] == "Петров Виктор Николаевич"


async def test_cli_create_admin_for_first_login(client: httpx.AsyncClient) -> None:
    await create_admin("00017", None)
    r = await login(client, "00017", FAKE_PASSWORD)
    assert r.status_code == 200
    assert r.json()["me"]["is_admin"] is True


# ---------------------------------------------------------------------------
# Защита от повышения прав и от потери последнего администратора
# ---------------------------------------------------------------------------


async def _hr_manager(client: httpx.AsyncClient, admin: str) -> tuple[str, dict[str, dict]]:
    """T-0001 получает роль «Кадровик» (только admin.users)."""
    await client.post(
        "/api/admin/roles",
        json={"name": "Кадровик", "permissions": ["admin.users", "search.common"]},
        headers=auth(admin),
    )
    await client.post(
        "/api/admin/roles",
        json={"name": "Аудитор", "permissions": ["audit.view"]},
        headers=auth(admin),
    )
    roles = await _roles(client, admin)
    await client.post(
        "/api/admin/users",
        json={"personnel_number": "T-0001", "role_ids": [roles["Кадровик"]["id"]]},
        headers=auth(admin),
    )
    return await login_ok(client, "T-0001"), roles


async def test_user_manager_cannot_grant_admin(client: httpx.AsyncClient) -> None:
    admin = await _admin(client)
    hr, roles = await _hr_manager(client, admin)
    r = await client.post(
        "/api/admin/users",
        json={"personnel_number": "T-0001", "role_ids": [roles["Администратор"]["id"]]},
        headers=auth(hr),
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "admin_only"


async def test_user_manager_cannot_grant_role_with_foreign_permissions(
    client: httpx.AsyncClient,
) -> None:
    admin = await _admin(client)
    hr, roles = await _hr_manager(client, admin)
    r = await client.post(
        "/api/admin/users",
        json={"personnel_number": "55555", "role_ids": [roles["Аудитор"]["id"]]},
        headers=auth(hr),
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "escalation"
    # Через группу — тоже нельзя.
    g = await client.post(
        "/api/admin/groups",
        json={"name": "Аудиторы", "role_ids": [roles["Аудитор"]["id"]]},
        headers=auth(admin),
    )
    users = (await client.get("/api/admin/users?q=T-0001", headers=auth(admin))).json()
    uid = users["items"][0]["id"]
    r = await client.patch(
        f"/api/admin/groups/{g.json()['id']}", json={"member_ids": [uid]}, headers=auth(hr)
    )
    assert r.status_code == 403
    # Роль в пределах своих прав выдать можно.
    r = await client.post(
        "/api/admin/users",
        json={"personnel_number": "55555", "role_ids": [roles["Кадровик"]["id"]]},
        headers=auth(hr),
    )
    assert r.status_code == 200


async def test_admin_area_permission_cannot_edit_roles_without_admin_roles(
    client: httpx.AsyncClient,
) -> None:
    admin = await _admin(client)
    hr, roles = await _hr_manager(client, admin)
    r = await client.patch(
        f"/api/admin/roles/{roles['Кадровик']['id']}",
        json={"permissions": ["admin.users", "admin.roles"]},
        headers=auth(hr),
    )
    assert r.status_code == 403


async def test_cannot_remove_last_admin(client: httpx.AsyncClient) -> None:
    admin = await _admin(client)
    me = (await client.get("/api/auth/me", headers=auth(admin))).json()
    uid = me["user"]["id"]
    r = await client.patch(f"/api/admin/users/{uid}", json={"role_ids": []}, headers=auth(admin))
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "last_admin"
    r = await client.patch(
        f"/api/admin/users/{uid}", json={"is_active": False}, headers=auth(admin)
    )
    assert r.status_code == 422  # самого себя не блокируем
    # Есть второй администратор через группу — теперь можно.
    roles = await _roles(client, admin)
    await create_admin("00017", "Сидоров")
    r = await client.patch(f"/api/admin/users/{uid}", json={"role_ids": []}, headers=auth(admin))
    assert r.status_code == 200
    assert roles  # для читаемости: роли не менялись


async def test_cannot_drop_admin_group_with_last_admin(client: httpx.AsyncClient) -> None:
    admin = await _admin(client)
    roles = await _roles(client, admin)
    me = (await client.get("/api/auth/me", headers=auth(admin))).json()
    uid = me["user"]["id"]
    g = await client.post(
        "/api/admin/groups",
        json={"name": "ИТ", "role_ids": [roles["Администратор"]["id"]], "member_ids": [uid]},
        headers=auth(admin),
    )
    assert g.status_code == 201
    r = await client.patch(f"/api/admin/users/{uid}", json={"role_ids": []}, headers=auth(admin))
    assert r.status_code == 200  # админ остаётся через группу
    r = await client.delete(f"/api/admin/groups/{g.json()['id']}", headers=auth(admin))
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Пользователи и группы
# ---------------------------------------------------------------------------


async def test_user_list_filters_and_pagination(client: httpx.AsyncClient) -> None:
    admin = await _admin(client)
    r = await client.get("/api/admin/users?account_type=test&page_size=2", headers=auth(admin))
    body = r.json()
    assert body["total"] == 4
    assert len(body["items"]) == 2
    r = await client.get("/api/admin/users?q=аналитик", headers=auth(admin))
    assert [u["personnel_number"] for u in r.json()["items"]] == ["T-0002"]
    assert r.json()["items"][0]["groups"][0]["name"] == "Тестовая группа аналитиков"
    r = await client.get("/api/admin/users?q=%25", headers=auth(admin))
    assert r.json()["total"] == 0  # % — это символ, а не «всё»


async def test_locked_user_shown_and_unlocked_by_admin(client: httpx.AsyncClient) -> None:
    admin = await _admin(client)
    for _ in range(5):
        await login(client, "T-0001", "wrong")
    r = await client.get("/api/admin/users?status=locked", headers=auth(admin))
    item = r.json()["items"][0]
    assert item["personnel_number"] == "T-0001"
    assert item["status"] == "locked"
    assert item["failed_attempts"] == 5
    r = await client.post(f"/api/admin/users/{item['id']}/unlock", headers=auth(admin))
    assert r.json()["status"] == "active"
    await login_ok(client, "T-0001")


async def test_blocking_user_ends_sessions_immediately(client: httpx.AsyncClient) -> None:
    admin = await _admin(client)
    user = await login_ok(client, "T-0001")
    me = (await client.get("/api/auth/me", headers=auth(user))).json()
    r = await client.patch(
        f"/api/admin/users/{me['user']['id']}", json={"is_active": False}, headers=auth(admin)
    )
    assert r.json()["status"] == "blocked"
    assert (await client.get("/api/auth/me", headers=auth(user))).status_code == 401
    r = await login(client, "T-0001", "test-password-1")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "blocked"


async def test_revoke_sessions(client: httpx.AsyncClient) -> None:
    admin = await _admin(client)
    user = await login_ok(client, "T-0001")
    uid = (await client.get("/api/auth/me", headers=auth(user))).json()["user"]["id"]
    r = await client.post(f"/api/admin/users/{uid}/revoke-sessions", headers=auth(admin))
    assert r.json()["active_sessions"] == 0
    assert (await client.get("/api/auth/me", headers=auth(user))).status_code == 401


async def test_create_local_user(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    admin = await _admin(client)
    r = await client.post(
        "/api/admin/users/local",
        json={"personnel_number": "kiosk", "full_name": "Киоск цеха", "password": "short"},
        headers=auth(admin),
    )
    assert r.status_code == 422
    r = await client.post(
        "/api/admin/users/local",
        json={"personnel_number": "kiosk", "full_name": "Киоск цеха", "password": "long-password"},
        headers=auth(admin),
    )
    assert r.status_code == 201
    assert r.json()["account_type"] == "local"
    await login_ok(client, "kiosk", "long-password")
    async with engine.connect() as conn:
        h = (
            await conn.execute(
                text("SELECT password_hash FROM users WHERE personnel_number='kiosk'")
            )
        ).scalar_one()
    assert h.startswith("$argon2id$")


async def test_external_user_password_cannot_be_set(client: httpx.AsyncClient) -> None:
    admin = await _admin(client)
    await login(client, "10234", FAKE_PASSWORD)
    users = (await client.get("/api/admin/users?q=10234", headers=auth(admin))).json()
    r = await client.patch(
        f"/api/admin/users/{users['items'][0]['id']}",
        json={"password": "new-long-password"},
        headers=auth(admin),
    )
    assert r.status_code == 422


async def test_group_crud(client: httpx.AsyncClient) -> None:
    admin = await _admin(client)
    roles = await _roles(client, admin)
    users = (await client.get("/api/admin/users?account_type=test", headers=auth(admin))).json()
    ids = [u["id"] for u in users["items"]][:2]
    r = await client.post(
        "/api/admin/groups",
        json={
            "name": "Отдел ОТ и ПБ",
            "role_ids": [roles["Аналитик"]["id"]],
            "member_ids": ids,
        },
        headers=auth(admin),
    )
    assert r.status_code == 201
    group = r.json()
    assert group["members_count"] == 2
    assert {m["id"] for m in group["members"]} == set(ids)
    listed = (await client.get("/api/admin/groups", headers=auth(admin))).json()
    assert "Отдел ОТ и ПБ" in [g["name"] for g in listed]
    dup = await client.post(
        "/api/admin/groups", json={"name": "Отдел ОТ и ПБ"}, headers=auth(admin)
    )
    assert dup.status_code == 409
    r = await client.patch(
        f"/api/admin/groups/{group['id']}", json={"member_ids": []}, headers=auth(admin)
    )
    assert r.json()["members_count"] == 0
    r = await client.delete(f"/api/admin/groups/{group['id']}", headers=auth(admin))
    assert r.status_code == 204


async def test_admin_actions_are_audited(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    admin = await _admin(client)
    await client.post("/api/admin/roles", json={"name": "Новая"}, headers=auth(admin))
    async with engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT actor_login, action FROM audit_log ORDER BY id"))
        ).all()
    assert ("T-0004", "role.create") in [tuple(r) for r in rows]
