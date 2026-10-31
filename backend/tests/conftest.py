"""Общие фикстуры.

Юнит-тесты работают без внешних сервисов. Тесты с пометкой `db` требуют PostgreSQL
с pgvector: адрес задаётся переменной TEST_DATABASE_URL, например
postgresql+asyncpg://rag:rag@localhost:5432/postgres. Для каждого прогона создаётся
отдельная временная база, на неё накатываются миграции, после прогона база удаляется.
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

os.environ.setdefault("APP_ENV", "test")
BACKEND_DIR = Path(__file__).resolve().parent.parent


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("TEST_DATABASE_URL"):
        return
    skip = pytest.mark.skip(reason="TEST_DATABASE_URL не задан")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    admin_url = make_url(os.environ["TEST_DATABASE_URL"])
    name = f"rag_test_{uuid.uuid4().hex[:8]}"
    test_url = admin_url.set(database=name).render_as_string(hide_password=False)

    import asyncio

    async def admin(sql: str) -> None:
        engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            await conn.execute(text(sql))
        await engine.dispose()

    asyncio.run(admin(f'CREATE DATABASE "{name}"'))
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    cfg.attributes["database_url"] = test_url
    command.upgrade(cfg, "head")

    os.environ["DATABASE_URL"] = test_url
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        yield test_url
    finally:
        asyncio.run(admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture(scope="session")
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(database_url)
    yield eng
    await eng.dispose()


@pytest.fixture
async def clean_jobs(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE jobs RESTART IDENTITY"))
