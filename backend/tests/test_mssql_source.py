"""Источник в MS SQL Server: две таблицы функции → документы, синхронизация, права.

Вместо SQL Server — фейковая функция (fakes/fake_mssql.py) с теми же колонками, что на
скриншоте от разработчиков источника.
"""

import uuid
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import get_sessionmaker
from app.ingest import sync
from app.ingest.assemble import assemble_header
from app.models import Chunk, Document, DocumentFile, Source, SourceItem, SyncRun
from app.sources import open_source
from app.sources.base import BadSourceData
from app.sources.mssql import (
    ORD_ACCESS_SQL,
    ORD_LIST_SQL,
    ORD_MARK_SQL,
    MssqlConfig,
    ResultSet,
    SnapshotReader,
    file_kind,
    parse_address,
    parse_config,
    to_pyformat,
)
from fakes import fake_mssql as fm
from fakes.fake_mssql import FakeMssql
from tests.test_ingest_sync import drain

# ---------------------------------------------------------------------------
# Без базы: настройки, вызов, разбор таблиц
# ---------------------------------------------------------------------------


def test_address_and_config_validation() -> None:
    a = parse_address("mssql://sql.zavod.local:14330/Docflow")
    assert (a.host, a.port, a.database) == ("sql.zavod.local", 14330, "Docflow")
    assert parse_address("mssql://sql/Db").port == 1433
    with pytest.raises(BadSourceData, match="mssql://"):
        parse_address("https://sql.zavod.local/Docflow")
    with pytest.raises(BadSourceData, match="база данных"):
        parse_address("mssql://sql.zavod.local:1433/")

    assert parse_config({}).call_sql.startswith("EXEC")
    with pytest.raises(BadSourceData, match="EXEC или SELECT"):
        parse_config({"call_sql": "DROP TABLE dbo.Documents"})
    with pytest.raises(BadSourceData, match="один вызов"):
        parse_config({"call_sql": "EXEC dbo.A; DROP TABLE dbo.B"})
    with pytest.raises(BadSourceData, match="access_mode"):
        parse_config({"access_mode": "everyone"})


def test_placeholders_become_driver_parameters() -> None:
    sql = "EXEC dbo.GetDocuments @Login = :user, @Since = :since, @Mask = '10%'"
    assert to_pyformat(sql) == (
        "EXEC dbo.GetDocuments @Login = %(user)s, @Since = %(since)s, @Mask = '10%%'"
    )
    # «::» и время «12:30» — не подстановки.
    assert to_pyformat("SELECT CAST(x AS int)::text, '12:30'") == (
        "SELECT CAST(x AS int)::text, '12:30'"
    )


def test_file_kind_by_content_first() -> None:
    assert file_kind("docx", b"%PDF-1.4") == "pdf"  # содержимое важнее колонки
    assert file_kind(None, bytes.fromhex("D0CF11E0A1B11AE1")) == "doc"
    assert file_kind("tif", b"II*\x00abcd") == "tiff"
    assert file_kind(".DOCX", b"PK\x03\x04") == "docx"
    assert file_kind("application/pdf", b"????") == "pdf"


def test_reader_matches_tables_by_guid_and_stores_binaries(tmp_path: Path) -> None:
    settings = get_settings().model_copy(update={"documents_dir": str(tmp_path)})
    g = uuid.uuid4()
    meta = ResultSet(
        ["ID_GUID", "ID_Вид", "ID_Key", "Номер", "Дата", "Тип", "Тема", "Исполнитель"],
        [(g, 1, 17, "537к", datetime(2011, 5, 12), "ПРИКАЗ", "О допуске", None)],
    )
    files = ResultSet(
        ["ID_GUID", "BinaryFile", "ТипФайла"],
        [
            # GUID во второй таблице — строкой в нижнем регистре: всё равно тот же документ.
            (str(g).lower(), b"%PDF-1.4 test", "pdf"),
            (str(g).upper(), b"", "pdf"),
            ("00000000-0000-0000-0000-000000000000", b"%PDF-orphan", "pdf"),
        ],
    )
    snap = SnapshotReader(MssqlConfig(), settings).read(iter([meta, files]))
    key = str(g).upper()
    assert list(snap.docs) == [key] and snap.orphan_files == 1
    doc = snap.docs[key]
    assert doc.header == {
        "id": key,
        "_ID_Вид": 1,
        "_ID_Key": 17,
        "number": "537к",
        "date": "2011-05-12",
        "doc_type": "Приказ",
        "title": "О допуске",
    }
    ok, empty = doc.files
    assert ok["name"] == "приказ_537к_1.pdf" and ok["mime"] == "application/pdf"
    assert (tmp_path / ok["sha256"][:2] / ok["sha256"]).read_bytes() == b"%PDF-1.4 test"
    assert empty["sha256"] is None and empty["error"] == "источник отдал пустой файл"

    # Скрытые колонки в текст документа не попадают, «Исполнитель» — попадает.
    text = "\n".join(b.text for b in assemble_header(doc.header | {"executor": "Иванова"}, []))
    assert "Исполнитель: Иванова" in text and "ID_Key" not in text
    assert text.startswith("Приказ № 537к от 12.05.2011 «О допуске»")


