"""Общие фикстуры проверки ЕПоЗ."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.helpers import auth, login_ok, seed_test_users


@pytest.fixture
async def epoz_env(
    client: httpx.AsyncClient, clean_docs: None, engine: AsyncEngine
) -> dict[str, Any]:
    """Чистые таблицы ЕПоЗ, тестовые пользователи, группа «Закупки» с T-0001, токен админа."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE epoz_editions, epoz_parts, epoz_clauses, okpd2_editions, okpd2 "
                "RESTART IDENTITY CASCADE"
            )
        )
    await seed_test_users()
    async with engine.begin() as conn:
        gid = (
            await conn.execute(text("INSERT INTO groups (name) VALUES ('Закупки') RETURNING id"))
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO group_members (group_id, user_id) SELECT :g, id FROM users WHERE personnel_number='T-0001'"
            ),
            {"g": gid},
        )
    return {
        "group_id": gid,
        "admin": auth(await login_ok(client, "T-0004")),
        "member": auth(await login_ok(client, "T-0001")),
        "outsider": auth(await login_ok(client, "T-0003")),
    }
