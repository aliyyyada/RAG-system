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

    # ---------- Документы и синхронизация (этап 2) ----------
    # Папка с файлами документов (том Docker). Имя файла = SHA-256 содержимого.
    documents_dir: str = "/data/documents"
    # Часовой пояс завода: в нём считается час ночной сверки и показываются даты.
    app_timezone: str = "Europe/Moscow"
    # TLS к источникам документов. Если не задано — тот же CA, что у сервиса проверки пароля.
    source_ca_file: str | None = None
    source_client_cert: str | None = None
    source_client_key: str | None = None
    source_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    # Файл больше этого размера не скачиваем (защита диска от ошибки источника).
    source_file_max_mb: int = Field(default=200, ge=1)
    # Ручная загрузка через интерфейс.
    manual_upload_max_mb: int = Field(default=50, ge=1)
    # OCR сканов (Tesseract). Страница считается сканом, если в текстовом слое меньше
    # ocr_min_chars символов.
    ocr_languages: str = "rus+eng"
    ocr_dpi: int = Field(default=300, ge=100, le=600)
    ocr_min_chars: int = Field(default=30, ge=0)
    ocr_timeout_seconds: int = Field(default=180, ge=10)
    # Размер фрагмента: целимся в chunk_target_chars, не больше chunk_max_chars
    # (~ 400 и ~ 650 токенов bge-m3). Таблицы режем только по строкам.
    chunk_target_chars: int = Field(default=1500, ge=200)
    chunk_max_chars: int = Field(default=2500, ge=400)
    # Сколько раз пробуем обработать документ, прежде чем показать «ошибка».
    document_max_attempts: int = Field(default=3, ge=1, le=20)
    document_retry_delay_seconds: int = Field(default=60, ge=0)
    # Планировщик в worker: инкрементальная синхронизация и ночная сверка по расписанию.
    scheduler_enabled: bool = True

    # ---------- Доступ к документам (этап 3) ----------
    # Сколько минут помним ответ источника «какие документы доступны человеку».
    # Кэш сбрасывается при входе человека и кнопкой в админке («Источники»).
    access_cache_minutes: int = Field(default=5, ge=0, le=24 * 60)
    # Сколько ждём ответа источника о правах. Источники опрашиваются параллельно, поэтому
    # это и верхняя граница задержки для пользователя. Не ответил — документы источника скрыты.
    access_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    # Источник не ответил — следующую попытку делаем не раньше чем через столько секунд,
    # чтобы упавший источник не добавлял таймаут к каждому запросу каждого пользователя.
    access_retry_seconds: int = Field(default=30, ge=0, le=3600)

    # ---------- Поиск и чат (этап 4) ----------
    # Сколько фрагментов берём из каждого вида поиска (вектор и полнотекстовый) до слияния RRF.
    search_candidates: int = Field(default=30, ge=5, le=200)
    # Константа RRF: score = Σ 1 / (rrf_k + место в списке). 60 — значение из статьи о RRF.
    search_rrf_k: int = Field(default=60, ge=1)
    # Сколько лучших после слияния отдаём реранкеру и сколько после него — модели.
    rerank_top_n: int = Field(default=20, ge=1, le=100)
    rag_top_k: int = Field(default=6, ge=1, le=20)
    # Сколько символов текста фрагментов помещаем в промпт (~ 4 символа на токен у bge/qwen).
    rag_context_chars: int = Field(default=12000, ge=1000)
    # Реранкер bge-reranker-v2-m3: onnx — локальная модель в папке reranker_dir (CPU);
    # none — без реранкера (порядок RRF). Папки нет — работаем без реранкера с предупреждением.
    reranker: Literal["onnx", "none"] = "onnx"
    reranker_dir: str = "/models/reranker"
    reranker_threads: int = Field(default=4, ge=1, le=64)
    reranker_max_tokens: int = Field(default=512, ge=64, le=8192)
    # Фрагменты с оценкой реранкера ниже порога (0…1) в ответ не идут. Если ниже порога все —
    # отвечаем «в документах ответа нет», не обращаясь к модели.
    rerank_min_score: float = Field(default=0.05, ge=0, le=1)
    # Сколько последних реплик диалога передаём модели (вопрос + ответ = 2 реплики).
    chat_history_turns: int = Field(default=6, ge=0, le=40)
    # Сколько дней хранится история чатов (с последнего вопроса).
    chat_history_days: int = Field(default=90, ge=1, le=3650)
    # Сколько токенов максимум генерирует модель в ответе.
    llm_max_tokens: int = Field(default=1024, ge=64, le=8192)
    llm_temperature: float = Field(default=0.1, ge=0, le=2)
    # Сколько ждём первого байта от Ollama (загрузка модели в видеопамять — до минуты).
    llm_timeout_seconds: float = Field(default=180.0, gt=0)
    # Запись очереди к LLM без heartbeat дольше этого срока считается брошенной.
    llm_queue_stale_seconds: int = Field(default=30, ge=5)
    # Векторизация в worker: по сколько фрагментов за один запрос к Ollama.
    embed_batch_size: int = Field(default=16, ge=1, le=256)
    # Гостевой режим: не больше N вопросов в минуту с одного IP.
    guest_questions_per_minute: int = Field(default=6, ge=1, le=600)
    # Вопрос пользователя — не длиннее (символов).
    question_max_chars: int = Field(default=2000, ge=100, le=20000)

    # ---------- Граф знаний (этап 5) ----------
    # Режим поиска по умолчанию: chunks — только фрагменты (этап 4), graph — сущности и связи
    # документов → фрагменты, mixed — оба списка, слитые RRF. Пользователь может выбрать сам.
    search_mode: Literal["chunks", "graph", "mixed"] = "mixed"
    # Сколько сущностей, найденных по словам вопроса, и фрагментов графа берём в поиск.
    graph_entities_limit: int = Field(default=5, ge=1, le=50)
    graph_candidates: int = Field(default=20, ge=1, le=200)
    # Извлечение моделью: фрагменты короче этого не отправляем; длиннее — обрезаем.
    graph_min_chunk_chars: int = Field(default=60, ge=0)
    graph_chunk_max_chars: int = Field(default=3000, ge=200)
    # Ответ модели на один фрагмент — не длиннее (токенов): вызов не держит видеокарту долго.
    graph_max_tokens: int = Field(default=512, ge=64, le=4096)
    # Фрагментов за один заход задачи graph_extract (дальше задача встаёт в очередь снова).
    graph_chunks_per_job: int = Field(default=10, ge=1, le=500)

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
