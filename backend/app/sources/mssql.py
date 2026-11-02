"""Адаптер `mssql`: источник — функция (хранимая процедура) в Microsoft SQL Server.

Как устроен источник (по скриншоту от разработчиков источника, 02.11.2026). Вызов
функции возвращает две таблицы:

1. **Метаданные документов**: ID_GUID, ID_Вид, ID_Key, Номер, Дата, Тип, Тема, Исполнитель.
2. **Бинарники**: ID_GUID, BinaryFile и колонка с типом файла. Строки связаны с первой
   таблицей по ID_GUID.

Даты изменения и удаления у источника нет (вопрос 31 в docs/open-questions.md), а все
бинарники приходят одним вызовом. Поэтому адаптер работает со **снимком**:

    refresh():  вызываем функцию → первую таблицу держим в памяти, бинарники по одному
                пишем в хранилище файлов (имя = SHA-256) → для каждого документа считаем
                «отпечаток» = SHA-256(шапка + хэши файлов) → пишем в source_items.
                Отпечаток изменился — версия документа = момент, когда мы это увидели.
                Документа нет в полном снимке — он удалён в источнике.
    iter_documents / get_document / download_file — читают source_items и хранилище,
                функцию на каждый документ не вызывают.

Синхронизация (app/ingest/sync.py) и обработка документа (pipeline.py) работают через
интерфейс DocumentSource и не знают, что за источником стоит MS SQL.

Пример. Функция вернула приказ «537к» с PDF. Через сутки — тот же приказ, но с другим PDF:
отпечаток другой → версия = время этой синхронизации → документ встаёт в очередь на
переиндексацию. Приказ пропал из ответа → при ночной сверке он помечается удалённым.

Всё, что ещё неизвестно (имя функции, параметры, имена колонок, как спрашивать права),
задаётся в настройках источника (`sources.config`, см. MssqlConfig). Пароль — только в .env.
Драйвер — python-tds (MIT, чистый Python, без ODBC-драйвера Microsoft), шифрование
через pyOpenSSL (Apache-2.0).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from app.config import Settings
from app.db import get_sessionmaker
from app.ingest.files import EXTENSIONS, MIME, sniff
from app.ingest.storage import FileStore
from app.models import Source, SourceItem
from app.sources.base import (
    BadSourceData,
    DocRef,
    DocumentNotFound,
    DownloadedFile,
    SourceDocument,
    SourceFileRef,
    SourceUnavailable,
)

log = logging.getLogger(__name__)

PAGE_SIZE = 500
# Подстановки в тексте вызова: :user — табельный номер (или служебная учётка при синхронизации),
# :since — «изменено после» (если функция это умеет).
_PLACEHOLDER = re.compile(r"(?<![:\w]):(user|since)\b")
_SQL_START = re.compile(r"^\s*(EXEC|EXECUTE|SELECT)\b", re.IGNORECASE)

DEFAULT_HEADER_MAP = {
    "Номер": "number",
    "Дата": "date",
    "Тип": "doc_type",
    "Тема": "title",
    "Исполнитель": "executor",
}
DEFAULT_HIDDEN = ["ID_Вид", "ID_Key"]
# Как может называться колонка с типом файла, пока точное имя неизвестно.
FILE_TYPE_GUESSES = ("FileType", "Тип_файла", "ТипФайла", "Тип файла", "Расширение", "Ext")

# Значение из колонки типа → наш тип файла.
_TYPE_WORDS = {
    "pdf": "pdf",
    "application/pdf": "pdf",
    "docx": "docx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "doc": "doc",
    "application/msword": "doc",
    "tif": "tiff",
    "tiff": "tiff",
    "image/tiff": "tiff",
    "png": "png",
    "image/png": "png",
    "jpg": "jpeg",
    "jpeg": "jpeg",
    "image/jpeg": "jpeg",
}


class MssqlConfig(BaseModel):
    """Настройки адаптера. Хранятся в sources.config, меняются в админке."""

    # Вызов функции. Можно использовать :since и :user (см. выше).
    call_sql: str = Field(default="EXEC dbo.GetDocuments", min_length=5, max_length=2000)
    # Значение :user при синхронизации (если функции нужен пользователь).
    service_user: str | None = Field(default=None, max_length=100)
    id_column: str = "ID_GUID"
    # Колонка источника → наше поле шапки. Неизвестные колонки попадают в текст документа
    # с исходным именем («Исполнитель: …»), скрытые — нет.
    header_map: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_HEADER_MAP))
    hidden_columns: list[str] = Field(default_factory=lambda: list(DEFAULT_HIDDEN))
    modified_column: str | None = None
    deleted_column: str | None = None
    binary_column: str = "BinaryFile"
    # None — угадываем по FILE_TYPE_GUESSES или по единственной лишней колонке.
    file_type_column: str | None = None
    file_name_column: str | None = None
    # Права на документы источника:
    #   all_authenticated — любой вошедший видит все документы источника (допущение, вопрос 32);
    #   sql — спрашиваем access_sql с :user = табельный номер, ответ — список ID_GUID.
    access_mode: Literal["all_authenticated", "sql"] = "all_authenticated"
    access_sql: str | None = Field(default=None, max_length=2000)
    # Список общих документов (доступны всем вошедшим). Пусто — общих нет.
    common_sql: str | None = Field(default=None, max_length=2000)
    # Пока функция не умеет :since, полный снимок берём не чаще, чем раз в N минут
    # (ночная сверка — всегда).
    full_refresh_minutes: int = Field(default=360, ge=0, le=7 * 24 * 60)
    encrypt: bool = True
    # Имена переменных .env: {credentials_env}_USER и {credentials_env}_PASSWORD.
    credentials_env: str = Field(default="SOURCE_MSSQL", pattern=r"^[A-Z][A-Z0-9_]{1,60}$")
    login_timeout_seconds: int = Field(default=15, ge=1, le=300)
    # fake — встроенный фейковый источник (только разработка, ENABLE_TEST_USERS=true).
    driver: Literal["tds", "fake"] = "tds"

    @field_validator("call_sql", "access_sql", "common_sql")
    @classmethod
    def _one_statement(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not _SQL_START.match(v):
            raise ValueError("Вызов должен начинаться с EXEC или SELECT")
        if ";" in v:
            raise ValueError("Только один вызов, без «;»")
        return v

    @model_validator(mode="after")
    def _access_sql_needed(self) -> MssqlConfig:
        if self.access_mode == "sql" and not self.access_sql:
            raise ValueError("Для режима прав «sql» нужен запрос прав (access_sql)")
        return self


def parse_config(raw: dict[str, Any] | None) -> MssqlConfig:
    try:
        return MssqlConfig.model_validate(raw or {})
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(x) for x in first["loc"])
        msg = str(first["msg"]).removeprefix("Value error, ")
        prefix = f"Настройки источника, поле {where}" if where else "Настройки источника"
        raise BadSourceData(f"{prefix}: {msg}") from None


@dataclass(frozen=True)
class ServerAddress:
    host: str
    port: int
    database: str


def parse_address(url: str) -> ServerAddress:
    """mssql://sql.zavod.local:1433/Docflow → сервер, порт, база."""
    parts = urlsplit(url)
    if parts.scheme != "mssql" or not parts.hostname:
        raise BadSourceData("Адрес источника должен быть вида mssql://сервер:1433/База")
    database = unquote(parts.path.lstrip("/"))
    if not database:
        raise BadSourceData("В адресе источника не указана база данных: mssql://сервер:1433/База")
    return ServerAddress(parts.hostname, parts.port or 1433, database)