def test_reader_fails_without_metadata_table(tmp_path: Path) -> None:
    settings = get_settings().model_copy(update={"documents_dir": str(tmp_path)})
    from app.sources.base import SourceUnavailable

    with pytest.raises(SourceUnavailable, match="ID_GUID"):
        SnapshotReader(MssqlConfig(), settings).read(iter([ResultSet(["x"], [(1,)])]))


# ---------------------------------------------------------------------------
# С базой: синхронизация через настоящий конвейер
# ---------------------------------------------------------------------------


async def _docs() -> dict[str, Document]:
    async with get_sessionmaker()() as s:
        return {d.external_id: d for d in (await s.scalars(select(Document))).all()}


async def _source(source_id: int) -> Source:
    async with get_sessionmaker()() as s:
        src = await s.get(Source, source_id)
        assert src is not None
        return src


@pytest.mark.db
async def test_first_sync_loads_documents_from_two_tables(
    mssql_source: int, fake_mssql: FakeMssql
) -> None:
    stats = await sync.run_incremental(mssql_source, get_settings(), get_sessionmaker())
    await drain()
    assert stats["new"] == 6 and fake_mssql.main_calls == 1

    docs = await _docs()
    assert fm.G_ORPHAN.upper() not in docs  # бинарник без строки в первой таблице
    ready = {k for k, d in docs.items() if d.status == "ready"}
    assert ready == {fm.G_537, fm.G_151, fm.G_149, fm.G_159, fm.G_162}
    empty = docs[fm.G_655]
    assert (empty.status, empty.error_stage) == ("error", "completeness")
    assert "пустой файл" in (empty.error_message or "")

    order = docs[fm.G_537]
    assert (order.doc_type, order.number, str(order.doc_date)) == ("Приказ", "537к", "2011-05-12")
    assert order.title == "О допуске персонала к работам на высоте"

    async with get_sessionmaker()() as s:
        header_chunk = await s.scalar(
            select(Chunk).where(Chunk.document_id == order.id, Chunk.kind == "header")
        )
        scan_file = await s.scalar(
            select(DocumentFile).where(DocumentFile.document_id == docs[fm.G_149].id)
        )
        scan_pages = set(
            await s.scalars(
                select(Chunk.page_from).where(
                    Chunk.document_id == docs[fm.G_149].id, Chunk.file_id.is_not(None)
                )
            )
        )
    assert header_chunk is not None and "Исполнитель: Иванова А. С." in header_chunk.text
    assert "ID_Key" not in header_chunk.text
    # Скан TIFF: две страницы распознаны OCR, ссылки ведут на страницу.
    assert scan_file is not None and (scan_file.mime, scan_file.ocr_pages) == ("image/tiff", 2)
    assert scan_file.name == "указание_149.tif"
    assert scan_pages == {1, 2}

    # Старый Word (.doc): таблица попала во фрагменты таблицей Markdown.
    async with get_sessionmaker()() as s:
        doc_file = await s.scalar(
            select(DocumentFile).where(DocumentFile.document_id == docs[fm.G_162].id)
        )
        doc_chunks = "\n".join(
            await s.scalars(select(Chunk.text).where(Chunk.document_id == docs[fm.G_162].id))
        )
    assert doc_file is not None and doc_file.mime == "application/msword"
    assert doc_file.name == "указание_162.doc"
    assert "| Укладка рубероида | до 01.07.2011 | Смирнов О. Л. |" in doc_chunks


