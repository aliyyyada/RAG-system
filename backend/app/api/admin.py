"""Админка: пользователи, группы, роли и права.

Защита от повышения прав. Пример: у «Специалиста ОТ» есть право admin.users
(управлять людьми), но нет admin.roles и роли «Администратор». Он может добавить
коллегу в группу «Отдел ОТ и ПБ», но не может:
- выдать кому-либо (и себе) роль «Администратор» — это может только администратор;
- выдать роль, в которой есть права, которых нет у него самого.
Отдельно: нельзя убрать последнего активного администратора — иначе в админку
больше никто не попадёт.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, SecretStr, field_validator
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import permissions as perms
from app.auth.deps import SessionDep, client_ip, require_permission
from app.auth.passwords import hash_password, validate_new_password
from app.auth.rbac import (
    BUILTIN_ADMIN,
    BUILTIN_GUEST,
    BUILTIN_USER,
    Principal,
    count_active_admins,
    principal_for_user,
)
from app.auth.service import revoke_user_sessions, write_audit
from app.models import (
    AuthSession,
    Group,
    LoginLockout,
    Role,
    RolePermission,
    User,
    group_members,
    group_roles,
    user_roles,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])

UsersAdmin = Annotated[Principal, Depends(require_permission(perms.ADMIN_USERS))]
RolesAdmin = Annotated[Principal, Depends(require_permission(perms.ADMIN_ROLES))]
# Справочник ролей нужен и тому, кто управляет людьми (выбрать роль в форме).
AnyAccessAdmin = Annotated[
    Principal, Depends(require_permission(perms.ADMIN_USERS, perms.ADMIN_ROLES))
]


def _err(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


# ---------------------------------------------------------------------------
# Схемы
# ---------------------------------------------------------------------------


class Ref(BaseModel):
    id: int
    name: str


class PermissionOut(BaseModel):
    code: str
    title: str
    group: str
    guest_allowed: bool
    coming_soon: bool


class RoleOut(BaseModel):
    id: int
    name: str
    description: str
    builtin_key: str | None
    permissions: list[str]
    users_count: int
    groups_count: int


class RoleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    permissions: list[str] = Field(default_factory=list)


class RoleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    permissions: list[str] | None = None


UserStatus = Literal["active", "blocked", "locked"]


class UserOut(BaseModel):
    id: int
    personnel_number: str
    full_name: str
    department: str | None
    account_type: str
    is_emergency: bool
    is_active: bool
    status: UserStatus
    failed_attempts: int
    locked_until: datetime | None
    last_login_at: datetime | None
    created_at: datetime
    roles: list[Ref]
    groups: list[Ref]


class UserDetailOut(UserOut):
    effective_permissions: list[str]
    effective_roles: list[str]
    active_sessions: int


class UserPage(BaseModel):
    items: list[UserOut]
    total: int
    page: int
    page_size: int


class AssignRoles(BaseModel):
    """«Назначить роль»: человеку, который ещё ни разу не входил, тоже можно."""

    personnel_number: str = Field(min_length=1, max_length=64)
    full_name: str | None = Field(default=None, max_length=255)
    role_ids: list[int] = Field(default_factory=list)
    group_ids: list[int] = Field(default_factory=list)

    @field_validator("personnel_number")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class LocalUserCreate(BaseModel):
    personnel_number: str = Field(min_length=1, max_length=64)
    full_name: str = Field(min_length=1, max_length=255)
    department: str | None = Field(default=None, max_length=255)
    password: SecretStr
    is_emergency: bool = False
    role_ids: list[int] = Field(default_factory=list)
    group_ids: list[int] = Field(default_factory=list)

    @field_validator("personnel_number")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, max_length=255)
    department: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None
    role_ids: list[int] | None = None
    group_ids: list[int] | None = None
    password: SecretStr | None = None  # только локальные учётки


class GroupOut(BaseModel):
    id: int
    name: str
    description: str
    roles: list[Ref]
    members_count: int


class GroupDetailOut(GroupOut):
    members: list[Ref]


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)
    role_ids: list[int] = Field(default_factory=list)
    member_ids: list[int] = Field(default_factory=list)


class GroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    role_ids: list[int] | None = None
    member_ids: list[int] | None = None


# ---------------------------------------------------------------------------
# Общие проверки
# ---------------------------------------------------------------------------


def _ensure_can_grant(principal: Principal, roles: list[Role]) -> None:
    """Можно ли этому администратору выдавать/отбирать эти роли."""
    for role in roles:
        if role.builtin_key == BUILTIN_GUEST:
            raise _err(422, "guest_role", "Роль «Гость» людям не назначается")
        if role.builtin_key == BUILTIN_USER:
            raise _err(422, "user_role", "Роль «Пользователь» есть у всех вошедших автоматически")
        if principal.is_admin:
            continue
        if role.builtin_key == BUILTIN_ADMIN:
            raise _err(403, "admin_only", "Роль «Администратор» выдаёт только администратор")
        extra = role.permissions & perms.all_codes() - principal.permissions
        if extra:
            raise _err(
                403,
                "escalation",
                f"Роль «{role.name}» даёт права, которых нет у вас: {', '.join(sorted(extra))}",
            )


async def _roles_by_ids(session: AsyncSession, ids: list[int]) -> list[Role]:
    unique = set(ids)
    roles = list(await session.scalars(select(Role).where(Role.id.in_(unique))))
    if len(roles) != len(unique):
        raise _err(422, "unknown_role", "Роль не найдена")
    return roles


async def _groups_by_ids(session: AsyncSession, ids: list[int]) -> list[Group]:
    unique = set(ids)
    groups = list(await session.scalars(select(Group).where(Group.id.in_(unique))))
    if len(groups) != len(unique):
        raise _err(422, "unknown_group", "Группа не найдена")
    return groups


async def _users_by_ids(session: AsyncSession, ids: list[int]) -> list[User]:
    unique = set(ids)
    users = list(await session.scalars(select(User).where(User.id.in_(unique))))
    if len(users) != len(unique):
        raise _err(422, "unknown_user", "Пользователь не найден")
    return users


def _changed[T: (Role, Group, User)](old: list[T], new: list[T]) -> list[T]:
    old_ids = {x.id for x in old}
    new_ids = {x.id for x in new}
    return [x for x in old if x.id not in new_ids] + [x for x in new if x.id not in old_ids]


async def _ensure_admin_remains(session: AsyncSession) -> None:
    await session.flush()
    if await count_active_admins(session) == 0:
        await session.rollback()
        raise _err(
            409,
            "last_admin",
            "Нельзя убрать последнего администратора: в админку больше никто не попадёт",
        )


async def _commit(session: AsyncSession, conflict_message: str) -> None:
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise _err(409, "conflict", conflict_message) from None


def _refs(items: list[Role] | list[Group] | list[User]) -> list[Ref]:
    out = []
    for x in sorted(items, key=lambda i: i.id):
        name = x.full_name or x.personnel_number if isinstance(x, User) else x.name
        out.append(Ref(id=x.id, name=name))
    return out


# ---------------------------------------------------------------------------
# Права и роли
# ---------------------------------------------------------------------------


@router.get("/permissions", response_model=list[PermissionOut])
async def list_permissions(_: AnyAccessAdmin) -> list[PermissionOut]:
    return [
        PermissionOut(
            code=p.code,
            title=p.title,
            group=p.group,
            guest_allowed=p.guest_allowed,
            coming_soon=p.coming_soon,
        )
        for p in perms.catalog()
    ]


async def _role_out(session: AsyncSession, role: Role) -> RoleOut:
    users_count = await session.scalar(
        select(func.count()).select_from(user_roles).where(user_roles.c.role_id == role.id)
    )
    groups_count = await session.scalar(
        select(func.count()).select_from(group_roles).where(group_roles.c.role_id == role.id)
    )
    if role.builtin_key == BUILTIN_ADMIN:
        granted = perms.all_codes()
    else:
        granted = role.permissions & perms.all_codes()
    order = [p.code for p in perms.catalog()]
    return RoleOut(
        id=role.id,
        name=role.name,
        description=role.description,
        builtin_key=role.builtin_key,
        permissions=[c for c in order if c in granted],
        users_count=int(users_count or 0),
        groups_count=int(groups_count or 0),
    )


def _sort_key(role: Role) -> tuple[int, int]:
    # Порядок как в макете: Гость, Пользователь, свои роли, Администратор.
    rank = {BUILTIN_GUEST: 0, BUILTIN_USER: 1, BUILTIN_ADMIN: 3}.get(role.builtin_key or "", 2)
    return rank, role.id


@router.get("/roles", response_model=list[RoleOut])
async def list_roles(session: SessionDep, _: AnyAccessAdmin) -> list[RoleOut]:
    roles = sorted(await session.scalars(select(Role)), key=_sort_key)
    return [await _role_out(session, r) for r in roles]


def _validate_role_permissions(
    principal: Principal, role: Role | None, codes: list[str]
) -> set[str]:
    requested = set(codes)
    unknown = requested - perms.all_codes()
    if unknown:
        raise _err(422, "unknown_permission", f"Неизвестные права: {', '.join(sorted(unknown))}")
    if role is not None and role.builtin_key == BUILTIN_ADMIN:
        raise _err(422, "admin_role", "У администратора всегда все права, их нельзя изменить")
    if role is not None and role.builtin_key == BUILTIN_GUEST:
        extra = requested - perms.guest_allowed_codes()
        if extra:
            raise _err(
                422,
                "guest_limit",
                "Гостю можно выдать только поиск по общим документам",
            )
    if not principal.is_admin:
        current = role.permissions if role is not None else set()
        added_or_removed = requested ^ current
        foreign = added_or_removed - principal.permissions
        if foreign:
            raise _err(
                403,
                "escalation",
                f"Нельзя менять права, которых нет у вас: {', '.join(sorted(foreign))}",
            )
    return requested


@router.post("/roles", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
async def create_role(
    body: RoleCreate, request: Request, session: SessionDep, principal: RolesAdmin
) -> RoleOut:
    codes = _validate_role_permissions(principal, None, body.permissions)
    role = Role(
        name=body.name.strip(),
        description=body.description.strip(),
        permission_rows=[RolePermission(permission=c) for c in sorted(codes)],
    )
    session.add(role)
    await write_audit(
        session,
        "role.create",
        actor=principal.user,
        target_type="role",
        details={"name": role.name, "permissions": sorted(codes)},
        ip=client_ip(request),
    )
    await _commit(session, "Роль с таким названием уже есть")
    return await _role_out(session, role)


@router.patch("/roles/{role_id}", response_model=RoleOut)
async def update_role(
    role_id: int, body: RoleUpdate, request: Request, session: SessionDep, principal: RolesAdmin
) -> RoleOut:
    role = await session.get(Role, role_id)
    if role is None:
        raise _err(404, "not_found", "Роль не найдена")
    changes: dict[str, object] = {}
    if body.name is not None and body.name.strip() != role.name:
        if role.builtin_key:
            raise _err(422, "builtin", "Встроенную роль нельзя переименовать")
        changes["name"] = [role.name, body.name.strip()]
        role.name = body.name.strip()
    if body.description is not None:
        role.description = body.description.strip()
    if body.permissions is not None:
        codes = _validate_role_permissions(principal, role, body.permissions)
        before = role.permissions
        if codes != before:
            changes["added"] = sorted(codes - before)
            changes["removed"] = sorted(before - codes)
            role.permission_rows = [RolePermission(permission=c) for c in sorted(codes)]
    await write_audit(
        session,
        "role.update",
        actor=principal.user,
        target_type="role",
        target_id=role.id,
        details={"name": role.name, **changes},
        ip=client_ip(request),
    )
    await _commit(session, "Роль с таким названием уже есть")
    await session.refresh(role)
    return await _role_out(session, role)


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(
    role_id: int, request: Request, session: SessionDep, principal: RolesAdmin
) -> None:
    role = await session.get(Role, role_id)
    if role is None:
        raise _err(404, "not_found", "Роль не найдена")
    if role.builtin_key:
        raise _err(422, "builtin", "Встроенную роль нельзя удалить")
    if not principal.is_admin:
        _ensure_can_grant(principal, [role])
    await session.delete(role)
    await write_audit(
        session,
        "role.delete",
        actor=principal.user,
        target_type="role",
        target_id=role_id,
        details={"name": role.name},
        ip=client_ip(request),
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Пользователи
# ---------------------------------------------------------------------------


def _status(user: User, lock: LoginLockout | None, now: datetime) -> UserStatus:
    if not user.is_active:
        return "blocked"
    if lock is not None and lock.locked_until is not None and lock.locked_until > now:
        return "locked"
    return "active"


def _user_out(user: User, lock: LoginLockout | None) -> UserOut:
    now = datetime.now(UTC)
    locked = lock is not None and lock.locked_until is not None and lock.locked_until > now
    return UserOut(
        id=user.id,
        personnel_number=user.personnel_number,
        full_name=user.full_name,
        department=user.department,
        account_type=user.account_type,
        is_emergency=user.is_emergency,
        is_active=user.is_active,
        status=_status(user, lock, now),
        failed_attempts=lock.failed_count if lock is not None else 0,
        locked_until=lock.locked_until if locked and lock is not None else None,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
        roles=_refs(user.roles),
        groups=_refs(user.groups),
    )


async def _load_user(session: AsyncSession, user_id: int) -> tuple[User, LoginLockout | None]:
    row = (
        await session.execute(
            select(User, LoginLockout)
            .outerjoin(LoginLockout, LoginLockout.login == User.personnel_number)
            .where(User.id == user_id)
            .options(selectinload(User.roles), selectinload(User.groups))
            .execution_options(populate_existing=True)
        )
    ).first()
    if row is None:
        raise _err(404, "not_found", "Пользователь не найден")
    return row[0], row[1]


async def _user_detail(session: AsyncSession, user_id: int) -> UserDetailOut:
    user, lock = await _load_user(session, user_id)
    principal = await principal_for_user(session, user)
    sessions = await session.scalar(
        select(func.count())
        .select_from(AuthSession)
        .where(
            AuthSession.user_id == user.id,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > func.now(),
        )
    )
    return UserDetailOut(
        **_user_out(user, lock).model_dump(),
        effective_permissions=sorted(principal.permissions),
        effective_roles=list(principal.roles),
        active_sessions=int(sessions or 0),
    )


@router.get("/users", response_model=UserPage)
async def list_users(
    session: SessionDep,
    _: UsersAdmin,
    q: Annotated[str | None, Query(max_length=100)] = None,
    group_id: int | None = None,
    role_id: int | None = None,
    account_type: Literal["external", "local", "test"] | None = None,
    status_filter: Annotated[UserStatus | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> UserPage:
    stmt = select(User, LoginLockout).outerjoin(
        LoginLockout, LoginLockout.login == User.personnel_number
    )
    if q:
        escaped = q.strip().replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        pattern = f"%{escaped}%"
        stmt = stmt.where(or_(User.personnel_number.ilike(pattern), User.full_name.ilike(pattern)))
    if group_id is not None:
        stmt = stmt.where(
            User.id.in_(select(group_members.c.user_id).where(group_members.c.group_id == group_id))
        )
    if role_id is not None:
        stmt = stmt.where(
            User.id.in_(select(user_roles.c.user_id).where(user_roles.c.role_id == role_id))
        )
    if account_type is not None:
        stmt = stmt.where(User.account_type == account_type)
    if status_filter == "blocked":
        stmt = stmt.where(User.is_active.is_(False))
    elif status_filter == "locked":
        stmt = stmt.where(User.is_active, LoginLockout.locked_until > func.now())
    elif status_filter == "active":
        stmt = stmt.where(
            User.is_active,
            or_(LoginLockout.locked_until.is_(None), LoginLockout.locked_until <= func.now()),
        )

    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = await session.execute(
        stmt.order_by(User.last_login_at.desc().nulls_last(), User.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return UserPage(
        items=[_user_out(u, lock) for u, lock in rows.all()],
        total=int(total or 0),
        page=page,
        page_size=page_size,
    )


@router.get("/users/{user_id}", response_model=UserDetailOut)
async def get_user(user_id: int, session: SessionDep, _: UsersAdmin) -> UserDetailOut:
    return await _user_detail(session, user_id)


@router.post("/users", response_model=UserDetailOut)
async def assign_roles(
    body: AssignRoles, request: Request, session: SessionDep, principal: UsersAdmin
) -> UserDetailOut:
    """Назначить роли и группы человеку по табельному номеру.

    Если человек ещё не входил, создаётся запись внешнего пользователя: при первом входе
    роли уже будут на месте (так назначается первый администратор в админке).
    """
    roles = await _roles_by_ids(session, body.role_ids)
    groups = await _groups_by_ids(session, body.group_ids)
    _ensure_can_grant(principal, roles + [r for g in groups for r in g.roles])
    user = await session.scalar(select(User).where(User.personnel_number == body.personnel_number))
    created = user is None
    if user is None:
        user = User(
            personnel_number=body.personnel_number,
            full_name=(body.full_name or "").strip(),
            account_type="external",
            roles=[],
            groups=[],
        )
        session.add(user)
    for role in roles:
        if role not in user.roles:
            user.roles.append(role)
    for group in groups:
        if group not in user.groups:
            user.groups.append(group)
    await session.flush()
    await write_audit(
        session,
        "user.assign",
        actor=principal.user,
        target_type="user",
        target_id=user.id,
        details={
            "personnel_number": user.personnel_number,
            "created": created,
            "roles": [r.name for r in roles],
            "groups": [g.name for g in groups],
        },
        ip=client_ip(request),
    )
    await _commit(session, "Пользователь с таким табельным номером уже есть")
    return await _user_detail(session, user.id)


@router.post("/users/local", response_model=UserDetailOut, status_code=status.HTTP_201_CREATED)
async def create_local_user(
    body: LocalUserCreate, request: Request, session: SessionDep, principal: UsersAdmin
) -> UserDetailOut:
    if body.is_emergency and not principal.is_admin:
        raise _err(403, "admin_only", "Аварийную учётку создаёт только администратор")
    try:
        validate_new_password(body.password.get_secret_value())
    except ValueError as exc:
        raise _err(422, "weak_password", str(exc)) from None
    roles = await _roles_by_ids(session, body.role_ids)
    groups = await _groups_by_ids(session, body.group_ids)
    _ensure_can_grant(principal, roles + [r for g in groups for r in g.roles])
    exists = await session.scalar(
        select(User.id).where(User.personnel_number == body.personnel_number)
    )
    if exists is not None:
        raise _err(409, "conflict", "Пользователь с таким логином уже есть")
    user = User(
        personnel_number=body.personnel_number,
        full_name=body.full_name.strip(),
        department=(body.department or "").strip() or None,
        account_type="local",
        password_hash=hash_password(body.password.get_secret_value()),
        is_emergency=body.is_emergency,
        roles=roles,
        groups=groups,
    )
    session.add(user)
    await session.flush()
    await write_audit(
        session,
        "user.create_local",
        actor=principal.user,
        target_type="user",
        target_id=user.id,
        details={
            "personnel_number": user.personnel_number,
            "emergency": user.is_emergency,
            "roles": [r.name for r in roles],
        },
        ip=client_ip(request),
    )
    await _commit(session, "Пользователь с таким логином уже есть")
    return await _user_detail(session, user.id)


@router.patch("/users/{user_id}", response_model=UserDetailOut)
async def update_user(
    user_id: int, body: UserUpdate, request: Request, session: SessionDep, principal: UsersAdmin
) -> UserDetailOut:
    user, _lock = await _load_user(session, user_id)
    details: dict[str, object] = {"personnel_number": user.personnel_number}

    if body.full_name is not None:
        user.full_name = body.full_name.strip()
    if body.department is not None:
        user.department = body.department.strip() or None
    if body.role_ids is not None:
        new_roles = await _roles_by_ids(session, body.role_ids)
        changed = _changed(list(user.roles), new_roles)
        _ensure_can_grant(principal, changed)
        if changed:
            details["roles"] = [r.name for r in new_roles]
        user.roles = new_roles
    if body.group_ids is not None:
        new_groups = await _groups_by_ids(session, body.group_ids)
        changed_groups = _changed(list(user.groups), new_groups)
        _ensure_can_grant(principal, [r for g in changed_groups for r in g.roles])
        if changed_groups:
            details["groups"] = [g.name for g in new_groups]
        user.groups = new_groups
    if body.is_active is not None and body.is_active != user.is_active:
        if principal.user is not None and principal.user.id == user.id and not body.is_active:
            raise _err(422, "self_block", "Нельзя заблокировать самого себя")
        user.is_active = body.is_active
        details["is_active"] = body.is_active
        if not body.is_active:
            await revoke_user_sessions(session, user.id)
    if body.password is not None:
        if user.account_type == "external":
            raise _err(
                422, "external_password", "Пароль внешнего пользователя меняется во внешней системе"
            )
        try:
            validate_new_password(body.password.get_secret_value())
        except ValueError as exc:
            raise _err(422, "weak_password", str(exc)) from None
        user.password_hash = hash_password(body.password.get_secret_value())
        details["password_changed"] = True
        await revoke_user_sessions(session, user.id)

    await _ensure_admin_remains(session)
    await write_audit(
        session,
        "user.update",
        actor=principal.user,
        target_type="user",
        target_id=user.id,
        details=details,
        ip=client_ip(request),
    )
    await session.commit()
    return await _user_detail(session, user.id)


@router.post("/users/{user_id}/unlock", response_model=UserDetailOut)
async def unlock_user(
    user_id: int, request: Request, session: SessionDep, principal: UsersAdmin
) -> UserDetailOut:
    """Снять временную блокировку за неверный пароль."""
    user, _lock = await _load_user(session, user_id)
    await session.execute(delete(LoginLockout).where(LoginLockout.login == user.personnel_number))
    await write_audit(
        session,
        "user.unlock",
        actor=principal.user,
        target_type="user",
        target_id=user.id,
        details={"personnel_number": user.personnel_number},
        ip=client_ip(request),
    )
    await session.commit()
    return await _user_detail(session, user.id)


@router.post("/users/{user_id}/revoke-sessions", response_model=UserDetailOut)
async def revoke_sessions(
    user_id: int, request: Request, session: SessionDep, principal: UsersAdmin
) -> UserDetailOut:
    """Завершить все сессии человека (например, потерял пропуск / ушёл с общего ПК)."""
    user, _lock = await _load_user(session, user_id)
    count = await revoke_user_sessions(session, user.id)
    await write_audit(
        session,
        "user.revoke_sessions",
        actor=principal.user,
        target_type="user",
        target_id=user.id,
        details={"personnel_number": user.personnel_number, "sessions": count},
        ip=client_ip(request),
    )
    await session.commit()
    return await _user_detail(session, user.id)


# ---------------------------------------------------------------------------
# Группы
# ---------------------------------------------------------------------------


async def _group_out(session: AsyncSession, group: Group) -> GroupOut:
    count = await session.scalar(
        select(func.count()).select_from(group_members).where(group_members.c.group_id == group.id)
    )
    return GroupOut(
        id=group.id,
        name=group.name,
        description=group.description,
        roles=_refs(group.roles),
        members_count=int(count or 0),
    )


async def _get_group(session: AsyncSession, group_id: int) -> Group:
    # populate_existing + явные selectinload: группа могла уже попасть в сессию через
    # User.groups текущего пользователя, и тогда её коллекции не загружены (цикл User↔Group).
    group = await session.scalar(
        select(Group)
        .where(Group.id == group_id)
        .options(selectinload(Group.members), selectinload(Group.roles))
        .execution_options(populate_existing=True)
    )
    if group is None:
        raise _err(404, "not_found", "Группа не найдена")
    return group


async def _group_detail(session: AsyncSession, group_id: int) -> GroupDetailOut:
    group = await _get_group(session, group_id)
    base = await _group_out(session, group)
    return GroupDetailOut(**base.model_dump(), members=_refs(group.members))


@router.get("/groups", response_model=list[GroupOut])
async def list_groups(session: SessionDep, _: UsersAdmin) -> list[GroupOut]:
    groups = await session.scalars(select(Group).order_by(Group.name))
    return [await _group_out(session, g) for g in groups]


@router.get("/groups/{group_id}", response_model=GroupDetailOut)
async def get_group(group_id: int, session: SessionDep, _: UsersAdmin) -> GroupDetailOut:
    return await _group_detail(session, group_id)


@router.post("/groups", response_model=GroupDetailOut, status_code=status.HTTP_201_CREATED)
async def create_group(
    body: GroupCreate, request: Request, session: SessionDep, principal: UsersAdmin
) -> GroupDetailOut:
    roles = await _roles_by_ids(session, body.role_ids)
    members = await _users_by_ids(session, body.member_ids)
    _ensure_can_grant(principal, roles)
    group = Group(
        name=body.name.strip(), description=body.description.strip(), roles=roles, members=members
    )
    session.add(group)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise _err(409, "conflict", "Группа с таким названием уже есть") from None
    await write_audit(
        session,
        "group.create",
        actor=principal.user,
        target_type="group",
        target_id=group.id,
        details={"name": group.name, "roles": [r.name for r in roles], "members": len(members)},
        ip=client_ip(request),
    )
    await session.commit()
    return await _group_detail(session, group.id)


@router.patch("/groups/{group_id}", response_model=GroupDetailOut)
async def update_group(
    group_id: int, body: GroupUpdate, request: Request, session: SessionDep, principal: UsersAdmin
) -> GroupDetailOut:
    group = await _get_group(session, group_id)
    details: dict[str, object] = {"name": group.name}
    if body.name is not None and body.name.strip() != group.name:
        details["renamed"] = [group.name, body.name.strip()]
        group.name = body.name.strip()
    if body.description is not None:
        group.description = body.description.strip()
    if body.role_ids is not None:
        new_roles = await _roles_by_ids(session, body.role_ids)
        _ensure_can_grant(principal, _changed(list(group.roles), new_roles))
        details["roles"] = [r.name for r in new_roles]
        group.roles = new_roles
    if body.member_ids is not None:
        new_members = await _users_by_ids(session, body.member_ids)
        if _changed(list(group.members), new_members):
            # Добавить человека в группу = выдать ему роли группы.
            _ensure_can_grant(principal, list(group.roles))
        details["members"] = len(new_members)
        group.members = new_members
    await _ensure_admin_remains(session)
    await write_audit(
        session,
        "group.update",
        actor=principal.user,
        target_type="group",
        target_id=group.id,
        details=details,
        ip=client_ip(request),
    )
    await _commit(session, "Группа с таким названием уже есть")
    return await _group_detail(session, group.id)


@router.delete("/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: int, request: Request, session: SessionDep, principal: UsersAdmin
) -> None:
    group = await _get_group(session, group_id)
    if group.members:
        _ensure_can_grant(principal, list(group.roles))
    name = group.name
    await session.delete(group)
    await _ensure_admin_remains(session)
    await write_audit(
        session,
        "group.delete",
        actor=principal.user,
        target_type="group",
        target_id=group_id,
        details={"name": name},
        ip=client_ip(request),
    )
    await session.commit()
