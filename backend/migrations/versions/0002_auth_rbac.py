"""Этап 1: пользователи, роли, группы, права, сессии, журнал входов, аудит.

Встроенные роли «Гость», «Пользователь», «Администратор» создаются здесь же,
чтобы система работала сразу после установки.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("personnel_number", sa.String(64), nullable=False, unique=True),
        sa.Column("full_name", sa.String(255), server_default=sa.text("''"), nullable=False),
        sa.Column("department", sa.String(255)),
        sa.Column(
            "account_type", sa.String(16), server_default=sa.text("'external'"), nullable=False
        ),
        sa.Column("password_hash", sa.Text()),
        sa.Column("is_emergency", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_login_at", TZ),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "account_type IN ('external', 'local', 'test')", name="ck_users_account_type"
        ),
        # Пароль хранится только у локальных и тестовых учёток.
        sa.CheckConstraint(
            "account_type <> 'external' OR password_hash IS NULL",
            name="ck_users_external_no_password",
        ),
    )

    op.create_table(
        "roles",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("builtin_key", sa.String(16), unique=True),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "builtin_key IS NULL OR builtin_key IN ('guest', 'user', 'admin')",
            name="ck_roles_builtin_key",
        ),
    )
    op.create_table(
        "role_permissions",
        sa.Column(
            "role_id",
            sa.BigInteger(),
            sa.ForeignKey("roles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("permission", sa.String(100), primary_key=True),
    )

    op.create_table(
        "groups",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "user_roles",
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "role_id",
            sa.BigInteger(),
            sa.ForeignKey("roles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_table(
        "group_roles",
        sa.Column(
            "group_id",
            sa.BigInteger(),
            sa.ForeignKey("groups.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "role_id",
            sa.BigInteger(),
            sa.ForeignKey("roles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_table(
        "group_members",
        sa.Column(
            "group_id",
            sa.BigInteger(),
            sa.ForeignKey("groups.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_index("ix_group_members_user", "group_members", ["user_id"])

    op.create_table(
        "auth_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("prev_token_hash", sa.String(64)),
        sa.Column("rotated_at", TZ),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("last_used_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", TZ, nullable=False),
        sa.Column("revoked_at", TZ),
        sa.Column("ip", sa.String(64)),
        sa.Column("user_agent", sa.String(512)),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_prev_token_hash", "auth_sessions", ["prev_token_hash"])

    op.create_table(
        "login_lockouts",
        sa.Column("login", sa.String(64), primary_key=True),
        sa.Column("failed_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_failed_at", TZ),
        sa.Column("locked_until", TZ),
    )

    op.create_table(
        "login_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("login", sa.String(64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("account_type", sa.String(16)),
        sa.Column("ip", sa.String(64)),
        sa.Column("user_agent", sa.String(512)),
    )
    op.create_index("ix_login_events_created_at", "login_events", ["created_at"])
    op.create_index("ix_login_events_login", "login_events", ["login"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("actor_login", sa.String(64)),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(32)),
        sa.Column("target_id", sa.String(64)),
        sa.Column(
            "details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("ip", sa.String(64)),
    )
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])

    # Встроенные роли. Права администратора не хранятся: у него всегда все права (app/auth/rbac.py).
    op.execute(
        """
        INSERT INTO roles (name, description, builtin_key) VALUES
          ('Гость', 'Без входа: поиск и общие документы. Больше общих документов гостю выдать нельзя.', 'guest'),
          ('Пользователь', 'Есть у каждого вошедшего автоматически.', 'user'),
          ('Администратор', 'Все права. Нельзя удалить или ограничить.', 'admin')
        """
    )
    op.execute(
        """
        INSERT INTO role_permissions (role_id, permission)
        SELECT id, p FROM roles, unnest(ARRAY['search.common']) AS p WHERE builtin_key = 'guest'
        UNION ALL
        SELECT id, p FROM roles, unnest(ARRAY['search.common', 'documents.view']) AS p
        WHERE builtin_key = 'user'
        """
    )


def downgrade() -> None:
    for table in (
        "audit_log",
        "login_events",
        "login_lockouts",
        "auth_sessions",
        "group_members",
        "group_roles",
        "user_roles",
        "groups",
        "role_permissions",
        "roles",
        "users",
    ):
        op.drop_table(table)
