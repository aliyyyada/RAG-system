"""Вход, обновление сессии, выход, «кто я»."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr

from app.auth import service
from app.auth.deps import (
    PrincipalDep,
    SessionDep,
    SettingsDep,
    client_ip,
    require_user,
)
from app.auth.permissions import ADMIN_AREA
from app.auth.providers import AuthProvider, get_auth_provider
from app.auth.rbac import Principal, principal_for_user
from app.auth.tokens import REFRESH_COOKIE, REFRESH_COOKIE_PATH
from app.config import Settings
from app.models import User
from app.skills.registry import SKILLS

router = APIRouter(prefix="/api/auth", tags=["auth"])

ProviderDep = Annotated[AuthProvider, Depends(get_auth_provider)]


class LoginRequest(BaseModel):
    login: str = Field(min_length=1, max_length=64, description="Табельный номер")
    # SecretStr: пароль не попадёт в repr/логи даже при ошибке валидации.
    password: SecretStr = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    id: int
    personnel_number: str
    full_name: str
    department: str | None
    account_type: str


class SkillOut(BaseModel):
    name: str
    title: str
    available: bool


class MeOut(BaseModel):
    user: UserOut | None
    permissions: list[str]
    roles: list[str]
    is_admin: bool
    can_admin: bool  # показывать ли «Администрирование» в шапке
    guest_mode_enabled: bool
    skills: list[SkillOut]


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 — тип токена, не пароль
    expires_in: int
    me: MeOut


def _me(principal: Principal, settings: Settings) -> MeOut:
    user = principal.user
    return MeOut(
        user=UserOut.model_validate(user, from_attributes=True) if user else None,
        permissions=sorted(principal.permissions),
        roles=list(principal.roles),
        is_admin=principal.is_admin,
        can_admin=bool(principal.permissions & ADMIN_AREA),
        guest_mode_enabled=settings.guest_mode_enabled,
        skills=[
            SkillOut(name=s.name, title=s.title, available=s.available)
            for s in SKILLS
            if principal.has(f"skill.{s.name}.use")
        ],
    )


def _set_refresh_cookie(response: Response, settings: Settings, token: str, expires: datetime):
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        expires=expires,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=True,  # HTTPS везде, включая разработку
        samesite="strict",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        REFRESH_COOKIE, path=REFRESH_COOKIE_PATH, httponly=True, secure=True, samesite="strict"
    )


def _error(err: service.LoginError) -> JSONResponse:
    body: dict[str, object] = {"code": err.code, "message": err.message}
    if err.remaining_attempts is not None:
        body["remaining_attempts"] = err.remaining_attempts
    if err.locked_until is not None:
        body["locked_until"] = err.locked_until.isoformat()
    return JSONResponse({"detail": body}, status_code=err.status)


async def _token_response(
    session: SessionDep, settings: Settings, issued: service.IssuedSession
) -> JSONResponse:
    principal = await principal_for_user(session, issued.user, issued.session_id)
    out = TokenOut(
        access_token=issued.access_token,
        expires_in=settings.access_token_minutes * 60,
        me=_me(principal, settings),
    )
    response = JSONResponse(out.model_dump(mode="json"))
    if issued.refresh_token is not None:
        _set_refresh_cookie(response, settings, issued.refresh_token, issued.expires_at)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post(
    "/login",
    response_model=TokenOut,
    responses={401: {}, 403: {}, 422: {}, 429: {}, 503: {}},
)
async def login(
    body: LoginRequest,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    provider: ProviderDep,
) -> JSONResponse:
    client = service.ClientInfo(ip=client_ip(request), user_agent=request.headers.get("user-agent"))
    try:
        issued = await service.login(
            session, settings, provider, body.login, body.password.get_secret_value(), client
        )
    except service.LoginError as err:
        return _error(err)
    return await _token_response(session, settings, issued)


@router.post("/refresh", response_model=TokenOut, responses={401: {}})
async def refresh(
    session: SessionDep,
    settings: SettingsDep,
    refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> JSONResponse:
    if not refresh_token:
        return _error(service.LoginError("no_session", "Сессии нет"))
    try:
        issued = await service.refresh(session, settings, refresh_token)
    except service.RefreshError:
        response = _error(service.LoginError("session_closed", "Сессия завершена, войдите снова"))
        _clear_refresh_cookie(response)
        return response
    return await _token_response(session, settings, issued)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    session: SessionDep,
    refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> None:
    """Выход на этом устройстве. Работает и с истёкшим access-токеном."""
    if refresh_token:
        row = await service.revoke_session_by_token(session, refresh_token)
        if row is not None:
            user = await session.get(User, row.user_id)
            if user is not None:
                await service.log_logout(
                    session,
                    user,
                    service.ClientInfo(client_ip(request), request.headers.get("user-agent")),
                )
        await session.commit()
    _clear_refresh_cookie(response)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(
    response: Response,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require_user)],
) -> None:
    """Выйти на всех устройствах (отозвать все свои сессии)."""
    assert principal.user is not None
    await service.revoke_user_sessions(session, principal.user.id)
    await session.commit()
    _clear_refresh_cookie(response)


@router.get("/me", response_model=MeOut)
async def me(principal: PrincipalDep, settings: SettingsDep) -> MeOut:
    """Кто я и что мне можно. Без входа — права гостя."""
    return _me(principal, settings)
