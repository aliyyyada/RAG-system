"""ORM-модели: очередь (этап 0), пользователи и RBAC (1), документы (2), доступ (3)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Job(Base):
    """Фоновая задача worker'а (синхронизация, парсинг, векторизация, граф, противоречия).

    Очередь живёт прямо в PostgreSQL: отдельный Redis/RabbitMQ не нужен.
    Worker забирает задачу запросом SELECT ... FOR UPDATE SKIP LOCKED,
    поэтому несколько worker'ов не возьмут одну и ту же задачу.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # Больше — раньше. Новые документы получат приоритет выше, чем первичная индексация.
    priority: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # queued → running → done | failed (failed после исчерпания попыток)
    status: Mapped[str] = mapped_column(String(16), server_default=text("'queued'"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, server_default=text("3"))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(128))
    # Ключ от повторов: пока задача с таким ключом стоит в очереди, вторая не ставится.
    # Пример: «document:42» — синхронизация дважды увидела изменённый документ 42.
    dedupe_key: Mapped[str | None] = mapped_column(String(128))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("status IN ('queued', 'running', 'done', 'failed')", name="ck_jobs_status"),
        Index(
            "ix_jobs_dequeue",
            text("priority DESC"),
            text("id"),
            postgresql_where=text("status = 'queued'"),
        ),
        Index(
            "ux_jobs_dedupe_queued",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status = 'queued' AND dedupe_key IS NOT NULL"),
        ),
    )


# ---------------------------------------------------------------------------
# Этап 1: пользователи, RBAC, сессии
# ---------------------------------------------------------------------------

user_roles = Table(
    "user_roles",
    Base.metadata,
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
)