@pytest.mark.db
async def test_incremental_uses_snapshot_and_reconcile_detects_changes(
    mssql_source: int, fake_mssql: FakeMssql
) -> None:
    sm, settings = get_sessionmaker(), get_settings()
    await sync.run_incremental(mssql_source, settings, sm)
    await drain()
    before = await _docs()

    # Инкремент сразу после загрузки: снимок свежий, функцию не вызываем, ничего не меняется.
    stats = await sync.run_incremental(mssql_source, settings, sm)
    assert fake_mssql.main_calls == 1 and stats["new"] == stats["changed"] == 0

    # У источника поменялся файл одного документа и тема другого, третий пропал.
    fake_mssql.replace_file(fm.G_655, (fm.FILES_DIR / "reglament_to.pdf").read_bytes())
    fake_mssql.change(fm.G_159, Тема="О возврате оборудования из аренды")
    fake_mssql.remove(fm.G_151)
    stats = await sync.run_reconcile(mssql_source, settings, sm)
    await drain()
    assert fake_mssql.main_calls == 2
    assert stats["changed"] == 2 and stats["deleted"] == 1

    after = await _docs()
    assert after[fm.G_655].status == "ready" and after[fm.G_655].version > before[fm.G_655].version
    assert after[fm.G_159].title == "О возврате оборудования из аренды"
    assert after[fm.G_151].deleted_at is not None
    assert after[fm.G_537].version == before[fm.G_537].version  # не менялся — версия та же

    # Документ вернулся в ответ функции — снова в поиске.
    fake_mssql.reset()
    stats = await sync.run_reconcile(mssql_source, settings, sm)
    await drain()
    assert stats["restored"] == 1
    assert (await _docs())[fm.G_151].deleted_at is None


@pytest.mark.db
async def test_sql_server_down_breaks_nothing(mssql_source: int, fake_mssql: FakeMssql) -> None:
    sm, settings = get_sessionmaker(), get_settings()
    await sync.run_incremental(mssql_source, settings, sm)
    await drain()
    fake_mssql.unavailable = True
    await sync.run_reconcile(mssql_source, settings, sm)

    src = await _source(mssql_source)
    assert src.available is False and "не отвечает" in (src.last_error or "")
    async with sm() as s:
        run = await s.scalar(select(SyncRun).order_by(SyncRun.id.desc()))
        alive = await s.scalar(
            select(SourceItem).where(SourceItem.deleted_at.is_not(None)).limit(1)
        )
    assert run is not None and run.status == "error"
    assert alive is None  # ничего не помечено удалённым
    assert all(d.deleted_at is None for d in (await _docs()).values())


@pytest.mark.db
async def test_rights_all_authenticated_and_sql_mode(
    mssql_source: int, fake_mssql: FakeMssql
) -> None:
    sm, settings = get_sessionmaker(), get_settings()
    await sync.run_incremental(mssql_source, settings, sm)

    # По умолчанию (допущение): любой вошедший видит все документы источника.
    src = await _source(mssql_source)
    adapter = open_source(src, settings)
    assert len(await adapter.user_document_ids("T-0003")) == 6
    assert await adapter.common_ids() == set()

    # Режим sql: права спрашиваем у источника отдельным вызовом с табельным номером.
    async with sm() as s, s.begin():
        row = await s.get(Source, mssql_source)
        assert row is not None
        row.config = row.config | {
            "access_mode": "sql",
            "access_sql": "EXEC dbo.GetUserDocuments @Login = :user",
            "common_sql": "EXEC dbo.GetCommonDocuments",
        }
    adapter = open_source(await _source(mssql_source), settings)
    assert await adapter.user_document_ids("T-0001") == {fm.G_537, fm.G_149}
    assert await adapter.user_document_ids("T-0003") == set()
    assert await adapter.user_document_ids("nobody") == set()
    assert await adapter.common_ids() == {fm.G_151}
    assert fake_mssql.calls[-2][1] == {"user": "nobody"}


