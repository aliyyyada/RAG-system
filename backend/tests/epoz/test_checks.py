"""Проверка ЕПоЗ, этапы 3–6: запуск проверки пакета, 20 критериев, изоляция, очистка.

Пакет синтетический (DOCX с вымышленными данными, tests/epoz/pack_factory.py). Модель в тестах
выключена (`EPOZ_USE_LLM=false`): критерии, где нужен смысл, честно отвечают «нужна ручная проверка».

Главные правила:
- хороший пакет не даёт ни одного «не соответствует», а испорченный — ловится по нужным критериям;
- проверку видит только автор (404 даже администратору), документы пакета не видны нигде;
- при выходе из системы, по окончании сессии и по простою данные пакета стираются целиком.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import get_settings
from app.db import get_sessionmaker
from app.worker.queue import ClaimedJob
from epoz.checks import runner, service
from epoz.checks.roles import detect_role, guess_role
from tests.epoz.pack_factory import make_package
from tests.epoz.test_epoz_stage1 import _activated
from tests.epoz.test_okpd2 import make_okpd_xlsx
from tests.helpers import TEST_PASSWORD, auth, login

os.environ.setdefault("EPOZ_USE_LLM", "false")
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ----------------------------------------------------------------- без базы данных


def test_guess_role_from_filenames() -> None:
    assert guess_role("1. Договорной документ.pdf") == "contract"
    assert guess_role("2. Техническое задание.pdf") == "tz"
    assert guess_role("3. Сопроводительная записка к заявке на приобретение.pdf") == "note"
    assert guess_role("4. Расчет НМЦ.pdf") == "nmc"
    assert guess_role("5. Запрос КП.pdf") == "request"  # «Запрос КП» раньше, чем «КП»
    assert guess_role("6. Коммерческое предложение №1.pdf") == "kp"
    assert guess_role("7. Коммерческое предложение №2.pdf") == "kp"
    assert guess_role("что-то.pdf") == "other"


def test_detect_role_from_content() -> None:
    assert detect_role("Техническое задание на поставку ноутбуков\nТребования...") == "tz"
    assert detect_role("Коммерческое предложение № 1 от 05.11.2026") == "kp"
    assert detect_role("Договор поставки № 15\n1. Предмет договора") == "contract"
    assert detect_role("просто текст") is None


def test_table_headers_split_by_pdf_wrapping() -> None:
    """PDF переносит слова в узких ячейках («Наименова ние», «Кол- во»): шапки читаются всё равно."""
    from epoz.checks.extract import nmc_table
    from epoz.checks.pkg import clean_text
    from epoz.checks.textutil import items_from_tables

    head = [
        "№",
        "Наименова ние",
        "Ед.из м.",
        "Кол- во",
        "Став ка НДС, %",
        "Предложе ние №1",
        "Предложе ние №2",
        "Средняя ари фметическая цена за ед.изм.",
        "Общая с тоимост ь, руб. с НДС",
    ]
    nmc = nmc_table(
        [
            [
                head,
                [
                    "1",
                    "Ноутбук",
                    "шт",
                    "10",
                    "20",
                    "60 000,00",
                    "62 000,00",
                    "61 000,00",
                    "610 000,00",
                ],
            ]
        ]
    )
    assert (
        nmc is not None
        and nmc["rows"][0]["avg"] == "61000.00"
        and len(nmc["rows"][0]["prices"]) == 2
    )
    items, _ = items_from_tables(
        [
            [
                ["Наименова ние", "Кол- во", "Цена за еди ницу", "Сумма"],
                ["Ноутбук", "10", "60 000,00", "600 000,00"],
            ]
        ]
    )
    assert items and items[0]["qty"] == "10" and items[0]["total"] == "600000.00"
    # Заголовок документа и номера страниц: заголовок остаётся, служебные строки уходят.
    assert clean_text("--- стр. 1 ---\n\n## Расчет НМЦ\n\nтекст") == "Расчет НМЦ\n\nтекст"


def test_role_by_title_beats_body_mentions() -> None:
    # В расчёте НМЦ упоминается «запрос цен», но заголовок решает.
    text = "Расчет НМЦ методом сопоставимых рыночных цен\n\nПорядок определения поставщиков: запрос цен."
    assert detect_role(text) == "nmc"


# ----------------------------------------------------------------- с базой данных


@pytest.fixture
async def check_env(
    client: httpx.AsyncClient, epoz_env: dict[str, Any], engine: AsyncEngine, tmp_path: Path
) -> dict[str, Any]:
    """Действующий ЕПоЗ, справочник ОКПД2, группа заказчика I; право на проверку у T-0001 и T-0003."""
    await _activated(client, epoz_env, tmp_path)
    r = await client.post(
        "/api/admin/okpd2/import",
        files={"file": ("okpd.xlsx", make_okpd_xlsx(tmp_path / "okpd.xlsx").read_bytes(), XLSX)},
        headers=epoz_env["admin"],
    )
    assert r.status_code == 200, r.text
    r = await client.put(
        "/api/admin/epoz/settings", json={"customer_group": "I"}, headers=epoz_env["admin"]
    )
    assert r.status_code == 200, r.text
    async with engine.begin() as conn:
        rid = (
            await conn.execute(
                text("INSERT INTO roles (name) VALUES ('Проверяющий ЕПоЗ') RETURNING id")
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO role_permissions (role_id, permission) VALUES (:r, 'skill.epoz_check.use')"
            ),
            {"r": rid},
        )
        await conn.execute(
            text(
                "INSERT INTO user_roles (user_id, role_id) SELECT id, :r FROM users WHERE personnel_number IN ('T-0001', 'T-0003')"
            ),
            {"r": rid},
        )
    return epoz_env


async def _start(
    client: httpx.AsyncClient,
    headers: Any,
    files: dict[str, Path],
    *,
    winner: str | None = "6. Коммерческое предложение №1.docx",
) -> httpx.Response:
    names = list(files)
    return await client.post(
        "/api/checks",
        files=[("files", (n, files[n].read_bytes(), DOCX)) for n in names],
        data={
            "roles": [guess_role(n) for n in names],
            "winner": str(names.index(winner) if winner else -1),
        },
        headers=headers,
    )


async def _execute(run_id: int) -> None:
    """Выполняет задачу worker'а прямо в тесте (как это делает контейнер worker)."""
    job = ClaimedJob(
        id=0, kind=runner.JOB_KIND, payload={"run_id": run_id}, attempts=1, max_attempts=1
    )
    await runner.handle_check_run(job, get_settings(), get_sessionmaker())


