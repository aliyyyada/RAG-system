"""Токены нашей сессии.

- access-токен — JWT (HS256) на 15 минут. Фронтенд держит его только в памяти вкладки
  и отправляет в заголовке `Authorization: Bearer …`. В localStorage не кладём:
  оттуда его может прочитать любой внедрённый скрипт.
- refresh-токен — случайная строка в cookie `HttpOnly; Secure; SameSite=Strict`.
  JavaScript её не видит, на чужие сайты браузер её не отправит. В БД лежит только SHA-256.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from app.config import Settings

ALGORITHM = "HS256"
REFRESH_COOKIE = "rag_refresh"
# Cookie уходит только на эндпоинты /api/auth/* — остальные запросы её не несут.
REFRESH_COOKIE_PATH = "/api/auth"


@dataclass(frozen=True)
class AccessClaims:
    user_id: int
    session_id: uuid.UUID


class InvalidToken(Exception):
    pass


def create_access_token(settings: Settings, user_id: int, session_id: uuid.UUID) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "sid": str(session_id),
        "typ": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=ALGORITHM)


def decode_access_token(settings: Settings, token: str) -> AccessClaims:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[ALGORITHM],
            options={"require": ["exp", "sub", "sid"]},
        )
        if payload.get("typ") != "access":
            raise InvalidToken("wrong token type")
        return AccessClaims(user_id=int(payload["sub"]), session_id=uuid.UUID(payload["sid"]))
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise InvalidToken(str(exc)) from exc


def new_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
