"""Ролевая система №1: какие функции системы доступны человеку.

Итоговые права = роль «Пользователь» (есть у каждого вошедшего)
               ∪ роли, назначенные человеку напрямую
               ∪ роли всех его групп.
Если среди них есть «Администратор» — все права из каталога.

Гость (без входа) получает права встроенной роли «Гость», но только из разрешённого
гостю списка (сейчас это search.common): даже если кто-то вручную допишет строку в БД,
гость не получит ничего сверх общих документов.
"""

from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import permissions as perms
from app.models import Group, Role, RolePermission, User, group_members, group_roles, user_roles

BUILTIN_GUEST = "guest"
BUILTIN_USER = "user"
BUILTIN_ADMIN = "admin"


@dataclass(frozen=True)
class Principal:
    """Кто делает запрос: пользователь после входа или гость."""

    user: User | None
    permissions: frozenset[str]
    is_admin: bool = False
    session_id: object | None = None
    roles: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_guest(self) -> bool:
        return self.user is None

    def has(self, permission: str) -> bool:
        return permission in self.permissions


async def guest_permissions(session: AsyncSession) -> frozenset[str]:
    rows = await session.scalars(
        select(RolePermission.permission)
        .join(Role, Role.id == RolePermission.role_id)
        .where(Role.builtin_key == BUILTIN_GUEST)
    )
    return frozenset(rows) & perms.guest_allowed_codes()


async def roles_of_user(session: AsyncSession, user_id: int) -> list[Role]:
    """Все роли человека: «Пользователь» + свои + групповые (без повторов)."""
    via_groups = (
        select(group_roles.c.role_id)
        .join(group_members, group_members.c.group_id == group_roles.c.group_id)
        .where(group_members.c.user_id == user_id)
    )
    direct = select(user_roles.c.role_id).where(user_roles.c.user_id == user_id)
    result = await session.scalars(
        select(Role)
        .where(
            or_(
                Role.builtin_key == BUILTIN_USER,
                Role.id.in_(direct),
                Role.id.in_(via_groups),
            )
        )
        .order_by(Role.id)
    )
    return list(result)


def permissions_for_roles(roles: list[Role]) -> tuple[frozenset[str], bool]:
    if any(r.builtin_key == BUILTIN_ADMIN for r in roles):
        return perms.all_codes(), True
    granted: set[str] = set()
    for role in roles:
        if role.builtin_key == BUILTIN_GUEST:
            continue  # роль гостя людям не назначается
        granted |= role.permissions
    return frozenset(granted) & perms.all_codes(), False


async def principal_for_user(
    session: AsyncSession, user: User, session_id: object | None = None
) -> Principal:
    roles = await roles_of_user(session, user.id)
    granted, is_admin = permissions_for_roles(roles)
    return Principal(
        user=user,
        permissions=granted,
        is_admin=is_admin,
        session_id=session_id,
        roles=tuple(r.name for r in roles),
    )


async def count_active_admins(session: AsyncSession) -> int:
    """Сколько активных людей имеют роль «Администратор» (напрямую или через группу).

    Используется защитой «нельзя убрать последнего администратора»: иначе в админку
    больше никто не попадёт.
    """
    admin_role = select(Role.id).where(Role.builtin_key == BUILTIN_ADMIN).scalar_subquery()
    direct = select(user_roles.c.user_id).where(user_roles.c.role_id == admin_role)
    via_group = (
        select(group_members.c.user_id)
        .join(group_roles, group_roles.c.group_id == group_members.c.group_id)
        .where(group_roles.c.role_id == admin_role)
    )
    return int(
        await session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.is_active, or_(User.id.in_(direct), User.id.in_(via_group)))
        )
        or 0
    )


async def get_builtin_role(session: AsyncSession, key: str) -> Role:
    role = await session.scalar(select(Role).where(Role.builtin_key == key))
    if role is None:  # pragma: no cover — роли создаёт миграция 0002
        raise RuntimeError(f"Нет встроенной роли {key}: примените миграции")
    return role


__all__ = [
    "BUILTIN_ADMIN",
    "BUILTIN_GUEST",
    "BUILTIN_USER",
    "Group",
    "Principal",
    "count_active_admins",
    "get_builtin_role",
    "guest_permissions",
    "permissions_for_roles",
    "principal_for_user",
    "roles_of_user",
]
