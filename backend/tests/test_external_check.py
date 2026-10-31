"""Адаптер «внешняя функция проверки»: разбор ответов внешнего сервиса (без БД)."""

import json

import httpx
import pytest

from app.auth.providers import AuthOutcome
from app.auth.providers.external_check import ExternalCheckProvider
from app.config import Settings


def _provider(handler) -> ExternalCheckProvider:  # type: ignore[no-untyped-def]
    return ExternalCheckProvider(Settings(), transport=httpx.MockTransport(handler))


async def test_ok_with_profile() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "full_name": " Иванова А. С. "})

    result = await _provider(handler).authenticate("10234", "pw")
    assert result.outcome is AuthOutcome.OK
    assert result.profile is not None and result.profile.full_name == "Иванова А. С."
    assert seen == {"login": "10234", "password": "pw"}


async def test_rejected() -> None:
    result = await _provider(lambda r: httpx.Response(200, json={"ok": False})).authenticate(
        "1", "2"
    )
    assert result.outcome is AuthOutcome.INVALID


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"ok": True}),
        httpx.Response(200, text="<html>proxy error</html>"),
        httpx.Response(200, json={"ok": "yes"}),
        httpx.Response(200, json=[1, 2]),
    ],
)
async def test_garbage_means_unavailable(response: httpx.Response) -> None:
    result = await _provider(lambda r: response).authenticate("1", "2")
    assert result.outcome is AuthOutcome.UNAVAILABLE


async def test_timeout_means_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    assert (await _provider(handler).authenticate("1", "2")).outcome is AuthOutcome.UNAVAILABLE


async def test_fake_service_contract() -> None:
    """Фейковый сервис отвечает в том же формате, что ждёт адаптер."""
    from fakes.fake_service import FAKE_PASSWORD
    from fakes.fake_service import app as fake_app

    provider = ExternalCheckProvider(Settings(), transport=httpx.ASGITransport(app=fake_app))
    ok = await provider.authenticate("00017", FAKE_PASSWORD)
    assert ok.outcome is AuthOutcome.OK
    assert ok.profile is not None and ok.profile.department == "Отдел ИТ"
    assert (await provider.authenticate("00017", "nope")).outcome is AuthOutcome.INVALID
    assert (await provider.authenticate("nobody", FAKE_PASSWORD)).outcome is AuthOutcome.INVALID
