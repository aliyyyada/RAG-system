"""Адаптер «внешняя функция проверки» (вариант 4 из ТЗ).

Отправляем логин и пароль по HTTPS (при необходимости с клиентским сертификатом, mTLS)
и получаем ответ «да» или «нет». Формат — допущение (docs/open-questions.md, вопрос 6):

    POST <EXTERNAL_AUTH_URL>
    {"login": "10234", "password": "…"}
    → 200 {"ok": true, "full_name": "Иванова Анна Сергеевна", "department": "Служба ОТ и ПБ"}
    → 200 {"ok": false}

Любая другая ситуация (таймаут, 5xx, не-JSON) — «сервис недоступен»: вход не выполняется,
но и попытка не считается неудачной (пользователь не виноват).
"""

import logging
import ssl

import httpx

from app.auth.providers.base import AuthOutcome, AuthResult, ExternalProfile
from app.config import Settings

log = logging.getLogger(__name__)


class ExternalCheckProvider:
    name = "external_check"

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self._url = settings.external_auth_url
        self._timeout = settings.external_auth_timeout_seconds
        self._transport = transport
        self._verify: ssl.SSLContext | bool = True
        if transport is None:
            ctx = ssl.create_default_context(cafile=settings.external_auth_ca_file)
            if settings.external_auth_client_cert:
                ctx.load_cert_chain(
                    settings.external_auth_client_cert, settings.external_auth_client_key
                )
            self._verify = ctx

    async def authenticate(self, login: str, password: str) -> AuthResult:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, verify=self._verify, transport=self._transport
            ) as client:
                resp = await client.post(self._url, json={"login": login, "password": password})
            if resp.status_code != 200:
                # Тело ответа не логируем: вдруг сервис эхом вернул запрос с паролем.
                log.warning("Сервис проверки пароля ответил HTTP %s", resp.status_code)
                return AuthResult(AuthOutcome.UNAVAILABLE)
            data = resp.json()
        except (httpx.HTTPError, ValueError, ssl.SSLError) as exc:
            log.warning("Сервис проверки пароля недоступен: %s", type(exc).__name__)
            return AuthResult(AuthOutcome.UNAVAILABLE)

        if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
            log.warning("Сервис проверки пароля вернул неожиданный ответ")
            return AuthResult(AuthOutcome.UNAVAILABLE)
        if not data["ok"]:
            return AuthResult(AuthOutcome.INVALID)
        profile = ExternalProfile(
            full_name=_str_or_none(data.get("full_name")),
            department=_str_or_none(data.get("department")),
        )
        return AuthResult(AuthOutcome.OK, profile)


def _str_or_none(value: object) -> str | None:
    return value.strip()[:255] or None if isinstance(value, str) else None