def test_real_driver_reports_unreachable_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Настоящий драйвер python-tds: сервер не отвечает → «источник недоступен», без пароля."""
    from app.sources.base import SourceUnavailable
    from app.sources.mssql import TdsRunner

    src = Source(id=1, code="x", name="x", adapter="mssql", base_url="mssql://127.0.0.1:1/Db")
    cfg = MssqlConfig(encrypt=False, login_timeout_seconds=1)
    monkeypatch.delenv("SOURCE_MSSQL_USER", raising=False)
    with pytest.raises(SourceUnavailable, match="SOURCE_MSSQL_USER"):
        TdsRunner(src, cfg, get_settings())

    monkeypatch.setenv("SOURCE_MSSQL_USER", "rag_reader")
    monkeypatch.setenv("SOURCE_MSSQL_PASSWORD", "very-secret-pass")
    runner = TdsRunner(src, cfg, get_settings())
    with pytest.raises(SourceUnavailable, match="не отвечает") as err:
        runner.connect()
    assert "very-secret-pass" not in str(err.value)

    # Шифрование включено, а сертификата CA нет — понятная ошибка до подключения.
    settings = get_settings().model_copy(
        update={"source_ca_file": None, "external_auth_ca_file": None}
    )
    with pytest.raises(SourceUnavailable, match="SOURCE_CA_FILE"):
        TdsRunner(src, MssqlConfig(), settings).connect()


@pytest.mark.db
async def test_document_card_hides_technical_columns_and_guest_sees_nothing(
    client, mssql_source: int, fake_mssql: FakeMssql
) -> None:  # type: ignore[no-untyped-def]
    from tests.helpers import auth, login_ok, seed_test_users

    await seed_test_users()
    await sync.run_incremental(mssql_source, get_settings(), get_sessionmaker())
    await drain()
    doc_id = (await _docs())[fm.G_149].id

    user = auth(await login_ok(client, "T-0003"))  # все вошедшие видят документы источника
    r = await client.get(f"/api/documents/{doc_id}", headers=user)
    assert r.status_code == 200, r.text
    labels = {f["label"] for f in r.json()["fields"]}
    assert "Исполнитель" in labels and not any(lbl.startswith("_") for lbl in labels)

    r = await client.get(f"/api/documents/{doc_id}")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "login_required"
    r = await client.get("/api/documents")
    assert r.json()["total"] == 0


# ---------------------------------------------------------------------------
# Процедура «Электронный архив ОРД» (04.11.2026): список-очередь, запись, доступ
# ---------------------------------------------------------------------------


async def _use_ord_procedure(source_id: int, **extra: object) -> None:
    async with get_sessionmaker()() as s, s.begin():
        row = await s.get(Source, source_id)
        assert row is not None
        row.config = {"call_sql": ORD_LIST_SQL, "mark_sql": ORD_MARK_SQL, **extra}


def test_ord_procedure_config() -> None:
    cfg = parse_config(
        {"mark_sql": ORD_MARK_SQL, "access_mode": "sql", "access_sql": ORD_ACCESS_SQL}
    )
    assert cfg.call_sql == ORD_LIST_SQL and cfg.queue
    assert to_pyformat(ORD_MARK_SQL).endswith("@ID_GUID = %(id)s")
    assert to_pyformat(ORD_ACCESS_SQL).endswith("@ID_UserBoss = %(user)s")
    assert not parse_config({}).queue
    with pytest.raises(BadSourceData, match=":id"):
        parse_config({"mark_sql": "EXEC dbo.Mark"})


@pytest.mark.db
async def test_queue_marks_each_loaded_document_on_the_same_connection(
    mssql_source: int, fake_mssql: FakeMssql
) -> None:
    await _use_ord_procedure(mssql_source)
    sm, settings = get_sessionmaker(), get_settings()
    stats = await sync.run_incremental(mssql_source, settings, sm)
    await drain()
    assert stats["new"] == 6

    # Одно соединение на всю загрузку: «список», затем «запись» по каждому документу.
    assert fake_mssql.connections == 1 and fake_mssql.open_connections == 0
    assert set(fake_mssql.connection_of_call) == {1}
    kinds = [("(список)" in sql, "(запись)" in sql) for sql, _ in fake_mssql.calls]
    assert kinds[0] == (True, False) and all(m for _, m in kinds[1:])
    # Пустой файл (655к) не отмечаем: пусть источник отдаст его снова.
    assert fake_mssql.marked == {fm.G_537, fm.G_151, fm.G_149, fm.G_159, fm.G_162}
    assert {p["id"] for _, p in fake_mssql.calls[1:]} == fake_mssql.marked

    # Следующая синхронизация: в очереди только 655к. Остальные НЕ считаются удалёнными.
    await sync.run_incremental(mssql_source, settings, sm)
    await drain()
    assert fake_mssql.connections == 2
    list_calls = [sql for sql, _ in fake_mssql.calls if "(список)" in sql]
    assert len(list_calls) == 2
    docs = await _docs()
    assert all(d.deleted_at is None for d in docs.values())
    assert {k for k, d in docs.items() if d.status == "ready"} == {
        fm.G_537,
        fm.G_151,
        fm.G_149,
        fm.G_159,
        fm.G_162,
    }

    # Ночная сверка в режиме очереди тоже ничего не удаляет.
    await sync.run_reconcile(mssql_source, settings, sm)
    await drain()
    assert all(d.deleted_at is None for d in (await _docs()).values())


@pytest.mark.db
async def test_queue_mark_failure_keeps_saved_documents(
    mssql_source: int, fake_mssql: FakeMssql, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Источник «лёг» посреди отметок: сохранённое остаётся у нас, соединение закрыто."""
    from app.sources.base import SourceUnavailable

    await _use_ord_procedure(mssql_source)
    original = fake_mssql.execute

    def flaky(sql: str, params: dict, conn_no: int = 0):  # type: ignore[no-untyped-def]
        if "(запись)" in sql and len(fake_mssql.marked) >= 2:
            raise SourceUnavailable("SQL Server не отвечает: обрыв")
        return original(sql, params, conn_no)

    monkeypatch.setattr(fake_mssql, "execute", flaky)
    adapter = open_source(await _source(mssql_source), get_settings())
    with pytest.raises(SourceUnavailable):
        await adapter.refresh(None)  # type: ignore[attr-defined]
    assert fake_mssql.open_connections == 0 and len(fake_mssql.marked) == 2
    async with get_sessionmaker()() as s:
        saved = set(await s.scalars(select(SourceItem.external_id)))
    assert len(saved) == 6  # неотмеченные придут снова и совпадут по отпечатку


