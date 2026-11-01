"""Адаптер `http_json`: источник отдаёт данные по HTTPS в JSON.

Настоящий формат функции источника пока неизвестен (docs/open-questions.md, вопрос 7),
поэтому протокол — наше допущение, описан в docs/source-protocol.md. Фейковый сервис
(fakes/fake_service.py) говорит на нём же. Когда придёт настоящий формат, меняется
только этот файл.

    GET  {base}/documents?modified_since=<ISO>&cursor=<...>&limit=500
         → {"items": [{"id", "modified_at", "deleted_at"}], "next_cursor": "..." | null}
    GET  {base}/documents/{id}
         → {"header": {...}, "parts": [{"name", "row_count", "rows": [...]}],
            "files": [{"id", "name", "size", "mime"}]}
    GET  {base}/files/{file_id}            → байты файла
    GET  {base}/access/common              → {"ids": [...]}
    GET  {base}/access/users/{табельный}   → {"ids": [...]}   (404 — человека нет: пусто)
"""

import hashlib
import logging
import ssl
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings
from app.sources.base import (
    BadSourceData,
    DocRef,
    DocumentNotFound,
    DownloadedFile,
    SourceDocument,
    SourceFileRef,
    SourceUnavailable,
    TablePart,
)

log = logging.getLogger(__name__)

PAGE_SIZE = 500


def _dt(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise BadSourceData(f"Поле {field}: ожидалась дата, пришло {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise BadSourceData(f"Поле {field}: не дата {value!r}") from None
    if parsed.tzinfo is None:
        raise BadSourceData(f"Поле {field}: дата без часового пояса {value!r}")
    return parsed


def _opt_dt(value: Any, field: str) -> datetime | None:
    return None if value in (None, "") else _dt(value, field)


class HttpJsonSource:
    def __init__(
        self,
        base_url: str,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        verify: ssl.SSLContext | bool = True
        if transport is None:
            ctx = ssl.create_default_context(
                cafile=settings.source_ca_file or settings.external_auth_ca_file
            )
            if settings.source_client_cert:
                ctx.load_cert_chain(settings.source_client_cert, settings.source_client_key)
            verify = ctx
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=settings.source_timeout_seconds,
            verify=verify,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        try:
            resp = await self._client.get(url, params=params)
        except (httpx.HTTPError, ssl.SSLError) as exc:
            raise SourceUnavailable(f"Источник не отвечает: {type(exc).__name__}") from exc
        if resp.status_code == 404:
            raise DocumentNotFound(url)
        if resp.status_code != 200:
            raise SourceUnavailable(f"Источник ответил HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError:
            raise SourceUnavailable("Источник вернул не JSON") from None

    async def iter_documents(self, modified_since: datetime | None) -> AsyncIterator[list[DocRef]]:
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": PAGE_SIZE}
            if modified_since is not None:
                params["modified_since"] = modified_since.isoformat()
            if cursor:
                params["cursor"] = cursor
            data = await self._get_json("/documents", params)
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise SourceUnavailable("Список документов: неожиданный формат ответа")
            page = [
                DocRef(
                    external_id=str(item["id"]),
                    modified_at=_dt(item.get("modified_at"), "modified_at"),
                    deleted_at=_opt_dt(item.get("deleted_at"), "deleted_at"),
                )
                for item in data["items"]
            ]
            yield page
            cursor = data.get("next_cursor")
            if not cursor:
                return

    async def get_document(self, external_id: str) -> SourceDocument:
        data = await self._get_json(f"/documents/{quote(external_id, safe='')}")
        if not isinstance(data, dict) or not isinstance(data.get("header"), dict):
            raise BadSourceData("Документ: нет шапки (header)")
        header: dict[str, Any] = data["header"]
        parts = []
        for p in data.get("parts") or []:
            rows = p.get("rows")
            if not isinstance(rows, list):
                raise BadSourceData(f"Табличная часть {p.get('name')!r}: нет строк")
            declared = p.get("row_count")
            parts.append(
                TablePart(
                    name=str(p.get("name") or "Табличная часть"),
                    rows=rows,
                    declared_rows=int(declared) if declared is not None else None,
                )
            )
        files = [
            SourceFileRef(
                file_id=str(f["id"]),
                name=str(f.get("name") or f["id"]),
                size=int(f["size"]) if f.get("size") is not None else None,
                mime=f.get("mime"),
            )
            for f in data.get("files") or []
        ]
        return SourceDocument(
            external_id=str(header.get("id") or external_id),
            modified_at=_dt(header.get("modified_at"), "modified_at"),
            deleted_at=_opt_dt(header.get("deleted_at"), "deleted_at"),
            header=header,
            parts=parts,
            files=files,
        )

    async def download_file(self, file_id: str, dest: Path, max_bytes: int) -> DownloadedFile:
        digest = hashlib.sha256()
        size = 0
        try:
            async with self._client.stream("GET", f"/files/{quote(file_id, safe='')}") as resp:
                if resp.status_code == 404:
                    raise DocumentNotFound(f"Файл {file_id}")
                if resp.status_code != 200:
                    raise SourceUnavailable(f"Файл {file_id}: источник ответил {resp.status_code}")
                with dest.open("wb") as out:
                    async for chunk in resp.aiter_bytes(1 << 16):
                        size += len(chunk)
                        if size > max_bytes:
                            raise BadSourceData(
                                f"Файл {file_id} больше {max_bytes // (1 << 20)} МБ"
                            )
                        digest.update(chunk)
                        out.write(chunk)
        except (httpx.HTTPError, ssl.SSLError) as exc:
            raise SourceUnavailable(f"Файл {file_id}: {type(exc).__name__}") from exc
        return DownloadedFile(path=dest, size=size, sha256=digest.hexdigest())

    async def common_ids(self) -> set[str]:
        data = await self._get_json("/access/common")
        if not isinstance(data, dict) or not isinstance(data.get("ids"), list):
            raise SourceUnavailable("Список общих документов: неожиданный формат ответа")
        return {str(x) for x in data["ids"]}

    async def user_document_ids(self, personnel_number: str) -> set[str]:
        try:
            data = await self._get_json(f"/access/users/{quote(personnel_number, safe='')}")
        except DocumentNotFound:
            return set()  # источник не знает этого человека — прав у него нет
        if not isinstance(data, dict) or not isinstance(data.get("ids"), list):
            raise SourceUnavailable("Права пользователя: неожиданный формат ответа")
        return {str(x) for x in data["ids"]}
