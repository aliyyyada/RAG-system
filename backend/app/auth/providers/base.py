"""Интерфейс AuthProvider: как проверяется пароль внешнего пользователя.

Сейчас (решено): внешняя функция проверки (`ExternalCheckProvider`).
Позже (план): единый вход через Ory Hydra (OIDC). Тогда пароль к нам не придёт вовсе,
а вместо `authenticate(login, password)` будет обмен кода авторизации на токен —
это добавится как отдельный метод адаптера, остальная система (сессии, RBAC) не меняется.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class AuthOutcome(StrEnum):
    OK = "ok"
    INVALID = "invalid"  # неверный логин или пароль
    UNAVAILABLE = "unavailable"  # сервис проверки не ответил — это не ошибка пользователя


@dataclass(frozen=True)
class ExternalProfile:
    """Что внешний сервис сообщил о пользователе (если сообщил)."""

    full_name: str | None = None
    department: str | None = None


@dataclass(frozen=True)
class AuthResult:
    outcome: AuthOutcome
    profile: ExternalProfile | None = None


class AuthProvider(Protocol):
    name: str

    async def authenticate(self, login: str, password: str) -> AuthResult:
        """Проверить логин и пароль. Пароль нельзя логировать и сохранять."""
        ...
