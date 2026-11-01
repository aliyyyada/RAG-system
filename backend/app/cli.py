"""Команды администрирования из консоли.

Запуск на сервере (из папки установки):

    docker compose exec app python -m app.cli create-admin 00017
    docker compose exec app python -m app.cli create-local-user admin \\
        --name "Аварийный администратор" --admin --emergency
    docker compose exec app python -m app.cli seed-test-users        # только разработка
    docker compose exec app python -m app.cli add-source docflow --name "Документооборот" \
        --url https://docflow.zavod.local/api/rag
    docker compose exec app python -m app.cli seed-fake-source       # только разработка
    docker compose exec app python -m app.cli source-token docflow   # токен для API источника
    docker compose exec app python -m app.cli sync docflow [--reconcile]

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
from app.models import Group, Role, RolePermission, Source, User

# Пароль всех тестовых учёток. Работает только при ENABLE_TEST_USERS=true (в prod запрещено).
TEST_PASSWORD = "test-password-1"  # noqa: S105 — тестовые учётки разработки

# Набор тестовых пользователей с разными ролями и доступами (ТЗ, п. 4).
# Доступ к документам им даёт фейковый источник: таблица ACCESS в fakes/fake_source.py.
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


# Фейковый источник документов (контейнер fake-service), только разработка.
FAKE_SOURCE_URL = "https://fake-service:8443/source"


async def add_source(code: str, name: str, url: str) -> str:
    from app.ingest.service import ServiceError, create_source

    async with get_sessionmaker()() as session:
        try:
            src = await create_source(session, code=code, name=name, base_url=url)
        except ServiceError as exc:
            raise SystemExit(exc.message) from None
        await write_audit(
            session,
            "cli.add_source",
            target_type="source",
            target_id=src.id,
            details={"code": code},
        )
        await session.commit()
    return f"Готово: источник {code} подключён, первая синхронизация начнётся в течение минуты"


async def seed_fake_source(url: str) -> str:
    if not get_settings().enable_test_users:
        raise SystemExit("Фейковый источник только для разработки (ENABLE_TEST_USERS=true)")
    async with get_sessionmaker()() as session:
        if await session.scalar(select(Source).where(Source.code == "docflow")):
            return "Источник docflow уже подключён"
    return await add_source("docflow", "Документооборот (тестовый)", url)


async def _source_by_code(session: AsyncSession, code: str) -> Source:
    src = await session.scalar(select(Source).where(Source.code == code))
    if src is None:
        raise SystemExit(f"Нет источника с кодом {code}")
    return src


async def source_token(code: str) -> str:
    from app.ingest.service import issue_callback_token

    async with get_sessionmaker()() as session:
        src = await _source_by_code(session, code)
        token = await issue_callback_token(session, src)
        await write_audit(session, "cli.source_token", target_type="source", target_id=src.id)
        await session.commit()
    return (
        f"Токен для API источника {code} (показывается один раз, предыдущий больше не действует):"
        f"\n{token}"
    )


async def sync_source(code: str, reconcile: bool) -> str:
    from app.ingest.service import request_sync

    async with get_sessionmaker()() as session:
        src = await _source_by_code(session, code)
        queued = await request_sync(session, src, reconcile=reconcile)
        await session.commit()
    what = "Ночная сверка" if reconcile else "Синхронизация"
    return f"{what} поставлена в очередь" if queued else f"{what} уже стоит в очереди"


async def eval_search(path: str, as_user: str | None, mode: str | None = None) -> str:
    """Оценка качества поиска: recall@k по набору вопросов (app/search/evaluate.py).

    --mode all — сравнение режимов chunks / graph / mixed одной таблицей."""
    import json
    from pathlib import Path

    from app.config import get_settings
    from app.search.evaluate import evaluate, load_questions

    data = load_questions(Path(path))
    if as_user:
        data["as_user"] = as_user
    if mode != "all":
        async with get_sessionmaker()() as session:
            report = await evaluate(session, data, get_settings(), mode)
        return json.dumps(report, ensure_ascii=False, indent=2)
    lines = ["режим   | вопросы | recall@1 | recall@5 | MRR   | о связях: recall@5"]
    for m in ("chunks", "graph", "mixed"):
        async with get_sessionmaker()() as session:
            r = await evaluate(session, data, get_settings(), m)
        graph_part = r["by_kind"].get("graph", {}).get("recall", {}).get(5, "—")
        lines.append(
            f"{m:<7} | {r['questions']:>7} | {r['recall'][1]:>8} | {r['recall'][5]:>8} "
            f"| {r['mrr']:<5} | {graph_part}"
        )
    return "\n".join(lines)


async def graph_estimate(sample: int) -> str:
    """Замер времени извлечения сущностей моделью на выборке и прогноз (app/graph/estimate.py)."""
    from app.config import get_settings
    from app.graph.estimate import estimate, report

    result = await estimate(get_settings(), get_sessionmaker(), sample=sample, actor="cli")
    return report(result)


async def graph_llm(enabled: bool) -> str:
    """Включить или выключить извлечение сущностей моделью (то же, что кнопка в админке)."""
    from app.core.flags import GRAPH_LLM_ENABLED, set_flag
    from app.graph.extract import backfill_tick

    async with get_sessionmaker()() as session, session.begin():
        await set_flag(session, GRAPH_LLM_ENABLED, enabled, "cli")
        n = await backfill_tick(session, limit=100_000) if enabled else 0
    if enabled:
        return f"Извлечение моделью включено, в очередь поставлено документов: {n}"
    return "Извлечение моделью выключено (обработанное остаётся в графе)"


async def contra(enabled: bool) -> str:
    """Включить или выключить поиск противоречий (то же, что кнопка в админке)."""
    from app.contradictions.scan import backfill_tick
    from app.core.flags import CONTRA_ENABLED, set_flag

    async with get_sessionmaker()() as session, session.begin():
        await set_flag(session, CONTRA_ENABLED, enabled, "cli")
        n = await backfill_tick(session, limit=100_000) if enabled else 0
    if enabled:
        return f"Поиск противоречий включён, в очередь поставлено документов: {n}"
    return "Поиск противоречий выключен (найденное остаётся)"


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

    p = sub.add_parser("add-source", help="Подключить источник документов")
    p.add_argument("code", help="Короткий код латиницей, например docflow")
    p.add_argument("--name", required=True, help="Название, например «Документооборот»")
    p.add_argument("--url", required=True, help="Адрес API источника (https://…)")

    p = sub.add_parser("seed-fake-source", help="Подключить фейковый источник (разработка)")
    p.add_argument("--url", default=FAKE_SOURCE_URL)

    p = sub.add_parser("source-token", help="Выпустить токен для API «что у вас загружено»")
    p.add_argument("code")

    p = sub.add_parser("sync", help="Запустить синхронизацию источника сейчас")
    p.add_argument("code")
    p.add_argument("--reconcile", action="store_true", help="Полная сверка вместо инкремента")

    p = sub.add_parser("eval-search", help="Оценить качество поиска (recall@k) по набору вопросов")
    p.add_argument("--file", default="eval/questions.json")
    p.add_argument("--as-user", help="От чьего имени искать (по умолчанию as_user из файла)")
    p.add_argument(
        "--mode",
        choices=["chunks", "graph", "mixed", "all"],
        help="Режим поиска (по умолчанию SEARCH_MODE); all — сравнить все три",
    )

    p = sub.add_parser("graph-estimate", help="Замерить время извлечения сущностей и дать прогноз")
    p.add_argument("--sample", type=int, default=20, help="Сколько фрагментов отправить в модель")

    p = sub.add_parser("graph-llm", help="Включить / выключить извлечение сущностей моделью")
    p.add_argument("state", choices=["on", "off"])

    p = sub.add_parser("contra", help="Включить / выключить поиск противоречий моделью")
    p.add_argument("state", choices=["on", "off"])

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
            if args.cmd == "add-source":
                if not args.url.startswith("https://"):
                    raise SystemExit("Адрес источника должен начинаться с https://")
                return await add_source(args.code, args.name, args.url)
            if args.cmd == "seed-fake-source":
                return await seed_fake_source(args.url)
            if args.cmd == "source-token":
                return await source_token(args.code)
            if args.cmd == "sync":
                return await sync_source(args.code, args.reconcile)
            if args.cmd == "eval-search":
                return await eval_search(args.file, args.as_user, args.mode)
            if args.cmd == "graph-estimate":
                return await graph_estimate(args.sample)
            if args.cmd == "graph-llm":
                return await graph_llm(args.state == "on")
            if args.cmd == "contra":
                return await contra(args.state == "on")
            return await seed_test_users()
        finally:
            await dispose_engine()

    print(asyncio.run(run()))
    sys.exit(0)


if __name__ == "__main__":
    main()