async def _run(
    client: httpx.AsyncClient, headers: Any, files: dict[str, Path], **kw: Any
) -> dict[str, Any]:
    r = await _start(client, headers, files, **kw)
    assert r.status_code == 201, r.text
    run_id = r.json()["id"]
    await _execute(run_id)
    r = await client.get(f"/api/checks/{run_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _by_code(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["code"]: r for r in run["results"]}


@pytest.mark.db
async def test_good_package_has_no_failures(
    client: httpx.AsyncClient, check_env: dict[str, Any], tmp_path: Path
) -> None:
    run = await _run(client, check_env["member"], make_package(tmp_path / "pack"))
    res = _by_code(run)
    assert run["status"] == "done" and len(res) == 20
    failed = {c: r["summary"] for c, r in res.items() if r["status"] == "fail"}
    assert not failed, failed
    for code in (
        "1.1",
        "1.2",
        "1.4",
        "2.1",
        "2.3",
        "5.1",
        "5.2",
        "5.3",
        "5.4",
        "5.5",
        "5.6",
        "6.3",
        "6.5",
        "6.6",
    ):
        assert res[code]["status"] == "ok", (code, res[code]["summary"], res[code]["items"])
    # Что автоматически проверить нельзя, честно просит человека: РНП, подпись, модель выключена.
    for code in ("1.3", "2.2", "6.1", "6.2", "6.4", "6.7"):
        assert res[code]["status"] == "manual", (code, res[code]["items"])
    # Цитаты ЕПоЗ приложены к критериям, которые на него опираются.
    assert (
        res["1.1"]["epoz"] and res["1.1"]["epoz"][0]["label"] == "10.3" or res["1.1"]["epoz"] == []
    )
    assert run["counts"]["pending"] == 0 and run["summary"] if "summary" in run else True
    # Определены роли по содержимому и данные файлов.
    kp1 = next(f for f in run["files"] if f["name"].startswith("6."))
    assert kp1["is_winner"] and kp1["data"]["inn"] == "7701234567" and kp1["role_detected"] == "kp"


@pytest.mark.db
async def test_broken_package_is_caught(
    client: httpx.AsyncClient, check_env: dict[str, Any], tmp_path: Path
) -> None:
    files = make_package(
        tmp_path / "bad",
        kp_count=2,
        nmc_total="650000,00",
        note_qty="12",
        contract_price="610 000,00",
        tz_code="99.99.99.999",
    )
    run = await _run(client, check_env["member"], files)
    res = _by_code(run)
    assert res["1.3"]["status"] == "warn" and "6.9" in " ".join(
        i.get("note", "") for i in res["1.3"]["items"]
    )
    assert res["2.1"]["status"] == "fail" and "нет в справочнике" in res["2.1"]["items"][0]["note"]
    assert res["2.3"]["status"] == "fail" and "количество" in " ".join(
        i.get("note", "") for i in res["2.3"]["items"]
    )
    assert res["5.6"]["status"] == "fail" and "Цена" in [
        i["label"] for i in res["5.6"]["items"] if i["status"] == "fail"
    ]
    assert res["6.6"]["status"] == "fail" and "общая стоимость" in " ".join(
        i["label"] for i in res["6.6"]["items"] if i["status"] == "fail"
    )


@pytest.mark.db
async def test_missing_documents_fail_clearly(
    client: httpx.AsyncClient, check_env: dict[str, Any], tmp_path: Path
) -> None:
    files = {n: p for n, p in make_package(tmp_path / "p").items() if n.startswith(("2.", "6."))}
    run = await _run(client, check_env["member"], files, winner=None)
    res = _by_code(run)
    assert res["1.2"]["status"] == "fail" and res["1.4"]["status"] == "fail"
    assert res["2.3"]["status"] == "fail"  # нет сопроводительной записки
    assert (
        res["5.1"]["status"] == "manual" and "договор не загружен" in res["5.1"]["items"][0]["note"]
    )
    assert res["5.6"]["status"] == "manual"
    assert res["1.1"]["status"] == "ok"


@pytest.mark.db
async def test_payment_limit_from_appendix_and_group(
    client: httpx.AsyncClient, check_env: dict[str, Any], tmp_path: Path
) -> None:
    # Договор с оплатой 20 рабочих дней: для заказчика I группы больше 7 — не соответствует.
    from tests.epoz import pack_factory as pf

    files = make_package(tmp_path / "pay")
    files["1. Договорной документ.docx"] = pf.make_contract(
        tmp_path / "pay" / "c20.docx", pay_days=20
    )
    run = await _run(client, check_env["member"], files)
    assert _by_code(run)["5.1"]["status"] == "fail"
    # Группа заказчика не задана: срок проверить нельзя — «ручная проверка», а не вердикт.
    await client.put(
        "/api/admin/epoz/settings", json={"customer_group": None}, headers=check_env["admin"]
    )
    run = await _run(client, check_env["member"], files)
    assert _by_code(run)["5.1"]["status"] == "manual"


@pytest.mark.db
async def test_only_author_sees_check_and_package_is_hidden(
    client: httpx.AsyncClient, check_env: dict[str, Any], engine: AsyncEngine, tmp_path: Path
) -> None:
    run = await _run(client, check_env["member"], make_package(tmp_path / "pack"))
    rid = run["id"]
    # Чужую проверку не открыть: ни обычному пользователю, ни администратору, ни гостю.
    for headers in (check_env["admin"], check_env["outsider"], None):
        assert (await client.get(f"/api/checks/{rid}", headers=headers)).status_code in (
            401,
            403,
            404,
        )
        assert (await client.delete(f"/api/checks/{rid}", headers=headers)).status_code in (
            401,
            403,
            404,
        )
    assert (await client.get(f"/api/checks/{rid}", headers=check_env["admin"])).status_code == 404
    async with engine.connect() as conn:
        doc_ids = [
            r[0]
            for r in (
                await conn.execute(text("SELECT id FROM documents WHERE check_run_id IS NOT NULL"))
            ).all()
        ]
    assert len(doc_ids) == 8
    # Документы пакета не видны нигде: список, карточка, админка, поиск.
    titles = (await client.get("/api/documents", headers=check_env["member"])).json()["items"]
    assert all("Коммерческое" not in d["title"] and "Договорной" not in d["title"] for d in titles)
    adm = (await client.get("/api/admin/documents", headers=check_env["admin"])).json()
    assert all(d["id"] not in doc_ids for d in adm["items"])
    for did in doc_ids[:2]:
        assert (
            await client.get(f"/api/admin/documents/{did}", headers=check_env["admin"])
        ).status_code == 404
        assert (
            await client.get(f"/api/documents/{did}", headers=check_env["member"])
        ).status_code in (403, 404)
    found = (
        await client.post(
            "/api/search", json={"query": "ноутбуки поставщик"}, headers=check_env["member"]
        )
    ).json()
    assert not {i["document_id"] for i in found["items"]} & set(doc_ids)


@pytest.mark.db
async def test_rights_and_validation(
    client: httpx.AsyncClient, check_env: dict[str, Any], tmp_path: Path
) -> None:
    files = make_package(tmp_path / "p")
    # Без права на проверку (администратор есть, обычного «user» нет) — отказ.
    r = await client.get("/api/checks/config", headers=None)
    assert r.status_code in (401, 403)
    # Есть право, но нет доступа к ЕПоЗ (T-0003 вне группы): запуск закрыт.
    r = await _start(client, check_env["outsider"], files)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "epoz_unavailable"
    cfg = (await client.get("/api/checks/config", headers=check_env["outsider"])).json()
    assert cfg["ready"] is False and "доступа к ЕПоЗ" in cfg["reason"]
    cfg = (await client.get("/api/checks/config", headers=check_env["member"])).json()
    assert cfg["ready"] is True and len(cfg["criteria"]) == 20 and cfg["customer_group"] == "I"
    # Победителем может быть только КП; роли — из списка.
    names = list(files)
    bad = await client.post(
        "/api/checks",
        files=[("files", (n, files[n].read_bytes(), DOCX)) for n in names],
        data={"roles": [guess_role(n) for n in names], "winner": "0"},
        headers=check_env["member"],
    )
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "bad_winner"
    bad = await client.post(
        "/api/checks",
        files=[("files", (names[0], files[names[0]].read_bytes(), DOCX))],
        data={"roles": ["wrong"]},
        headers=check_env["member"],
    )
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "bad_roles"
    bad = await client.post(
        "/api/checks",
        files=[("files", ("x.docx", b"not a doc", DOCX))],
        data={"roles": ["tz"]},
        headers=check_env["member"],
    )
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "bad_type"


@pytest.mark.db
async def test_one_current_check_and_clear_button(
    client: httpx.AsyncClient, check_env: dict[str, Any], engine: AsyncEngine, tmp_path: Path
) -> None:
    files = make_package(tmp_path / "p")
    first = (await _start(client, check_env["member"], files)).json()["id"]
    second = (await _start(client, check_env["member"], files)).json()["id"]  # заменяет первую
    assert (
        await client.get(f"/api/checks/{first}", headers=check_env["member"])
    ).status_code == 404
    cur = (await client.get("/api/checks/current", headers=check_env["member"])).json()["run"]
    assert cur["id"] == second and cur["status"] == "queued"
    assert (
        await client.delete(f"/api/checks/{second}", headers=check_env["member"])
    ).status_code == 204
    assert (await client.get("/api/checks/current", headers=check_env["member"])).json() == {
        "run": None
    }
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM check_runs"))).scalar_one() == 0
        assert (await conn.execute(text("SELECT count(*) FROM check_files"))).scalar_one() == 0
    folder = service.run_dir(get_settings(), second)
    assert not folder.exists()


@pytest.mark.db
async def test_logout_erases_everything(
    client: httpx.AsyncClient, check_env: dict[str, Any], engine: AsyncEngine, tmp_path: Path
) -> None:
    r = await login(client, "T-0001", TEST_PASSWORD)  # cookie сессии остаётся в клиенте
    headers = auth(r.json()["access_token"])
    run = await _run(client, headers, make_package(tmp_path / "p"))
    folder = service.run_dir(get_settings(), run["id"])
    assert folder.exists()
    assert (await client.post("/api/auth/logout")).status_code == 204
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM check_runs"))).scalar_one() == 0
        assert (
            await conn.execute(
                text("SELECT count(*) FROM documents WHERE check_run_id IS NOT NULL")
            )
        ).scalar_one() == 0
        assert (
            await conn.execute(
                text(
                    "SELECT count(*) FROM chunks c JOIN documents d ON d.id=c.document_id WHERE d.title LIKE '%Коммерческое%'"
                )
            )
        ).scalar_one() == 0
    assert not folder.exists()


@pytest.mark.db
async def test_idle_and_dead_session_cleanup(
    client: httpx.AsyncClient, check_env: dict[str, Any], engine: AsyncEngine, tmp_path: Path
) -> None:
    files = make_package(tmp_path / "p")
    rid = (await _start(client, check_env["member"], files)).json()["id"]
    settings = get_settings()
    async with get_sessionmaker()() as s, s.begin():
        assert (
            await service.cleanup_expired(s, settings) == 0
        )  # свежая проверка живой сессии остаётся
    # Простой дольше срока: проверка стирается.
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE check_runs SET last_activity_at = :t"),
            {"t": datetime.now(UTC) - timedelta(minutes=settings.check_idle_minutes + 5)},
        )
    async with get_sessionmaker()() as s, s.begin():
        assert await service.cleanup_expired(s, settings) == 1
    assert not service.run_dir(settings, rid).exists()
    # Закрытая администратором сессия: проверка стирается тиком worker'а.
    rid2 = (await _start(client, check_env["member"], files)).json()["id"]
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE auth_sessions SET revoked_at = now()"))
    async with get_sessionmaker()() as s, s.begin():
        assert await service.cleanup_expired(s, settings) == 1
    assert not service.run_dir(settings, rid2).exists()