# ---------------------------------------------------------------------------
# Выполнение SQL: настоящий драйвер или подмена в тестах
# ---------------------------------------------------------------------------


@dataclass
class ResultSet:
    columns: list[str]
    rows: Iterable[tuple[Any, ...]]


class SqlRunner(Protocol):
    """Выполняет один вызов и отдаёт таблицы по очереди. Вызывается в отдельном потоке.

    Строки таблицы читаются лениво: следующую таблицу можно брать, только дочитав текущую
    (так бинарники не копятся в памяти).
    """

    def execute(self, sql: str, params: dict[str, Any]) -> Iterator[ResultSet]: ...


RunnerFactory = Callable[[Source, MssqlConfig, Settings], SqlRunner]
_runner_override: RunnerFactory | None = None


def set_runner_override(factory: RunnerFactory | None) -> None:
    """Тесты подставляют фейковые таблицы вместо SQL Server."""
    global _runner_override
    _runner_override = factory


def to_pyformat(sql: str) -> str:
    """:user → %(user)s (стиль параметров python-tds). Знак % в тексте экранируем."""
    return _PLACEHOLDER.sub(r"%(\1)s", sql.replace("%", "%%"))


class TdsRunner:
    """Подключение к SQL Server через python-tds."""

    def __init__(self, source: Source, config: MssqlConfig, settings: Settings):
        self.address = parse_address(source.base_url)
        self.config = config
        self.settings = settings
        prefix = config.credentials_env
        if prefix == "SOURCE_MSSQL":  # из Settings: читается и из .env без Docker
            self.user = os.environ.get("SOURCE_MSSQL_USER", settings.source_mssql_user)
            self.password = os.environ.get(
                "SOURCE_MSSQL_PASSWORD", settings.source_mssql_password.get_secret_value()
            )
        else:  # второй SQL-источник со своими переменными
            self.user = os.environ.get(f"{prefix}_USER", "")
            self.password = os.environ.get(f"{prefix}_PASSWORD", "")
        if not self.user:
            raise SourceUnavailable(f"Не задан логин SQL Server: {prefix}_USER в .env")

    def execute(self, sql: str, params: dict[str, Any]) -> Iterator[ResultSet]:
        import pytds  # импорт здесь: драйвер нужен только источнику MS SQL

        cafile = None
        if self.config.encrypt:
            cafile = self.settings.source_ca_file or self.settings.external_auth_ca_file
            if not cafile:
                raise SourceUnavailable(
                    "Шифрование включено, но не задан сертификат CA: SOURCE_CA_FILE в .env"
                )
        try:
            conn = pytds.connect(
                dsn=self.address.host,
                port=self.address.port,
                database=self.address.database,
                user=self.user,
                password=self.password,
                login_timeout=self.config.login_timeout_seconds,
                timeout=self.settings.source_timeout_seconds,
                cafile=cafile,
                validate_host=True,
                autocommit=True,
                readonly=True,
                appname="rag-system",
                bytes_to_unicode=True,
                disable_connect_retry=True,
            )
        except Exception as exc:  # noqa: BLE001 — сеть, TLS, неверный пароль: всё «не отвечает»
            # Текст ошибки драйвера может содержать имя пользователя, но не пароль.
            raise SourceUnavailable(f"SQL Server не отвечает: {type(exc).__name__}: {exc}") from exc
        try:
            cur = conn.cursor()
            try:
                cur.execute(to_pyformat(sql), {k: v for k, v in params.items() if v is not None})
            except Exception as exc:  # noqa: BLE001
                raise SourceUnavailable(f"Вызов функции источника: {exc}"[:500]) from exc
            while True:
                if cur.description:
                    columns = [d[0] for d in cur.description]
                    yield ResultSet(columns, iter(cur.fetchone, None))
                if not cur.nextset():
                    break
        finally:
            conn.close()


