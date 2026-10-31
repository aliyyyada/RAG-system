import pytest
from pydantic import ValidationError

from app.config import Settings


def test_prod_refuses_test_users() -> None:
    with pytest.raises(ValidationError, match="ENABLE_TEST_USERS"):
        Settings(app_env="prod", enable_test_users=True)


PROD_SECRET = "x" * 40


def test_prod_ok_without_test_users() -> None:
    s = Settings(app_env="prod", enable_test_users=False, jwt_secret=PROD_SECRET)
    assert s.app_env == "prod"


def test_prod_requires_jwt_secret() -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(app_env="prod", enable_test_users=False, jwt_secret="short")


def test_prod_requires_https_for_password_check() -> None:
    with pytest.raises(ValidationError, match="https"):
        Settings(
            app_env="prod",
            enable_test_users=False,
            jwt_secret=PROD_SECRET,
            external_auth_url="http://auth.local/check",
        )


def test_dev_generates_jwt_secret_if_missing() -> None:
    s = Settings(app_env="dev", jwt_secret="")
    assert len(s.jwt_secret.get_secret_value()) >= 32
    assert s.jwt_secret.get_secret_value() not in repr(s)


def test_unimplemented_auth_provider_rejected() -> None:
    with pytest.raises(ValidationError, match="AUTH_PROVIDER"):
        Settings(auth_provider="ldap")


def test_heartbeat_must_fit_windows_idle_timeout() -> None:
    # Требование ТЗ: heartbeat не реже, чем раз в 2–3 секунды.
    with pytest.raises(ValidationError):
        Settings(sse_heartbeat_seconds=5)


def test_database_url_not_leaked_in_repr() -> None:
    s = Settings(database_url="postgresql+asyncpg://rag:topsecret@db/rag")
    assert "topsecret" not in repr(s)