@pytest.mark.db
async def test_access_procedure_uses_six_digit_id_set_by_admin(
    client, mssql_source: int, fake_mssql: FakeMssql
) -> None:  # type: ignore[no-untyped-def]
    from tests.helpers import auth, login_ok, seed_test_users

    await seed_test_users()
    await _use_ord_procedure(mssql_source, access_mode="sql", access_sql=ORD_ACCESS_SQL)
    await sync.run_incremental(mssql_source, get_settings(), get_sessionmaker())
    await drain()

    async def visible(token: str) -> set[str]:
        r = await client.get("/api/documents", headers=auth(token))
        assert r.status_code == 200, r.text
        docs = await _docs()
        by_id = {d.id: k for k, d in docs.items()}
        return {by_id[i["id"]] for i in r.json()["items"]}

    user = await login_ok(client, "T-0003")  # по табельному номеру источник ничего не даёт
    assert await visible(user) == set()
    assert fake_mssql.calls[-1][1] == {"user": "T-0003"}

    admin = auth(await login_ok(client, "T-0004"))
    users = (await client.get("/api/admin/users?q=T-0003", headers=admin)).json()["items"]
    uid = users[0]["id"]
    bad = await client.patch(
        f"/api/admin/users/{uid}", json={"source_user_id": "12345"}, headers=admin
    )
    assert bad.status_code == 422

    r = await client.patch(
        f"/api/admin/users/{uid}", json={"source_user_id": "784683"}, headers=admin
    )
    assert r.status_code == 200 and r.json()["source_user_id"] == "784683"
    # Кэш прав сброшен: сразу видны документы ID 784683, а не старый пустой ответ.
    assert await visible(user) == {fm.G_537, fm.G_151}
    assert fake_mssql.calls[-1][1] == {"user": "784683"}
    assert "(доступ)" in fake_mssql.calls[-1][0]

    r = await client.patch(f"/api/admin/users/{uid}", json={"source_user_id": ""}, headers=admin)
    assert r.json()["source_user_id"] is None
    assert await visible(user) == set()