group_roles = Table(
    "group_roles",
    Base.metadata,
    Column("group_id", ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
)

group_members = Table(
    "group_members",
    Base.metadata,
    Column("group_id", ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
)


class User(Base):
    """Пользователь. Ключ во всех системах завода — табельный номер.

    Сами себя пользователи не регистрируют: внешний (`external`) появляется здесь при
    первом успешном входе или когда администратор заранее назначает ему роль.
    `local` — учётка с паролем у нас (argon2), например аварийный администратор;
    `test` — тестовые учётки разработки, в prod не входят (ENABLE_TEST_USERS=false).
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    personnel_number: Mapped[str] = mapped_column(String(64), unique=True)
    full_name: Mapped[str] = mapped_column(String(255), server_default=text("''"))
    department: Mapped[str | None] = mapped_column(String(255))
    account_type: Mapped[str] = mapped_column(String(16), server_default=text("'external'"))
    # Только для local/test. Для внешних пользователей пароль у нас не хранится никогда.
    password_hash: Mapped[str | None] = mapped_column(Text)
    # Аварийная локальная учётка администратора: работает в prod, каждый вход — в аудит.
    is_emergency: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # false — заблокирован администратором (в отличие от временной блокировки за пароль).
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    roles: Mapped[list["Role"]] = relationship(secondary=user_roles, lazy="selectin")
    # Без back_populates: группы и участники меняются в разных запросах, а двусторонняя
    # синхронизация коллекций в async-сессии тянет ленивые загрузки.
    groups: Mapped[list["Group"]] = relationship(secondary=group_members, lazy="selectin")

    __table_args__ = (
        CheckConstraint(
            "account_type IN ('external', 'local', 'test')", name="ck_users_account_type"
        ),
        CheckConstraint(
            "account_type <> 'external' OR password_hash IS NULL",
            name="ck_users_external_no_password",
        ),
    )


class Role(Base):
    """Роль = набор прав (галочек). Встроенные роли помечены builtin_key и не удаляются."""

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))
    # guest | user | admin — встроенные роли; NULL — созданные администратором.
    builtin_key: Mapped[str | None] = mapped_column(String(16), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    permission_rows: Mapped[list["RolePermission"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def permissions(self) -> set[str]:
        return {p.permission for p in self.permission_rows}


class RolePermission(Base):
    """Право роли. Код права — строка из каталога app/auth/permissions.py.

    Права живут в коде, а не в таблице: их проверяет код, и новый навык добавляет своё
    право вместе со своим кодом. Неизвестные коды (например, удалённого навыка) игнорируются.
    """

    __tablename__ = "role_permissions"

    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission: Mapped[str] = mapped_column(String(100), primary_key=True)


class Group(Base):
    """Группа людей (например, «Отдел ОТ и ПБ»). Группе назначают роли."""

    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    roles: Mapped[list[Role]] = relationship(secondary=group_roles, lazy="selectin")
    members: Mapped[list[User]] = relationship(
        secondary=group_members, lazy="selectin", overlaps="groups"
    )


class AuthSession(Base):
    """Сессия входа = один refresh-токен (одно устройство/браузер).

    Храним только SHA-256 от токена: утечка таблицы не даёт войти. При каждом обновлении
    токен меняется (ротация); предыдущий хэш помним минуту, чтобы две вкладки, обновившие
    токен одновременно, не выбили друг друга.
    """

    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prev_token_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))


class LoginLockout(Base):
    """Счётчик неудачных попыток по введённому логину (пользователя у нас может ещё не быть)."""

    __tablename__ = "login_lockouts"

    login: Mapped[str] = mapped_column(String(64), primary_key=True)
    failed_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginEvent(Base):
    """Журнал входов: кто, когда, откуда, успешно или почему нет. Пароль не пишется никогда."""

    __tablename__ = "login_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    login: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # success | invalid_credentials | locked | blocked | disabled | provider_unavailable | logout
    result: Mapped[str] = mapped_column(String(32))
    account_type: Mapped[str | None] = mapped_column(String(16))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))


class AuditLog(Base):
    """Журнал действий администраторов и важных событий безопасности.

    Экран журнала появится на этапе 7; писать в него начинаем сразу.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    actor_login: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    ip: Mapped[str | None] = mapped_column(String(64))


# ---------------------------------------------------------------------------
# Этап 2: источники, реестр документов, файлы, фрагменты, синхронизация
# ---------------------------------------------------------------------------


class Source(Base):
    """Подключённая внешняя система с документами (например, «Документооборот»).

    Как с ней говорить, решает адаптер (`adapter`, см. app/sources). Настройки меняются
    в админке, секреты (если появятся) — только в .env.
    """

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Короткий код латиницей: docflow. Используется в API для самого источника.
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    adapter: Mapped[str] = mapped_column(String(32), server_default=text("'http_json'"))
    base_url: Mapped[str] = mapped_column(String(500))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    sync_interval_minutes: Mapped[int] = mapped_column(Integer, server_default=text("15"))
    # Перекрытие окна: берём изменения с (последняя дата изменения − N минут), чтобы не
    # потерять документ, который источник записал «задним числом» во время прошлой синхронизации.
    overlap_minutes: Mapped[int] = mapped_column(Integer, server_default=text("60"))
    # Час ночной сверки по местному времени (APP_TIMEZONE).
    reconcile_hour: Mapped[int] = mapped_column(Integer, server_default=text("2"))
    # SHA-256 токена, с которым источник сам обращается к нашему API (сам токен не храним).
    callback_token_hash: Mapped[str | None] = mapped_column(String(64))

    sync_cursor: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reconcile_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Сколько живых документов источник отдал при последней полной сверке.
    source_total: Mapped[int | None] = mapped_column(Integer)
    # Ответил ли источник при последнем обращении. Не ответил — показываем предупреждение.
    available: Mapped[bool | None] = mapped_column(Boolean)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("code ~ '^[a-z][a-z0-9_-]{1,31}$'", name="ck_sources_code"),
        CheckConstraint("sync_interval_minutes >= 1", name="ck_sources_interval"),
        CheckConstraint("reconcile_hour BETWEEN 0 AND 23", name="ck_sources_reconcile_hour"),
    )


class Collection(Base):
    """Коллекция документов: «весь источник X», «Ручные загрузки» или тематическая."""

    __tablename__ = "collections"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    # source — все документы одного источника; manual — ручные загрузки; topic — тематическая.
    kind: Mapped[str] = mapped_column(String(16))
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), unique=True
    )
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("kind IN ('source', 'manual', 'topic')", name="ck_collections_kind"),
    )


