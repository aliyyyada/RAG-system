"""FastAPI-зависимости: кто делает запрос и есть ли у него право.

Пример использования в эндпоинте:

    UsersAdmin = Annotated[Principal, Depends(require_permission("admin.users"))]

    @router.get("/api/admin/users")
    async def list_users(principal: UsersAdmin): ...

Без входа → 401 (фронтенд отправит на страницу входа), вошёл, но права нет → 403.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.rbac import Principal, guest_permissions, principal_for_user
from app.auth.tokens import InvalidToken, decode_access_token
from app.config import Settings, get_settings
from app.db import get_session
from app.models import AuthSession, User

_bearer = HTTPBearer(auto_error=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _unauthorized(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": code, "message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_principal(
    session: SessionDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if credentials is None:
        perms = await guest_permissions(session) if settings.guest_mode_enabled else frozenset()
        return Principal(user=None, permissions=perms)

    try:
        claims = decode_access_token(settings, credentials.credentials)
    except InvalidToken:
        raise _unauthorized("token_invalid", "Сессия истекла, войдите снова") from None

    # Проверяем сессию на каждом запросе: «выйти на всех устройствах» и блокировка
    # администратором действуют сразу, а не через 15 минут, когда истечёт access-токен.
    auth_session = await session.get(AuthSession, claims.session_id)
    now = datetime.now(UTC)
    if (
        auth_session is None
        or auth_session.user_id != claims.user_id
        or auth_session.revoked_at is not None
        or auth_session.expires_at <= now
    ):
        raise _unauthorized("session_closed", "Сессия завершена, войдите снова")
    user = await session.get(User, claims.user_id)
    if user is None or not user.is_active:
        raise _unauthorized("blocked", "Учётная запись заблокирована")
    if user.account_type == "test" and not settings.enable_test_users:
        raise _unauthorized("disabled", "Тестовые учётные записи отключены")
    return await principal_for_user(session, user, auth_session.id)


PrincipalDep = Annotated[Principal, Depends(get_principal)]


async def require_user(principal: PrincipalDep) -> Principal:
    if principal.user is None:
        raise _unauthorized("login_required", "Войдите в систему")
    return principal


def require_permission(*permissions: str) -> Callable[..., Awaitable[Principal]]:
    """Нужно хотя бы одно из перечисленных прав."""

    async def checker(request: Request, principal: PrincipalDep) -> Principal:
        if any(principal.has(p) for p in permissions):
            return principal
        if principal.user is None:
            raise _unauthorized("login_required", "Войдите в систему")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": "Нет доступа к этому разделу"},
        )

    return checker


def client_ip(request: Request) -> str | None:
    # uvicorn запущен с --proxy-headers: request.client — адрес из X-Forwarded-For от nginx.
    return request.client.host if request.client else None
