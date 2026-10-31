"""Источники документов: интерфейс DocumentSource и адаптеры.

    adapter = open_source(source_row, settings)
    try:
        async for page in adapter.iter_documents(modified_since=None): ...
    finally:
        await adapter.aclose()

В тестах вместо сети подставляется фейковый сервис в памяти:
`set_transport_override(ASGITransport(app=fake_app))`.
"""

import httpx

from app.config import Settings
from app.models import Source
from app.sources.base import DocumentSource
from app.sources.http_json import HttpJsonSource

ADAPTERS = ("http_json",)

_transport_override: httpx.AsyncBaseTransport | None = None


def set_transport_override(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport_override
    _transport_override = transport


def open_source(source: Source, settings: Settings) -> DocumentSource:
    if source.adapter == "http_json":
        return HttpJsonSource(source.base_url, settings, transport=_transport_override)
    raise ValueError(f"Неизвестный адаптер источника: {source.adapter}")
