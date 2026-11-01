"""Вход, обновление и завершение сессии.

Порядок входа (пример: сотрудник 10234 вводит пароль):
1. Если логин временно заблокирован (5 ошибок подряд) — отказ без проверки пароля.
2. Локальная или тестовая учётка → сверяем argon2-хэш у нас.
   Иначе → спрашиваем внешний сервис через AuthProvider.
3. Ошибка пароля → счётчик +1; на пятой — блокировка на 15 минут.
4. Успех → создаём пользователя при первом входе, сбрасываем счётчик,
   выдаём сессию (refresh-cookie) и access-токен, пишем в журнал входов.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.service import reset_cache as reset_access_cache
from app.auth.passwords import verify_password
from app.auth.providers import AuthOutcome, AuthProvider
from app.auth.tokens import create_access_token, hash_refresh_token, new_refresh_token
from app.config import Settings
from app.models import AuditLog, AuthSession, LoginEvent, LoginLockout, User

log = logging.getLogger(__name__)

# Сколько секунд после ротации ещё принимаем предыдущий refresh-токен
# (две вкладки обновили токен одновременно).
ROTATION_GRACE_SECONDS = 60


@dataclass(frozen=True)
class ClientInfo:
    ip: str | None
    user_agent: str | None


@dataclass(frozen=True)
class IssuedSession:
    user: User
    session_id: uuid.UUID
    access_token: str
    refresh_token: str | None  # None — cookie менять не нужно (льготное окно ротации)
    expires_at: datetime


class LoginError(Exception):
    """Отказ во входе. `code` уходит во фронтенд, текст показывается пользователю."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int = 401,
        remaining_attempts: int | None = None,
        locked_until: datetime | None = None,
        lockout_minutes: int | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.remaining_attempts = remaining_attempts
        self.locked_until = locked_until
        self.lockout_minutes = lockout_minutes


def _now() -> datetime:
    return datetime.now(UTC)


def normalize_login(login: str) -> str:
    return login.strip()[:64]


async def write_audit(
    session: AsyncSession,
    action: str,
    *,
    actor: User | None = None,
    target_type: str | None = None,
    target_id: object | None = None,
    details: dict[str, object] | None = None,
    ip: str | None = None,
) -> None:
    session.add(
        AuditLog(
            actor_user_id=actor.id if actor else None,
            actor_login=actor.personnel_number if actor else None,
            action=action,
            target_type=target_type,
            target_id=None if target_id is None else str(target_id),
            details=details or {},
            ip=ip,
        )
    )


async def _log_event(
    session: AsyncSession,
    login: str,
    result: str,
    client: ClientInfo,
    user: User | None = None,
) -> None:
    session.add(
        LoginEvent(
            login=login,
            user_id=user.id if user else None,
            result=result,
            account_type=user.account_type if user else None,
            ip=client.ip,
            user_agent=(client.user_agent or "")[:512] or None,
        )
    )


async def _register_failure(
    session: AsyncSession, settings: Settings, login: str, client: ClientInfo, user: User | None
) -> LoginError:
    now = _now()
    # Атомарный upsert: два одновременных неверных пароля не потеряют попытку
    # и не упадут на дубликате ключа. Истёкшая блокировка начинает отсчёт заново.
    expired = LoginLockout.locked_until.is_not(None) & (LoginLockout.locked_until <= now)
    stmt = pg_insert(LoginLockout).values(login=login, failed_count=1, last_failed_at=now)
    stmt = stmt.on_conflict_do_update(
        index_elements=[LoginLockout.login],
        set_={
            "failed_count": case((expired, 1), else_=LoginLockout.failed_count + 1),
            "locked_until": case((expired, None), else_=LoginLockout.locked_until),
            "last_failed_at": now,
        },
    ).returning(LoginLockout.failed_count)
    failed_count = int((await session.execute(stmt)).scalar_one())
    remaining = settings.login_max_attempts - failed_count
    if remaining <= 0:
        until = now + timedelta(minutes=settings.login_lockout_minutes)
        await session.execute(
            update(LoginLockout).where(LoginLockout.login == login).values(locked_until=until)
        )
        await _log_event(session, login, "invalid_credentials", client, user)
        await session.commit()
        return _locked_error(settings, until)
    await _log_event(session, login, "invalid_credentials", client, user)
    await session.commit()
    return LoginError(
        "invalid_credentials",
        "Неверный табельный номер или пароль",
        remaining_attempts=remaining,
        lockout_minutes=settings.login_lockout_minutes,
    )


def _locked_error(settings: Settings, until: datetime) -> LoginError:
    return LoginError(
        "locked",
        f"Вход заблокирован на {settings.login_lockout_minutes} минут после "
        f"{settings.login_max_attempts} неудачных попыток. Если это были не вы, "
        "сообщите администратору.",
        status=429,
        remaining_attempts=0,
        locked_until=until,
    )