DOC_STATUSES = ("queued", "processing", "ready", "error")


class Document(Base):
    """Документ в нашем реестре.

    Пример: приказ № 112 из «Документооборота» — source_id=1, external_id='7c1d…e5a4',
    version = дата изменения в источнике. Если источник поменял документ, version растёт,
    документ встаёт в очередь (status=queued) и после обработки indexed_version = version.
    Пока идёт переобработка, в поиске остаются старые фрагменты: документ не пропадает.
    """

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    collection_id: Mapped[int] = mapped_column(ForeignKey("collections.id"))
    # guid документа в источнике; у ручных загрузок NULL.
    external_id: Mapped[str | None] = mapped_column(String(128))
    version: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    indexed_version: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    number: Mapped[str | None] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(Text, server_default=text("''"))
    doc_type: Mapped[str | None] = mapped_column(String(100))
    topic: Mapped[str | None] = mapped_column(String(200))
    doc_date: Mapped[Any | None] = mapped_column(Date)
    # Шапка как пришла из источника (все поля, в том числе неизвестные нам).
    header: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # Табличные части: [{"name": "Ознакомлены", "rows": 46}]
    parts_summary: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))

    # Доступен всем, в том числе без входа (п. 6 ТЗ). Для источника — из источника.
    is_common: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # Администратор исключил из поиска (кнопка в карточке документа).
    is_excluded: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # Мягкое удаление: документ пропадает из поиска, но запись и история остаются.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # deleted_in_source | missing_in_source (ночная сверка не нашла его у источника)
    deleted_reason: Mapped[str | None] = mapped_column(String(32))

    status: Mapped[str] = mapped_column(String(16), server_default=text("'queued'"))
    # fetch | completeness | parse | ocr | store
    error_stage: Mapped[str | None] = mapped_column(String(16))
    error_message: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    status_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    chunk_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'processing', 'ready', 'error')", name="ck_documents_status"
        ),
        CheckConstraint(
            "(source_id IS NULL) = (external_id IS NULL)", name="ck_documents_source_ref"
        ),
        Index(
            "ux_documents_source_external",
            "source_id",
            "external_id",
            unique=True,
            postgresql_where=text("source_id IS NOT NULL"),
        ),
        Index("ix_documents_status", "source_id", "status"),
        Index("ix_documents_collection", "collection_id"),
    )


class DocumentFile(Base):
    """Файл документа. Сам файл лежит на диске: /data/documents/ab/abcdef… (SHA-256)."""

    __tablename__ = "document_files"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    ord: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    source_file_id: Mapped[str | None] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(500))
    mime: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    pages: Mapped[int | None] = mapped_column(Integer)
    # Сколько страниц пришлось распознавать (скан без текстового слоя).
    ocr_pages: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # Предупреждения разбора: «стр. 4: распознано мало текста» и т. п.
    warnings: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    # Распознанный текст целиком (вкладка «Распознанный текст» в карточке документа).
    text: Mapped[str] = mapped_column(Text, server_default="")


