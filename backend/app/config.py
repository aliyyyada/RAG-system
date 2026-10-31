"""Настройки приложения.

Все значения берутся из переменных окружения (файл .env в корне репозитория,
который в git не попадает). Пример со всеми ключами: .env.example.
"""

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

    # Пароли пользователей и тестовые учётки появятся на этапе 1; флаг заводим сразу,
    # чтобы проверка «в prod тестовые учётки выключены» была с первого дня.
    enable_test_users: bool = True

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
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
