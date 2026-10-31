"""Команды администрирования из консоли.

Запуск на сервере (из папки установки):

    docker compose exec app python -m app.cli create-admin 00017
    docker compose exec app python -m app.cli create-local-user admin \\
        --name "Аварийный администратор" --admin --emergency
    docker compose exec app python -m app.cli seed-test-users        # только разработка

create-admin — первый администратор (без него в админку попасть нельзя): выдаёт роль
«Администратор» человеку с этим табельным номером. Если человек ещё не входил,
запись создаётся заранее, при первом входе через внешний сервис роль уже будет на месте.

Пароль для create-local-user спрашивается с клавиатуры (не виден при вводе) или берётся
из переменной окружения RAG_NEW_PASSWORD (для автоматической установки). В аргументах
командной строки пароль не передаётся: он остался бы в истории команд.
"""

import argparse
import asyncio
import getpass
import os
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.passwords import hash_password, validate_new_password
from app.auth.rbac import BUILTIN_ADMIN, get_builtin_role
from app.auth.service import write_audit
from app.config import get_settings
from app.db import dispose_engine, get_sessionmaker
from app.models import Group, Role, RolePermission, User

# Пароль всех тестовых учёток. Работает только при ENABLE_TEST_USERS=true (в prod запрещено).
TEST_PASSWORD = "test-password-1"  # noqa: S105 — тестовые учётки разработки

# Набор тестовых пользователей с разными ролями и доступами (ТЗ, п. 4).
# Доступ к документам источника появится на этапе 3 (фейковый сервис отдаёт списки id).
TEST_USERS: tuple[tuple[str, str, str | None, tuple[str, ...]], ...] = (
    ("T-0001", "Тест: обычный пользователь", None, ()),
    ("T-0002", "Тест: аналитик", "Тестовая группа аналитиков", ()),
    ("T-0003", "Тест: без доступа к источнику X", None, ()),
    ("T-0004", "Тест: администратор", None, ("Администратор",)),
)
ANALYST_ROLE = "Аналитик"
ANALYST_PERMISSIONS = ("search.common", "documents.view", "contradictions.view", "audit.view")


async def _user(session: AsyncSession, personnel_number: str) -> User | None:
    return await session.scalar(select(User).where(User.personnel_number == personnel_number))


async def create_admin(personnel_number: str, full_name: str | None) -> str:
    sm = get_sessionmaker()
    async with sm() as session:
        admin_role = await get_builtin_role(session, BUILTIN_ADMIN)
        user = await _user(session, personnel_number)
        if user is None:
            user = User(
                personnel_number=personnel_number,
                full_name=full_name or "",
                account_type="external",
                roles=[],
                groups=[],
            )
            session.add(user)
        if admin_role not in user.roles:
            user.roles.append(admin_role)
        if not user.is_active:
            user.is_active = True
        await session.flush()
        await write_audit(
            session,
            "cli.create_admin",
            target_type="user",
            target_id=user.id,
            details={"personnel_number": personnel_number},
        )
        await session.commit()
    return f"Готово: {personnel_number} — администратор"


def _read_password() -> str:
    password = os.environ.get("RAG_NEW_PASSWORD")
    if password is None:
        password = getpass.getpass("Пароль: ")
        if getpass.getpass("Повторите пароль: ") != password:
            raise SystemExit("Пароли не совпадают")
    validate_new_password(password)
    return password


async def create_local_user(
    personnel_number: str, full_name: str, admin: bool, emergency: bool, password: str
) -> str:
    sm = get_sessionmaker()
    async with sm() as session:
        if await _user(session, personnel_number) is not None:
            raise SystemExit(f"Пользователь {personnel_number} уже есть")
        roles = [await get_builtin_role(session, BUILTIN_ADMIN)] if admin else []
        user = User(
            personnel_number=personnel_number,
            full_name=full_name,
            account_type="local",
            password_hash=hash_password(password),
            is_emergency=emergency,
            roles=roles,
            groups=[],
        )
        session.add(user)
        await session.flush()
        await write_audit(
            session,
            "cli.create_local_user",
            target_type="user",
            target_id=user.id,
            details={"personnel_number": personnel_number, "admin": admin, "emergency": emergency},
        )
        await session.commit()
    return f"Готово: локальная учётка {personnel_number}"


async def seed_test_users() -> str:
    if not get_settings().enable_test_users:
        raise SystemExit("Тестовые учётки выключены (ENABLE_TEST_USERS=false)")
    sm = get_sessionmaker()
    async with sm() as session:
        analyst = await session.scalar(select(Role).where(Role.name == ANALYST_ROLE))
        if analyst is None:
            analyst = Role(
                name=ANALYST_ROLE,
                description="Тестовая роль: поиск, противоречия, журнал аудита",
                permission_rows=[RolePermission(permission=p) for p in ANALYST_PERMISSIONS],
            )
            session.add(analyst)
        group_name = "Тестовая группа аналитиков"
        group = await session.scalar(select(Group).where(Group.name == group_name))
        if group is None:
            group = Group(name=group_name, description="Для проверки прав через группу")
            group.roles = [analyst]
            group.members = []
            session.add(group)
        await session.flush()

        created = 0
        password_hash = hash_password(TEST_PASSWORD)
        for number, name, group_ref, role_names in TEST_USERS:
            user = await _user(session, number)
            if user is None:
                user = User(
                    personnel_number=number,
                    full_name=name,
                    account_type="test",
                    password_hash=password_hash,
                    roles=[],
                    groups=[],
                )
                session.add(user)
                created += 1
            for role_name in role_names:
                role = await session.scalar(select(Role).where(Role.name == role_name))
                if role is not None and role not in user.roles:
                    user.roles.append(role)
            if group_ref == group.name and group not in user.groups:
                user.groups.append(group)
        await session.commit()
    return f"Готово: тестовых учёток создано {created}, пароль у всех: {TEST_PASSWORD}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create-admin", help="Назначить администратора по табельному номеру")
    p.add_argument("personnel_number")
    p.add_argument("--name", help="ФИО, если человек ещё не входил")

    p = sub.add_parser("create-local-user", help="Создать локальную учётку (пароль у нас)")
    p.add_argument("personnel_number", help="Логин, например admin")
    p.add_argument("--name", required=True, help="ФИО или описание учётки")
    p.add_argument("--admin", action="store_true", help="Выдать роль «Администратор»")
    p.add_argument(
        "--emergency",
        action="store_true",
        help="Аварийная учётка: работает в prod, когда внешний сервис недоступен",
    )

    sub.add_parser("seed-test-users", help="Создать тестовые учётки (только разработка)")

    args = parser.parse_args(argv)

    async def run() -> str:
        try:
            if args.cmd == "create-admin":
                return await create_admin(args.personnel_number.strip(), args.name)
            if args.cmd == "create-local-user":
                try:
                    password = _read_password()
                except ValueError as exc:
                    raise SystemExit(str(exc)) from None
                return await create_local_user(
                    args.personnel_number.strip(), args.name, args.admin, args.emergency, password
                )
            return await seed_test_users()
        finally:
            await dispose_engine()

    print(asyncio.run(run()))
    sys.exit(0)


if __name__ == "__main__":
    main()
