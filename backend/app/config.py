"""Настройки приложения.

Все значения берутся из переменных окружения (файл .env в корне репозитория,
который в git не попадает). Пример со всеми ключами: .env.example.
"""

import secrets
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # dev — разработка (самоподписанный сертификат, тестовые учётки, фейковый источник);
    # prod — завод. В prod опасные для безопасности режимы запрещены (см. validate_prod).
    app_env: Literal["dev", "test", "prod"] = "dev"

    # postgresql+asyncpg://user:password@host:5432/db
    database_url: SecretStr = SecretStr("postgresql+asyncpg://rag:rag@postgres:5432/rag")

    ollama_url: str = "http://ollama:11434"
    llm_model: str = "qwen2.5:7b-instruct-q4_K_M"
    llm_fallback_model: str = "qwen3:4b"
    embedding_model: str = "bge-m3"
    llm_num_ctx: int = 8192

    # Windows/WSL2 рвёт простаивающие HTTP-соединения через несколько секунд,
    # поэтому во всех потоковых ответах отправляем пустое SSE-событие не реже этого интервала.
    sse_heartbeat_seconds: float = Field(default=2.0, gt=0, le=3.0)

    # Тестовые учётки (T-0001…) и фейковый внешний сервис. В prod обязательно false.
    enable_test_users: bool = True

    # ---------- Вход и сессии (этап 1) ----------
    # Ключ подписи access-токенов (JWT, HS256). В prod обязателен, 32+ символа.
    # В dev/test, если не задан, генерируется при старте: после перезапуска app
    # все пользователи просто получат новый access-токен по refresh-cookie.
    jwt_secret: SecretStr = SecretStr("")
    access_token_minutes: int = Field(default=15, ge=1, le=60)
    # Срок жизни сессии (refresh-токена). 12 часов = одна смена: на общих компьютерах
    # цеха сессия не живёт неделями. Допущение, см. docs/open-questions.md.
    refresh_token_hours: int = Field(default=12, ge=1, le=24 * 30)
    # Блокировка входа после N неудачных попыток подряд на этот срок.
    login_max_attempts: int = Field(default=5, ge=1, le=100)
    login_lockout_minutes: int = Field(default=15, ge=1)

    # Как проверяем пароль внешних пользователей (интерфейс AuthProvider, app/auth/providers).
    # external_check — внешняя функция проверки по HTTPS (решено для текущего этапа);
    # oidc / ldap / hash — варианты на будущее, интерфейс под них готов, адаптеры не написаны.
    auth_provider: Literal["external_check", "oidc", "ldap", "hash"] = "external_check"
    # Адрес функции проверки: POST {"login", "password"} → {"ok": true/false, ...}.
    # В разработке — фейковый сервис (контейнер fake-service).
    external_auth_url: str = "https://fake-service:8443/auth/check"
    # Сертификат CA, которым подписан сертификат внешнего сервиса (если не системный).
    external_auth_ca_file: str | None = None
    # Клиентский сертификат и ключ для mTLS, если внешний сервис его требует.
    external_auth_client_cert: str | None = None
    external_auth_client_key: str | None = None
    external_auth_timeout_seconds: float = Field(default=5.0, gt=0, le=30)

    # Гостевой режим: без входа доступны поиск и общие документы.
    guest_mode_enabled: bool = True

    worker_poll_seconds: float = 2.0
    # Задача, которая «висит» в статусе running дольше этого срока (например, worker
    # упал во время обработки), возвращается в очередь. Так индексация переживает перезапуск.
    worker_stale_after_seconds: int = 15 * 60

    @model_validator(mode="after")
    def validate_prod(self) -> "Settings":
        if self.app_env == "prod" and self.enable_test_users:
            raise ValueError(
                "В prod тестовые учётки должны быть выключены: ENABLE_TEST_USERS=false"
            )
        if self.app_env == "prod" and len(self.jwt_secret.get_secret_value()) < 32:
            raise ValueError("В prod нужен JWT_SECRET длиной 32+ символа (см. .env.example)")
        if self.app_env == "prod" and not self.external_auth_url.startswith("https://"):
            raise ValueError("EXTERNAL_AUTH_URL должен начинаться с https:// — пароль идёт по сети")
        if self.auth_provider != "external_check":
            raise ValueError(
                f"AUTH_PROVIDER={self.auth_provider} пока не реализован; используйте external_check"
            )
        if not self.jwt_secret.get_secret_value():
            # dev/test: случайный ключ на время жизни процесса.
            self.jwt_secret = SecretStr(secrets.token_urlsafe(48))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