def _runner(source: Source, config: MssqlConfig, settings: Settings) -> SqlRunner:
    if _runner_override is not None:
        return _runner_override(source, config, settings)
    if config.driver == "fake":
        if not settings.enable_test_users:
            raise SourceUnavailable("Фейковый источник выключен (ENABLE_TEST_USERS=false)")
        from fakes.fake_mssql import FAKE_MSSQL

        return FAKE_MSSQL
    return TdsRunner(source, config, settings)


# ---------------------------------------------------------------------------
# Разбор таблиц
# ---------------------------------------------------------------------------


def norm_id(value: Any) -> str:
    """GUID в одном виде везде: SQL Server сравнивает их без учёта регистра, мы — строки."""
    return str(value).strip().upper()


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value.strip() if isinstance(value, str) else value
    if isinstance(value, datetime):
        if value.time() == time(0) and value.tzinfo is None:
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value).upper()
    if isinstance(value, bytes | bytearray | memoryview):
        return None  # бинарное поле в шапке в текст не идёт
    return str(value)


def _find(columns: list[str], name: str | None) -> int | None:
    if not name:
        return None
    lower = [c.lower() for c in columns]
    try:
        return lower.index(name.lower())
    except ValueError:
        return None


def _as_datetime(value: Any, tz: ZoneInfo, column: str) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.strip())
        except ValueError:
            raise BadSourceData(f"Колонка {column}: не дата {value!r}") from None
    else:
        raise BadSourceData(f"Колонка {column}: не дата {value!r}")
    # SQL Server datetime без часового пояса — это местное время завода.
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt


