"""Интерфейс источника документов `DocumentSource`.

Каждая внешняя система (сейчас одна, позже вторая) подключается адаптером, который
умеет: список документов с датами изменения, один документ целиком
(шапка + табличные части + список файлов), скачать файл, список общих документов
и список документов, доступных конкретному человеку (этап 3).
Остальной код (синхронизация, отчёт) работает только через этот интерфейс,
поэтому второй источник — это новый адаптер, а не переделка синхронизации.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol


class SourceError(Exception):
    """Базовая ошибка обращения к источнику."""


class SourceUnavailable(SourceError):
    """Источник не ответил, ответил ошибкой или непонятным ответом. Повторим позже."""


class DocumentNotFound(SourceError):
    """Источник не знает такой документ (удалён физически)."""


class BadSourceData(SourceError):
    """Источник ответил, но данные не годятся (нет обязательных полей и т. п.)."""


@dataclass(frozen=True)
class DocRef:
    """Строка списка документов: id, дата изменения и дата удаления (если удалён)."""

    external_id: str
    modified_at: datetime
    deleted_at: datetime | None = None


@dataclass(frozen=True)
class SourceFileRef:
    file_id: str
    name: str
    size: int | None
    mime: str | None = None


@dataclass
class TablePart:
    """Табличная часть документа, связанная со шапкой по guid документа.

    declared_rows — сколько строк, по словам источника, должно быть. Сравниваем
    с тем, сколько пришло: так ловим обрезанную выгрузку.
    """

    name: str
    rows: list[dict[str, Any]]
    declared_rows: int | None = None


@dataclass
class SourceDocument:
    external_id: str
    modified_at: datetime
    deleted_at: datetime | None
    header: dict[str, Any]
    parts: list[TablePart] = field(default_factory=list)
    files: list[SourceFileRef] = field(default_factory=list)


@dataclass(frozen=True)
class DownloadedFile:
    path: Path
    size: int
    sha256: str


class DocumentSource(Protocol):
    def iter_documents(self, modified_since: datetime | None) -> AsyncIterator[list[DocRef]]:
        """Страницы списка документов, изменённых не раньше modified_since.

        modified_since=None — полный список (для ночной сверки), включая удалённые.
        """
        ...

    async def get_document(self, external_id: str) -> SourceDocument: ...

    async def download_file(self, file_id: str, dest: Path, max_bytes: int) -> DownloadedFile:
        """Скачивает файл потоком во временный файл dest, считает размер и SHA-256."""
        ...

    async def common_ids(self) -> set[str]:
        """Id документов, доступных всем (в том числе без входа)."""
        ...

    async def user_document_ids(self, personnel_number: str) -> set[str]:
        """Id документов, которые источник разрешает человеку с этим табельным номером.

        Человек, которого источник не знает, — пустой список, а не ошибка.
        Источник не ответил — SourceUnavailable: документы источника скрываем (fail-closed).
        """
        ...

    async def aclose(self) -> None: ...
