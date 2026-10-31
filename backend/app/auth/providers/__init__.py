"""Выбор адаптера AuthProvider по настройке AUTH_PROVIDER."""

from functools import lru_cache

from app.auth.providers.base import AuthOutcome, AuthProvider, AuthResult, ExternalProfile
from app.auth.providers.external_check import ExternalCheckProvider
from app.config import get_settings

__all__ = [
    "AuthOutcome",
    "AuthProvider",
    "AuthResult",
    "ExternalProfile",
    "get_auth_provider",
]


@lru_cache
def get_auth_provider() -> AuthProvider:
    settings = get_settings()
    # Настройки уже проверены в Settings.validate_prod: другие значения пока запрещены.
    if settings.auth_provider == "external_check":
        return ExternalCheckProvider(settings)
    raise RuntimeError(f"AuthProvider {settings.auth_provider} не реализован")
