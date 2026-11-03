"""Проверка ЕПоЗ, этап 1: разбор с нумерацией, редакции, защита документов, доступ по группе.

Главные правила:
- номера пунктов восстанавливаются и по ним находится нужный пункт («п. 1.1.2»);
- документ ЕПоЗ нельзя удалить, исключить, переразобрать (API и триггер в БД);
- ЕПоЗ видят только члены группы, которой открыт комплект; гость и остальные — нет;
- замена части идёт через черновик, прежняя часть уходит в архив и становится невидимой.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import get_sessionmaker
from epoz import loader
from epoz.docx_parse import parse_epoz_docx, table_blocks
from tests.epoz.factory import make_app_docx, make_main_docx, make_matrix_docx

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


# ----------------------------------------------------------------- без базы данных


def test_detect_part() -> None:
    assert loader.detect_part("ЕПоЗ_сент 2026.docx") == "main"
    assert loader.detect_part("Приложение 5_Определение НМЦ.docx") == "app5"
    assert (
        loader.detect_part("Приложение 14 Перечень продукции с иными сроками оплаты.docx")
        == "app14"
    )
    assert loader.detect_part("Приложение 6_ Оценка и сопоставление заявок.docx") == "app6"
    assert (
        loader.detect_part("Прил.2 к прот.8-02.09.2026_Перечень изменений.docx") == loader.IGNORED
    )
    assert loader.detect_part("Приложение 99.docx") is None
    assert loader.detect_part("что-то.docx") is None


def test_numbering_restored_from_toc(tmp_path: Path) -> None:
    parsed = parse_epoz_docx(make_main_docx(tmp_path / "main.docx"))
    rep = parsed.report
    assert rep["method"] == "toc"
    assert rep["headings"] == rep["headings_matched"] == 6
    labels = [c.label for c in parsed.clauses if c.label]
    # Подраздел 1.1 содержит два пункта, у второго два подпункта и один вложенный.
    assert "1.1.1" in labels and "1.1.2" in labels
    assert "1.1.2(1)" in labels and "1.1.2(2)" in labels
    assert "1.1.2(2)(а)" in labels
    assert "1.2.1" in labels  # счётчик пунктов начался заново в новом подразделе
    assert "2.1.18" in labels
    # Заголовки получили номера из оглавления, а текст пункта — свой номер.
    by_label = {c.label: c for c in parsed.clauses if c.label}
    assert by_label["1.1"].is_heading and by_label["1.1"].text.startswith(
        "1.1 Требования к продукции"
    )
    assert by_label["1.1.2"].text.startswith("1.1.2. Срок оплаты")
    # Ссылка «п. 1.1.2» нашла пункт: доля найденных ссылок 100%.
    assert rep["refs_total"] == 1 and rep["refs_rate"] == 1.0


def test_typed_numbers_in_appendix(tmp_path: Path) -> None:
    parsed = parse_epoz_docx(make_app_docx(tmp_path / "app.docx"))
    assert parsed.report["method"] == "numbering"
    labels = {c.label for c in parsed.clauses if c.label}
    assert {"1", "1.1", "1.2"} <= labels


def test_matrix_becomes_sentences(tmp_path: Path) -> None:
    from docx import Document

    path = make_matrix_docx(tmp_path / "m.docx")
    blocks = table_blocks(Document(str(path)).tables[0])
    texts = [b.text for b in blocks]
    assert all(b.kind == "text" for b in blocks)
    first = texts[0]
    # Значения стоят под своими столбцами, объединённая ячейка получила обе подписи.
    assert "Конкурс / I группы: 15 дней" in first
    assert "Конкурс / II группы и Конкурс / ИДО: 30 дней" in first
    assert "Аукцион / I группы: 10 дней" in first
    assert "Аукцион / II группы и Аукцион / ИДО: не применимо" in first
    assert texts[0].startswith("Условие: Минимальный срок подачи заявок.")


# ----------------------------------------------------------------- с базой данных


async def _upload(client: httpx.AsyncClient, headers: Any, path: Path, name: str) -> dict[str, Any]:
    r = await client.post(
        "/api/admin/epoz/draft/files",
        files=[("files", (name, await asyncio.to_thread(path.read_bytes), DOCX))],
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _titles(client: httpx.AsyncClient, headers: Any = None) -> list[str]:
    r = await client.get("/api/documents", headers=headers)
    assert r.status_code == 200, r.text
    return [d["title"] for d in r.json()["items"]]


async def _activated(client: httpx.AsyncClient, env: dict[str, Any], tmp_path: Path) -> None:
    await _upload(client, env["admin"], make_main_docx(tmp_path / "m.docx"), "ЕПоЗ_тест.docx")
    await _upload(
        client,
        env["admin"],
        make_app_docx(tmp_path / "a5.docx"),
        "Приложение 5_Определение НМЦ.docx",
    )
    r = await client.put(
        "/api/admin/epoz/groups", json={"group_ids": [env["group_id"]]}, headers=env["admin"]
    )
    assert r.status_code == 200, r.text
    r = await client.post(
        "/api/admin/epoz/draft/activate", json={"force": True}, headers=env["admin"]
    )
    assert r.status_code == 200, r.text


@pytest.mark.db
async def test_upload_report_and_blockers(
    client: httpx.AsyncClient, epoz_env: dict[str, Any], tmp_path: Path
) -> None:
    body = await _upload(
        client, epoz_env["admin"], make_main_docx(tmp_path / "m.docx"), "ЕПоЗ_тест.docx"
    )
    assert body["results"][0]["ok"] and body["results"][0]["part"] == "main"
    rep = body["draft"]["report"]
    assert rep["parts"]["main"]["status"] == "ok"
    assert rep["parts"]["main"]["headings_matched"] == 6
    # Остальные части не загружены, пункты 10.3 и 6.6 не найдены: активация без force закрыта.
    assert not rep["can_activate"] and rep["missing_parts"]
    r = await client_post(client, epoz_env["admin"], {"force": False})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_ready"
    # Перечень изменений не загружается.
    r2 = await client.post(
        "/api/admin/epoz/draft/files",
        files=[("files", ("Прил.2 к прот.8_Перечень изменений.docx", b"x", DOCX))],
        headers=epoz_env["admin"],
    )
    assert (
        r2.json()["results"][0]["ok"] is False
        and "не загружается" in r2.json()["results"][0]["message"]
    )
    # Не Word: понятная ошибка, а не 500.
    r3 = await client.post(
        "/api/admin/epoz/draft/files",
        files=[("files", ("Приложение 3_тест.docx", b"not a docx", DOCX))],
        headers=epoz_env["admin"],
    )
    assert r3.json()["results"][0]["ok"] is False


async def client_post(
    client: httpx.AsyncClient, headers: Any, body: dict[str, Any]
) -> httpx.Response:
    return await client.post("/api/admin/epoz/draft/activate", json=body, headers=headers)


@pytest.mark.db
async def test_only_admin_manages(client: httpx.AsyncClient, epoz_env: dict[str, Any]) -> None:
    for headers in (epoz_env["member"], epoz_env["outsider"], None):
        r = await client.get("/api/admin/epoz/status", headers=headers)
        assert r.status_code in (401, 403), headers


@pytest.mark.db
async def test_group_sees_epoz_others_do_not(
    client: httpx.AsyncClient, epoz_env: dict[str, Any], tmp_path: Path
) -> None:
    await _activated(client, epoz_env, tmp_path)
    assert "Единое положение о закупке (ЕПоЗ)" in await _titles(client, epoz_env["member"])
    assert await _titles(client, epoz_env["outsider"]) == []  # ни общего, ни ЕПоЗ
    assert await _titles(client) == []  # гость не видит документов вообще
    # Поиск: тот, кому открыт комплект, находит пункт; остальные — ничего.
    q = {"query": "срок оплаты"}
    found = (await client.post("/api/search", json=q, headers=epoz_env["member"])).json()
    assert found["found"] > 0
    for headers in (epoz_env["outsider"], None):
        r = await client.post("/api/search", json=q, headers=headers)
        assert r.status_code == 200 and r.json()["found"] == 0


@pytest.mark.db
async def test_protected_cannot_be_deleted_or_excluded(
    client: httpx.AsyncClient, epoz_env: dict[str, Any], engine: AsyncEngine, tmp_path: Path
) -> None:
    await _activated(client, epoz_env, tmp_path)
    async with engine.connect() as conn:
        doc_id = (
            await conn.execute(text("SELECT id FROM documents WHERE epoz_part='main'"))
        ).scalar_one()
        assert (
            await conn.execute(
                text("SELECT is_protected FROM documents WHERE id=:i"), {"i": doc_id}
            )
        ).scalar_one()
    # База: ни удаление, ни исключение, ни снятие защиты.
    for sql in (
        "DELETE FROM documents WHERE id=:i",
        "UPDATE documents SET is_excluded=true WHERE id=:i",
        "UPDATE documents SET deleted_at=now() WHERE id=:i",
        "UPDATE documents SET is_protected=false WHERE id=:i",
    ):
        with pytest.raises(DBAPIError, match="ЕПоЗ"):
            async with engine.begin() as conn:
                await conn.execute(text(sql), {"i": doc_id})
    # API админки документов: все «опасные» кнопки закрыты.
    h = epoz_env["admin"]
    base = f"/api/admin/documents/{doc_id}"
    for method, url, body in (
        ("POST", f"{base}/exclude", {"excluded": True}),
        ("POST", f"{base}/reindex", None),
        ("POST", f"{base}/retry", None),
        ("PATCH", base, {"title": "другое"}),
        ("PUT", f"{base}/access", {"personnel_numbers": ["T-0003"], "group_ids": []}),
    ):
        r = await client.request(method, url, json=body, headers=h)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "protected", (
            method,
            url,
            r.text,
        )


@pytest.mark.db
async def test_new_edition_replaces_part_and_archives_old(
    client: httpx.AsyncClient, epoz_env: dict[str, Any], engine: AsyncEngine, tmp_path: Path
) -> None:
    await _activated(client, epoz_env, tmp_path)
    async with engine.connect() as conn:
        old_main = (
            await conn.execute(text("SELECT id FROM documents WHERE epoz_part='main'"))
        ).scalar_one()
    # Новая редакция: черновик наследует Приложение 5, а Положение заменяем.
    body = await _upload(
        client,
        epoz_env["admin"],
        make_main_docx(tmp_path / "m2.docx", edition="B"),
        "ЕПоЗ_новая.docx",
    )
    parts = body["draft"]["report"]["parts"]
    assert parts["app5"]["inherited"] is True and parts["main"]["inherited"] is False
    r = await client_post(client, epoz_env["admin"], {"force": True})
    assert r.status_code == 200
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT id, is_protected FROM documents WHERE epoz_part='main' ORDER BY id")
            )
        ).all()
        grants = (
            await conn.execute(
                text("SELECT count(*) FROM document_grants WHERE document_id=:i"), {"i": old_main}
            )
        ).scalar_one()
    assert len(rows) == 2 and all(p for _, p in rows)  # прежнее Положение осталось и защищено
    assert grants == 0  # но доступа к архивной части нет ни у кого
    # Участник группы видит одно Положение (новое), в нём «Редакция B».
    titles = await _titles(client, epoz_env["member"])
    assert titles.count("Единое положение о закупке (ЕПоЗ)") == 1
    async with engine.connect() as conn:
        new_main = (
            await conn.execute(text("SELECT max(id) FROM documents WHERE epoz_part='main'"))
        ).scalar_one()
    found = (
        await client.post("/api/search", json={"query": "Редакция"}, headers=epoz_env["member"])
    ).json()
    doc_ids = {i["document_id"] for i in found["items"]}
    assert new_main in doc_ids and old_main not in doc_ids  # поиск идёт только по новой редакции


@pytest.mark.db
async def test_cancel_draft_removes_only_new_documents(
    client: httpx.AsyncClient, epoz_env: dict[str, Any], engine: AsyncEngine, tmp_path: Path
) -> None:
    await _activated(client, epoz_env, tmp_path)
    await _upload(
        client,
        epoz_env["admin"],
        make_main_docx(tmp_path / "m3.docx", edition="C"),
        "ЕПоЗ_ещё.docx",
    )
    async with engine.connect() as conn:
        before = (
            await conn.execute(text("SELECT count(*) FROM documents WHERE epoz_part='main'"))
        ).scalar_one()
    assert before == 2
    r = await client.delete("/api/admin/epoz/draft", headers=epoz_env["admin"])
    assert r.status_code == 204
    async with engine.connect() as conn:
        after = (
            await conn.execute(text("SELECT count(*) FROM documents WHERE epoz_part='main'"))
        ).scalar_one()
        draft = (
            await conn.execute(text("SELECT count(*) FROM epoz_editions WHERE status='draft'"))
        ).scalar_one()
    assert after == 1 and draft == 0  # действующее Положение цело, лишнего документа нет


@pytest.mark.db
async def test_clause_lookup_by_number(
    client: httpx.AsyncClient, epoz_env: dict[str, Any], tmp_path: Path
) -> None:
    await _activated(client, epoz_env, tmp_path)
    r = await client.get(
        "/api/admin/epoz/clauses",
        params={"part": "main", "label": "1.1.2"},
        headers=epoz_env["admin"],
    )
    body = r.json()
    assert body["found"]
    labels = [c["label"] for c in body["clauses"]]
    assert labels[0] == "1.1.2" and "1.1.2(1)" in labels and "1.1.2(2)(а)" in labels
    assert "1.1.1" not in labels  # соседний пункт не попал в подпункты
    # 1.1 содержит свои пункты, но не «1.10»-подобные: сравнение по границе номера.
    async with get_sessionmaker()() as s:
        sub = await loader.get_clauses(s, "main", "1.1")
    assert {c.label for c in sub} >= {"1.1", "1.1.1", "1.1.2"}