class Chunk(Base):
    """Фрагмент документа — единица поиска.

    Хранит, откуда он: файл, страницы, раздел («4. Порядок допуска») и пункт («4.2»),
    чтобы ответ мог сослаться на документ и открыть его на нужном месте.
    Таблица в фрагменте всегда целыми строками и с шапкой таблицы.
    """

    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    file_id: Mapped[int | None] = mapped_column(ForeignKey("document_files.id", ondelete="CASCADE"))
    ord: Mapped[int] = mapped_column(Integer)
    # header — собран из шапки и табличных частей источника; text; table
    kind: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    section: Mapped[str | None] = mapped_column(Text)
    point: Mapped[str | None] = mapped_column(String(32))
    page_from: Mapped[int | None] = mapped_column(Integer)
    page_to: Mapped[int | None] = mapped_column(Integer)
    token_estimate: Mapped[int] = mapped_column(Integer)
    # Полнотекстовый поиск с русской морфологией («приказы» найдёт «приказ»). Этап 4.
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('russian', text)", persisted=True)
    )

    __table_args__ = (
        CheckConstraint("kind IN ('header', 'text', 'table')", name="ck_chunks_kind"),
        Index("ux_chunks_document_ord", "document_id", "ord", unique=True),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
    )


class DocumentEvent(Base):
    """История документа: получен, обработан, ошибка, удалён в источнике, повтор…"""

    __tablename__ = "document_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    event: Mapped[str] = mapped_column(String(32))
    version: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class StagingDocument(Base):
    """Промежуточная таблица: документ ровно в том виде, как его отдал источник.

    Сначала всё пишется сюда и проверяется на полноту (число строк табличных частей,
    размер файлов, файл открывается). Только после проверки данные попадают в реестр.
    Если проверка не прошла, строка остаётся здесь с причиной — видно, что именно пришло.
    """

    __tablename__ = "staging_documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(String(128))
    version: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    header: Mapped[dict[str, Any]] = mapped_column(JSONB)
    parts: Mapped[list[Any]] = mapped_column(JSONB)
    files: Mapped[list[Any]] = mapped_column(JSONB)
    # received | failed
    status: Mapped[str] = mapped_column(String(16), server_default=text("'received'"))
    check_errors: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))

    __table_args__ = (Index("ux_staging_source_external", "source_id", "external_id", unique=True),)


class SyncRun(Base):
    """Один запуск синхронизации (инкрементальной или ночной сверки) — для отчёта."""

    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    # incremental | reconcile
    kind: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # running | ok | error
    status: Mapped[str] = mapped_column(String(16), server_default=text("'running'"))
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    error: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------------------
# Этап 3: доступ к документам
# ---------------------------------------------------------------------------


class AccessCache(Base):
    """Кэш ответа источника «какие документы доступны человеку».

    Одна строка на пару (человек, источник). ok=false — источник не ответил: его документы
    человеку не показываем (fail-closed), а повторно спрашиваем через полминуты, чтобы
    упавший источник не добавлял таймаут к каждому запросу. Сами id — в AccessCacheId.
    Пример: Иванова (10234) × «Документооборот» → ok, 1 284 id, действует до 12:35.
    """

    __tablename__ = "access_cache"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ok: Mapped[bool] = mapped_column(Boolean)
    id_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text)


class AccessCacheId(Base):
    """Один разрешённый источником документ (guid источника) для человека.

    Отдельные строки, а не массив: поиск фильтрует по ним через EXISTS по первичному ключу,
    это быстро и на тысячах id. Удаляются каскадом вместе со строкой AccessCache.
    """

    __tablename__ = "access_cache_ids"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(128), primary_key=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "source_id"],
            ["access_cache.user_id", "access_cache.source_id"],
            ondelete="CASCADE",
        ),
    )


class DocumentGrant(Base):
    """Кому администратор открыл документ ручной загрузки: человеку или группе."""

    __tablename__ = "document_grants"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "(user_id IS NULL) <> (group_id IS NULL)", name="ck_document_grants_target"
        ),
        Index(
            "ux_document_grants_user",
            "document_id",
            "user_id",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
        ),
        Index(
            "ux_document_grants_group",
            "document_id",
            "group_id",
            unique=True,
            postgresql_where=text("group_id IS NOT NULL"),
        ),
    )
