import pytest
from pydantic import ValidationError

from app.config import Settings


def test_prod_refuses_test_users() -> None:
    with pytest.raises(ValidationError, match="ENABLE_TEST_USERS"):
        Settings(app_env="prod", enable_test_users=True)


def test_prod_ok_without_test_users() -> None:
    assert Settings(app_env="prod", enable_test_users=False).app_env == "prod"


def test_heartbeat_must_fit_windows_idle_timeout() -> None:
    # Требование ТЗ: heartbeat не реже, чем раз в 2–3 секунды.
    with pytest.raises(ValidationError):
        Settings(sse_heartbeat_seconds=5)


def test_database_url_not_leaked_in_repr() -> None:
    s = Settings(database_url="postgresql+asyncpg://rag:topsecret@db/rag")
    assert "topsecret" not in repr(s)