async def login(
    session: AsyncSession,
    settings: Settings,
    provider: AuthProvider,
    raw_login: str,
    password: str,
    client: ClientInfo,
) -> IssuedSession:
    login_name = normalize_login(raw_login)
    if not login_name or not password:
        raise LoginError("invalid_credentials", "Введите табельный номер и пароль", status=422)

    lock = await session.get(LoginLockout, login_name)
    if lock is not None and lock.locked_until is not None and lock.locked_until > _now():
        await _log_event(session, login_name, "locked", client)
        await session.commit()
        raise _locked_error(settings, lock.locked_until)

    user = await session.scalar(select(User).where(User.personnel_number == login_name))

    if user is not None and user.account_type in ("local", "test"):
        if user.account_type == "test" and not settings.enable_test_users:
            await _log_event(session, login_name, "disabled", client, user)
            await session.commit()
            raise LoginError("disabled", "Тестовые учётные записи отключены", status=403)
        if user.account_type == "local" and settings.app_env == "prod" and not user.is_emergency:
            # В prod из локальных работает только аварийная учётка администратора.
            await _log_event(session, login_name, "disabled", client, user)
            await session.commit()
            raise LoginError("disabled", "Локальные учётные записи отключены", status=403)
        if not verify_password(user.password_hash, password):
            raise await _register_failure(session, settings, login_name, client, user)
    else:
        result = await provider.authenticate(login_name, password)
        if result.outcome is AuthOutcome.UNAVAILABLE:
            await _log_event(session, login_name, "provider_unavailable", client, user)
            await session.commit()
            raise LoginError(
                "provider_unavailable",
                "Сервис проверки пароля сейчас недоступен. Попробуйте через несколько минут.",
                status=503,
            )
        if result.outcome is AuthOutcome.INVALID:
            raise await _register_failure(session, settings, login_name, client, user)
        profile = result.profile
        if user is None:
            user = User(personnel_number=login_name, account_type="external", full_name="")
            session.add(user)
        if profile is not None:
            if profile.full_name:
                user.full_name = profile.full_name
            if profile.department:
                user.department = profile.department
        await session.flush()

    assert user is not None
    if not user.is_active:
        await _log_event(session, login_name, "blocked", client, user)
        await session.commit()
        raise LoginError(
            "blocked",
            "Учётная запись заблокирована администратором",
            status=403,
        )

    await session.execute(
        update(LoginLockout)
        .where(LoginLockout.login == login_name)
        .values(failed_count=0, locked_until=None)
    )
    user.last_login_at = _now()
    # Права на документы при входе спрашиваем у источников заново (ТЗ, п. 6): если человеку
    # только что выдали доступ, он увидит документы сразу, а не через 5 минут.
    await reset_access_cache(session, user.id)
    issued = await _create_session(session, settings, user, client)
    await _log_event(session, login_name, "success", client, user)
    if user.account_type == "local":
        # Вход аварийной/локальной учётки — всегда в аудит: это обход внешнего сервиса.
        await write_audit(
            session,
            "auth.local_login",
            actor=user,
            details={"emergency": user.is_emergency},
            ip=client.ip,
        )
    await session.commit()
    return issued


async def _create_session(
    session: AsyncSession, settings: Settings, user: User, client: ClientInfo
) -> IssuedSession:
    token = new_refresh_token()
    expires = _now() + timedelta(hours=settings.refresh_token_hours)
    row = AuthSession(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=hash_refresh_token(token),
        expires_at=expires,
        ip=client.ip,
        user_agent=(client.user_agent or "")[:512] or None,
    )
    session.add(row)
    await session.flush()
    return IssuedSession(
        user=user,
        session_id=row.id,
        access_token=create_access_token(settings, user.id, row.id),
        refresh_token=token,
        expires_at=expires,
    )


class RefreshError(Exception):
    pass


async def refresh(session: AsyncSession, settings: Settings, token: str) -> IssuedSession:
    """Выдать новый access-токен по refresh-cookie и заменить сам refresh-токен."""
    now = _now()
    token_hash = hash_refresh_token(token)
    row = await session.scalar(
        select(AuthSession).where(AuthSession.token_hash == token_hash).with_for_update()
    )
    rotate = True
    if row is None:
        row = await session.scalar(
            select(AuthSession).where(AuthSession.prev_token_hash == token_hash).with_for_update()
        )
        if row is None:
            raise RefreshError("unknown token")
        grace = row.rotated_at and (now - row.rotated_at).total_seconds() <= ROTATION_GRACE_SECONDS
        if not grace:
            # Старый токен предъявлен после ротации — возможно, его украли. Закрываем сессию.
            row.revoked_at = now
            await write_audit(
                session,
                "auth.refresh_reuse",
                target_type="session",
                target_id=row.id,
                details={"user_id": row.user_id},
            )
            await session.commit()
            raise RefreshError("reused token")
        rotate = False

    if row.revoked_at is not None or row.expires_at <= now:
        raise RefreshError("session closed")
    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        raise RefreshError("user inactive")
    if user.account_type == "test" and not settings.enable_test_users:
        raise RefreshError("test users disabled")

    new_token: str | None = None
    if rotate:
        new_token = new_refresh_token()
        row.prev_token_hash = row.token_hash
        row.token_hash = hash_refresh_token(new_token)
        row.rotated_at = now
    row.last_used_at = now
    await session.commit()
    return IssuedSession(
        user=user,
        session_id=row.id,
        access_token=create_access_token(settings, user.id, row.id),
        refresh_token=new_token,
        expires_at=row.expires_at,
    )


async def revoke_session_by_token(session: AsyncSession, token: str) -> AuthSession | None:
    token_hash = hash_refresh_token(token)
    row = await session.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash))
    if row is not None and row.revoked_at is None:
        row.revoked_at = _now()
    return row


async def revoke_user_sessions(session: AsyncSession, user_id: int) -> int:
    result = await session.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def log_logout(session: AsyncSession, user: User, client: ClientInfo) -> None:
    await _log_event(session, user.personnel_number, "logout", client, user)