def file_kind(declared: Any, head: bytes) -> str | None:
    """Тип файла: по первым байтам, а если не узнали — по колонке типа от источника."""
    by_content = sniff(head)
    if by_content in ("pdf", "tiff", "png", "jpeg"):
        return by_content
    if by_content == "ole":
        return "doc"
    word = str(declared or "").strip().lower().lstrip(".")
    if by_content == "zip":
        return "docx" if word in ("", "docx") or "word" in word else word or None
    return _TYPE_WORDS.get(word, word or None)


def _file_name(
    header: dict[str, Any],
    external_id: str,
    n: int,
    total: int,
    kind: str | None,
    given: str | None,
) -> str:
    if given:
        return given[:250]
    base = str(header.get("number") or external_id[:8])
    base = re.sub(r'[\\/:*?"<>|\s]+', "_", base).strip("_") or "документ"
    if header.get("doc_type"):
        base = f"{str(header['doc_type']).lower()}_{base}"
    suffix = f"_{n + 1}" if total > 1 else ""
    ext = EXTENSIONS.get(kind or "", kind or "bin")
    return f"{base}{suffix}.{ext}"


@dataclass
class _Doc:
    header: dict[str, Any]
    modified: datetime | None
    deleted: datetime | None
    files: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Snapshot:
    docs: dict[str, _Doc]
    orphan_files: int = 0
    files_total: int = 0


