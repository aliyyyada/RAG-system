"""ORM-модели: очередь фоновых задач (этап 0), пользователи, роли, группы и сессии (этап 1)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
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
