"""Общие фикстуры.

Юнит-тесты работают без внешних сервисов. Тесты с пометкой `db` требуют PostgreSQL
с pgvector: адрес задаётся переменной TEST_DATABASE_URL, например
postgresql+asyncpg://rag:rag@localhost:5432/postgres. Для каждого прогона создаётся
отдельная временная база, на неё накатываются миграции, после прогона база удаляется.
"""

import os
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

os.environ.setdefault("APP_ENV", "test")
# Файлы документов — во временную папку; повтор упавшей задачи — сразу, без минуты ожидания.
os.environ.setdefault("DOCUMENTS_DIR", tempfile.mkdtemp(prefix="rag-test-docs-"))
os.environ.setdefault("DOCUMENT_RETRY_DELAY_SECONDS", "0")
# Ollama — фейковая в памяти (fakes/fake_ollama.py), реранкер по умолчанию выключен
# (тесты, которым он нужен, подставляют свой через set_reranker_override).
os.environ.setdefault("OLLAMA_URL", "http://fake-ollama/ollama")
os.environ.setdefault("RERANKER", "none")
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


@pytest.fixture(autouse=True)
def fake_ollama():  # type: ignore[no-untyped-def]
    """Фейковая Ollama для всех тестов: векторизация после разбора документа и ответы чата."""
    from httpx import ASGITransport

    from app.llm.ollama import set_transport_override
    from fakes.fake_ollama import FAKE_OLLAMA
    from fakes.fake_service import app as fake_app

    FAKE_OLLAMA.reset()
    set_transport_override(ASGITransport(app=fake_app))
    yield FAKE_OLLAMA
    set_transport_override(None)
    FAKE_OLLAMA.reset()


@pytest.fixture(scope="session")
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(database_url)
    yield eng
    await eng.dispose()


@pytest.fixture
async def clean_jobs(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE jobs RESTART IDENTITY"))


# ---------------------------------------------------------------------------
# Этап 1: приложение с фейковым внешним сервисом и чистые таблицы доступа
# ---------------------------------------------------------------------------

RESET_AUTH_SQL = """
TRUNCATE users, groups, auth_sessions, login_lockouts, login_events, audit_log,
         user_roles, group_roles, group_members, chats, chat_messages RESTART IDENTITY CASCADE;
DELETE FROM roles WHERE builtin_key IS NULL;
DELETE FROM role_permissions;
INSERT INTO role_permissions (role_id, permission)
SELECT id, 'search.common' FROM roles WHERE builtin_key IN ('guest', 'user')
UNION ALL SELECT id, 'documents.view' FROM roles WHERE builtin_key = 'user';
"""


@pytest.fixture
async def clean_auth(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for stmt in RESET_AUTH_SQL.split(";"):
            if stmt.strip():
                await conn.execute(text(stmt))


@pytest.fixture
def fake_service_transport():  # type: ignore[no-untyped-def]
    """Фейковый внешний сервис, вызываемый настоящим адаптером ExternalCheckProvider в памяти."""
    from httpx import ASGITransport

    from fakes.fake_service import app as fake_app

    return ASGITransport(app=fake_app)


@pytest.fixture
async def client(database_url: str, clean_auth: None, fake_service_transport):  # type: ignore[no-untyped-def]
    """HTTP-клиент к приложению. base_url https — иначе Secure-cookie клиент не сохранит."""
    from httpx import ASGITransport as _T
    from httpx import AsyncClient

    from app.auth.providers import get_auth_provider
    from app.auth.providers.external_check import ExternalCheckProvider
    from app.config import get_settings
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_auth_provider] = lambda: ExternalCheckProvider(
        get_settings(), transport=fake_service_transport
    )
    async with AsyncClient(transport=_T(app=app), base_url="https://testserver") as c:
        c.app = app  # type: ignore[attr-defined]
        yield c


# ---------------------------------------------------------------------------
# Этап 2: источник документов (фейковый, в памяти) и чистый реестр документов
# ---------------------------------------------------------------------------

RESET_DOCS_SQL = """
TRUNCATE sources, collections, documents, document_files, chunks, document_events,
         staging_documents, sync_runs, jobs, llm_queue RESTART IDENTITY CASCADE;
INSERT INTO collections (name, kind, description)
VALUES ('Ручные загрузки', 'manual', 'Документы, загруженные через интерфейс');
"""


@pytest.fixture
async def clean_docs(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for stmt in RESET_DOCS_SQL.split(";"):
            if stmt.strip():
                await conn.execute(text(stmt))


@pytest.fixture
def fake_source():  # type: ignore[no-untyped-def]
    """Фейковый источник: адаптер http_json ходит в него в памяти, без сети."""
    from httpx import ASGITransport

    from app.sources import set_transport_override
    from fakes.fake_service import app as fake_app
    from fakes.fake_source import ARCHIVE, SOURCE

    SOURCE.reset()
    ARCHIVE.reset()
    set_transport_override(ASGITransport(app=fake_app))
    yield SOURCE
    set_transport_override(None)
    SOURCE.reset()
    ARCHIVE.reset()


@pytest.fixture
async def docflow(database_url: str, clean_docs: None, fake_source):  # type: ignore[no-untyped-def]
    """Подключённый источник «Документооборот» (фейковый). Возвращает id источника."""
    from app.db import get_sessionmaker
    from app.ingest.service import create_source

    async with get_sessionmaker()() as s, s.begin():
        src = await create_source(
            s, code="docflow", name="Документооборот", base_url="https://fake/source"
        )
        return src.id