class SnapshotReader:
    """Превращает таблицы функции в документы. Бинарники сразу уходят в хранилище."""

    def __init__(self, config: MssqlConfig, settings: Settings):
        self.config = config
        self.store = FileStore(settings)
        self.max_bytes = settings.source_file_max_mb * 1024 * 1024
        self.tz = ZoneInfo(settings.app_timezone)

    def read(self, result_sets: Iterator[ResultSet]) -> Snapshot:
        snap = Snapshot(docs={})
        meta_seen = files_seen = False
        pending_files: list[tuple[str, dict[str, Any]]] = []
        for rs in result_sets:
            if _find(rs.columns, self.config.binary_column) is not None:
                files_seen = True
                for ext_id, entry in self._files(rs):
                    snap.files_total += 1
                    pending_files.append((ext_id, entry))
            elif _find(rs.columns, self.config.id_column) is not None and not meta_seen:
                meta_seen = True
                self._meta(rs, snap)
            else:
                for _ in rs.rows:  # лишняя таблица: дочитываем и пропускаем
                    pass
        if not meta_seen:
            raise SourceUnavailable(
                f"Функция источника не вернула таблицу с колонкой {self.config.id_column}"
            )
        if not files_seen:
            log.warning(
                "Функция источника не вернула таблицу с колонкой %s", self.config.binary_column
            )
        for ext_id, entry in pending_files:
            doc = snap.docs.get(ext_id)
            if doc is None:
                snap.orphan_files += 1
                continue
            doc.files.append(entry)
        for ext_id, doc in snap.docs.items():
            total = len(doc.files)
            for n, f in enumerate(doc.files):
                f["name"] = _file_name(
                    doc.header, ext_id, n, total, f.get("kind"), f.pop("given_name", None)
                )
        if snap.orphan_files:
            log.warning("Источник: %s файлов без документа в первой таблице", snap.orphan_files)
        return snap

    def _meta(self, rs: ResultSet, snap: Snapshot) -> None:
        cols = rs.columns
        cfg = self.config
        id_i = _find(cols, cfg.id_column)
        assert id_i is not None
        mod_i = _find(cols, cfg.modified_column)
        del_i = _find(cols, cfg.deleted_column)
        hidden = {h.lower() for h in cfg.hidden_columns}
        mapping = {k.lower(): v for k, v in cfg.header_map.items()}
        skip = {i for i in (id_i, mod_i, del_i) if i is not None}
        for row in rs.rows:
            if row[id_i] in (None, ""):
                raise BadSourceData(f"Строка без {cfg.id_column} в таблице документов")
            ext_id = norm_id(row[id_i])
            header: dict[str, Any] = {"id": ext_id}
            for i, col in enumerate(cols):
                if i in skip:
                    continue
                value = _json_value(row[i])
                if value in (None, ""):
                    continue
                low = col.lower()
                key = mapping.get(low, col)
                if low in hidden:
                    header[f"_{col}"] = value  # «_» — техническое поле, в текст не идёт
                elif key == "doc_type" and isinstance(value, str) and value.isupper():
                    header[key] = value.capitalize()  # «ПРИКАЗ» → «Приказ» в названии
                else:
                    header[key] = value
            snap.docs[ext_id] = _Doc(
                header=header,
                modified=_as_datetime(row[mod_i], self.tz, cols[mod_i])
                if mod_i is not None
                else None,
                deleted=_as_datetime(row[del_i], self.tz, cols[del_i])
                if del_i is not None
                else None,
            )

    def _type_column(self, cols: list[str], used: set[int]) -> int | None:
        cfg = self.config
        if cfg.file_type_column:
            return _find(cols, cfg.file_type_column)
        for guess in FILE_TYPE_GUESSES:
            i = _find(cols, guess)
            if i is not None:
                return i
        rest = [i for i in range(len(cols)) if i not in used]
        return rest[0] if len(rest) == 1 else None

    def _files(self, rs: ResultSet) -> Iterator[tuple[str, dict[str, Any]]]:
        cols = rs.columns
        cfg = self.config
        id_i = _find(cols, cfg.id_column)
        bin_i = _find(cols, cfg.binary_column)
        if id_i is None or bin_i is None:
            raise SourceUnavailable(f"В таблице файлов нет колонки {cfg.id_column}")
        name_i = _find(cols, cfg.file_name_column)
        used = {id_i, bin_i} | ({name_i} if name_i is not None else set())
        type_i = self._type_column(cols, used)
        for row in rs.rows:
            if row[id_i] in (None, ""):
                continue
            ext_id = norm_id(row[id_i])
            declared = row[type_i] if type_i is not None else None
            given = str(row[name_i]).strip() if name_i is not None and row[name_i] else None
            data = row[bin_i]
            entry: dict[str, Any] = {
                "declared_type": _json_value(declared),
                "given_name": given,
                "kind": file_kind(declared, b""),  # для имени файла, если файла нет
            }
            if data is None or len(data) == 0:
                entry |= {"sha256": None, "size": 0, "error": "источник отдал пустой файл"}
            elif len(data) > self.max_bytes:
                entry |= {
                    "sha256": None,
                    "size": len(data),
                    "error": f"файл больше {self.max_bytes // (1 << 20)} МБ",
                }
            else:
                raw = bytes(data)
                sha, _ = self.store.save_bytes(raw)
                kind = file_kind(declared, raw[:8])
                entry |= {
                    "sha256": sha,
                    "size": len(raw),
                    "kind": kind,
                    "mime": MIME.get(kind or ""),
                }
            yield ext_id, entry


