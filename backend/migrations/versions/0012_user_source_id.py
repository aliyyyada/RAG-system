"""ID пользователя в источнике документов (ID_UserBoss) — задаёт администратор.

Права на документы источник выдаёт по шестизначному ID пользователя (ID_UserBoss).
По умолчанию это табельный номер; для отладки администратор может подставить
другой ID (например, посмотреть систему глазами другого сотрудника). Пусто — табельный номер.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("source_user_id", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "source_user_id")