def fingerprint(header: dict[str, Any], files: list[dict[str, Any]]) -> str:
    body = json.dumps(
        {"h": header, "f": [(f.get("sha256"), f.get("size"), f.get("error")) for f in files]},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(body.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Адаптер
# ---------------------------------------------------------------------------


class MssqlSource:
    def __init__(self, source: Source, settings: Settings):
        self.source_id = source.id
        self.code = source.code
        self.settings = settings
        self.config = parse_config(source.config)
        self._source = source
        self._refreshed = False

    async def aclose(self) -> None:
        return None

    def _runner(self) -> SqlRunner:
        return _runner(self._source, self.config, self.settings)

    async def _query_ids(self, sql: str, params: dict[str, Any]) -> set[str]:
        def run() -> set[str]:
            ids: set[str] = set()
            for rs in self._runner().execute(sql, params):
                i = _find(rs.columns, self.config.id_column)
                for row in rs.rows:
                    value = row[i if i is not None else 0]
                    if value not in (None, ""):
                        ids.add(norm_id(value))
            return ids

        return await asyncio.to_thread(run)

    # -- снимок ---------------------------------------------------------------

    async def _last_refresh(self) -> datetime | None:
        async with get_sessionmaker()() as s:
            return await s.scalar(
                select(func.max(SourceItem.seen_at)).where(SourceItem.source_id == self.source_id)
            )

    async def refresh(self, since: datetime | None) -> dict[str, int]:
        """Вызывает функцию источника и обновляет source_items. Возвращает счётчики."""
        partial = since is not None and ":since" in self.config.call_sql
        params: dict[str, Any] = {"user": self.config.service_user}
        if partial:
            params["since"] = since.astimezone(ZoneInfo(self.settings.app_timezone)).replace(
                tzinfo=None
            )
        reader = SnapshotReader(self.config, self.settings)

        def run() -> Snapshot:
            return reader.read(self._runner().execute(self.config.call_sql, params))

        snap = await asyncio.to_thread(run)
        return await self._save(snap, full=not partial)

    async def _save(self, snap: Snapshot, *, full: bool) -> dict[str, int]:
        now = datetime.now(UTC)
        stats = {
            "documents": len(snap.docs),
            "files": snap.files_total,
            "orphan_files": snap.orphan_files,
            "changed": 0,
            "gone": 0,
        }
        async with get_sessionmaker()() as s, s.begin():
            old = {
                r.external_id: (r.fingerprint, r.version, r.deleted_at)
                for r in (
                    await s.execute(
                        select(
                            SourceItem.external_id,
                            SourceItem.fingerprint,
                            SourceItem.version,
                            SourceItem.deleted_at,
                        ).where(SourceItem.source_id == self.source_id)
                    )
                ).all()
            }
            rows = []
            for ext_id, doc in snap.docs.items():
                fp = fingerprint(doc.header, doc.files)
                prev = old.get(ext_id)
                if doc.modified is not None:
                    version = doc.modified
                elif prev is not None and prev[0] == fp:
                    version = prev[1]
                else:
                    version = now
                if prev is None or prev[0] != fp:
                    stats["changed"] += 1
                rows.append(
                    {
                        "source_id": self.source_id,
                        "external_id": ext_id,
                        "fingerprint": fp,
                        "version": version,
                        "deleted_at": doc.deleted,
                        "header": doc.header,
                        "files": doc.files,
                        "seen_at": now,
                    }
                )
            for i in range(0, len(rows), PAGE_SIZE):
                stmt = insert(SourceItem).values(rows[i : i + PAGE_SIZE])
                await s.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["source_id", "external_id"],
                        set_={
                            c: stmt.excluded[c]
                            for c in (
                                "fingerprint",
                                "version",
                                "deleted_at",
                                "header",
                                "files",
                                "seen_at",
                            )
                        },
                    )
                )
            if full:
                gone = [k for k, v in old.items() if k not in snap.docs and v[2] is None]
                stats["gone"] = len(gone)
                for i in range(0, len(gone), PAGE_SIZE):
                    await s.execute(
                        text(
                            "UPDATE source_items SET deleted_at = :now, seen_at = :now "
                            "WHERE source_id = :sid AND external_id = ANY(:ids)"
                        ),
                        {"now": now, "sid": self.source_id, "ids": gone[i : i + PAGE_SIZE]},
                    )
        log.info("Источник %s: снимок %s", self.code, stats)
        return stats

    # -- DocumentSource ---------------------------------------------------------

    async def iter_documents(self, modified_since: datetime | None) -> AsyncIterator[list[DocRef]]:
        if not self._refreshed:
            last = await self._last_refresh()
            minutes = self.config.full_refresh_minutes
            stale = (
                modified_since is None
                or ":since" in self.config.call_sql
                or last is None
                or (datetime.now(UTC) - last).total_seconds() >= minutes * 60
            )
            if stale:
                await self.refresh(modified_since)
            self._refreshed = True
        cond = "source_id = :sid"
        params: dict[str, Any] = {"sid": self.source_id}
        if modified_since is not None:
            cond += " AND (version >= :since OR deleted_at >= :since)"
            params["since"] = modified_since
        after = ""
        while True:
            async with get_sessionmaker()() as s:
                rows = (
                    await s.execute(
                        text(
                            f"SELECT external_id, version, deleted_at FROM source_items "  # noqa: S608
                            f"WHERE {cond} AND external_id > :after "
                            f"ORDER BY external_id LIMIT {PAGE_SIZE}"
                        ),
                        params | {"after": after},
                    )
                ).all()
            if not rows:
                return
            yield [DocRef(r.external_id, r.version, r.deleted_at) for r in rows]
            after = rows[-1].external_id
            if len(rows) < PAGE_SIZE:
                return

    async def get_document(self, external_id: str) -> SourceDocument:
        async with get_sessionmaker()() as s:
            item = await s.get(SourceItem, (self.source_id, norm_id(external_id)))
        if item is None:
            raise DocumentNotFound(external_id)
        files = []
        for f in item.files:
            if f.get("sha256") is None:
                # Ошибку файла передаём через id: pipeline спросит файл и получит BadSourceData.
                file_id = "!" + str(f.get("error") or "файл не получен")
            else:
                file_id = f["sha256"]
            files.append(
                SourceFileRef(
                    file_id=file_id, name=f["name"], size=f.get("size"), mime=f.get("mime")
                )
            )
        return SourceDocument(
            external_id=item.external_id,
            modified_at=item.version,
            deleted_at=item.deleted_at,
            header=item.header,
            parts=[],
            files=files,
        )

    async def download_file(self, file_id: str, dest: Path, max_bytes: int) -> DownloadedFile:
        if file_id.startswith("!"):
            raise BadSourceData(file_id[1:])
        store = FileStore(self.settings)
        try:
            src = store.path_for(file_id)
        except ValueError:
            raise DocumentNotFound(file_id) from None
        if not src.exists():
            raise DocumentNotFound(file_id)
        size = src.stat().st_size
        if size > max_bytes:
            raise BadSourceData(f"Файл больше {max_bytes // (1 << 20)} МБ")
        await asyncio.to_thread(shutil.copyfile, src, dest)
        return DownloadedFile(path=dest, size=size, sha256=file_id)

    async def common_ids(self) -> set[str]:
        if not self.config.common_sql:
            return set()
        return await self._query_ids(self.config.common_sql, {"user": self.config.service_user})

    async def user_document_ids(self, personnel_number: str) -> set[str]:
        if self.config.access_mode == "sql":
            if not self.config.access_sql:
                raise SourceUnavailable("Не задан запрос прав (access_sql)")
            return await self._query_ids(self.config.access_sql, {"user": personnel_number})
        async with get_sessionmaker()() as s:
            rows = await s.scalars(
                select(SourceItem.external_id).where(
                    SourceItem.source_id == self.source_id, SourceItem.deleted_at.is_(None)
                )
            )
            return set(rows.all())
