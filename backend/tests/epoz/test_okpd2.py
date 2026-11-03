"""Проверка ЕПоЗ, этап 2: справочник ОКПД2 из Excel.

Файл в тестах синтетический, но устроен как настоящий: сверху название, редакция («Введено
01.11.2026») и число записей, затем таблица «№ / Код / Название»; у кодов в конце пробел.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from openpyxl import Workbook

from epoz.okpd2 import normalize_code, parse_okpd2_xlsx

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Настоящие коды, на которых проверяем цепочку «класс → … → полный код».
REAL = [
    ("26", "Оборудование компьютерное, электронное и оптическое"),
    ("26.2", "Компьютеры и периферийное оборудование"),
    ("26.20", "Компьютеры и периферийное оборудование"),
    ("26.20.1", "Компьютеры и периферийное оборудование"),
    ("26.20.11", "Компьютеры портативные массой не более 10 кг, такие как ноутбуки"),
    ("26.20.11.110", "Ноутбуки, в том числе портативные в защищённом исполнении"),
]


def make_okpd_xlsx(
    path: Path, *, extra_rows: int = 600, edition: str = "149/2026", total: int | None = None
) -> Path:
    wb = Workbook()
    ws = wb.active
    rows: list[tuple[str, str]] = list(REAL)
    for i in range(extra_rows):  # синтетические полные коды без пересечения с настоящими
        rows.append(
            (
                f"7{i // 100}.{i // 10 % 10}{i % 10}.{i % 90 + 10}.{100 + i % 800}",
                f"Услуга синтетическая {i}",
            )
        )
    ws.append(
        ["ОКПД 2 — Общероссийский классификатор продукции по видам экономической деятельности"]
    )
    ws.append(
        [
            f"Классификатор ОК 034-2014 (КПЕС 2008) с изменением № {edition}. Введено 01.11.2026 . Приказ Росстандарта"
        ]
    )
    ws.append([f"Всего записей — {total if total is not None else len(rows)}."])
    ws.append(["Подготовлено сайтом classifikators.ru 02.11.2026"])
    ws.append([])
    ws.append(["№", "Код", "Название"])
    for n, (code, name) in enumerate(rows, 1):
        ws.append([str(n), code + " ", name])  # пробел в конце кода, как в настоящем файле
    ws.append(["", "ЭТО НЕ КОД", "мусорная строка"])
    wb.save(str(path))
    return path


def test_normalize_code() -> None:
    assert normalize_code("ОКПД2 26.20.11.110 ") == "26.20.11.110"
    assert normalize_code("26") == "26"
    assert normalize_code("нет кода") == ""


def test_parse_xlsx(tmp_path: Path) -> None:
    parsed = parse_okpd2_xlsx(make_okpd_xlsx(tmp_path / "o.xlsx"))
    assert parsed.title == "Классификатор ОК 034-2014 (КПЕС 2008) с изменением № 149/2026"
    assert parsed.effective_date is not None and parsed.effective_date.isoformat() == "2026-11-01"
    assert parsed.declared_total == len(parsed.rows)
    assert parsed.skipped == 1  # мусорная строка
    assert (
        "26.20.11.110",
        "Ноутбуки, в том числе портативные в защищённом исполнении",
    ) in parsed.rows  # пробел обрезан


def test_parse_rejects_non_okpd(tmp_path: Path) -> None:
    from epoz.okpd2 import Okpd2Error

    wb = Workbook()
    wb.active.append(["Имя", "Телефон"])
    wb.save(str(tmp_path / "x.xlsx"))
    with pytest.raises(Okpd2Error, match="Не найдены колонки"):
        parse_okpd2_xlsx(tmp_path / "x.xlsx")


async def _import(client: httpx.AsyncClient, headers: Any, path: Path) -> httpx.Response:
    return await client.post(
        "/api/admin/okpd2/import",
        files={"file": (path.name, await asyncio.to_thread(path.read_bytes), XLSX)},
        headers=headers,
    )


@pytest.mark.db
async def test_import_lookup_and_search(
    client: httpx.AsyncClient, epoz_env: dict[str, Any], tmp_path: Path
) -> None:
    h = epoz_env["admin"]
    r = await _import(client, h, make_okpd_xlsx(tmp_path / "o.xlsx"))
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["status"] == "imported" and rep["records"] == len(REAL) + 600
    assert rep["by_level"]["1"] == 1 and rep["by_level"]["6"] >= 601  # полные коды (9 знаков)
    # Цепочка: код и его предки.
    chain = (
        await client.get(
            "/api/admin/okpd2/lookup", params={"code": "ОКПД2 26.20.11.110"}, headers=h
        )
    ).json()
    assert [c["code"] for c in chain["chain"]] == [
        "26",
        "26.2",
        "26.20",
        "26.20.1",
        "26.20.11",
        "26.20.11.110",
    ]
    # Несуществующий код.
    none = (
        await client.get("/api/admin/okpd2/lookup", params={"code": "99.99.99.999"}, headers=h)
    ).json()
    assert none["found"] is False
    # Поиск по словам названия с морфологией: «ноутбук» находит «Ноутбуки».
    hits = (
        await client.get("/api/admin/okpd2/lookup", params={"q": "ноутбук защищённый"}, headers=h)
    ).json()
    assert hits["hits"][0]["code"] == "26.20.11.110"
    # Повторная загрузка той же редакции ничего не дублирует.
    again = (await _import(client, h, make_okpd_xlsx(tmp_path / "o2.xlsx"))).json()
    assert again["status"] == "unchanged" and again["edition_id"] == rep["edition_id"]
    # Новая редакция становится действующей, прежняя остаётся.
    new = (
        await _import(
            client, h, make_okpd_xlsx(tmp_path / "o3.xlsx", edition="150/2027", extra_rows=550)
        )
    ).json()
    assert new["status"] == "imported" and new["edition_id"] != rep["edition_id"]
    status = (await client.get("/api/admin/okpd2", headers=h)).json()
    assert status["active"]["id"] == new["edition_id"]


@pytest.mark.db
async def test_import_errors_and_rights(
    client: httpx.AsyncClient, epoz_env: dict[str, Any], tmp_path: Path
) -> None:
    h = epoz_env["admin"]
    small = make_okpd_xlsx(tmp_path / "small.xlsx", extra_rows=10)
    r = await _import(client, h, small)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "too_few"
    r = await client.post(
        "/api/admin/okpd2/import", files={"file": ("x.xlsx", b"not excel", XLSX)}, headers=h
    )
    assert r.status_code == 422 and r.json()["detail"]["code"] == "bad_file"
    # Ничего не осталось от неудачных загрузок.
    assert (await client.get("/api/admin/okpd2", headers=h)).json()["active"] is None
    # Обычный пользователь и гость справочник не загружают.
    for headers in (epoz_env["member"], None):
        r = await client.get("/api/admin/okpd2", headers=headers)
        assert r.status_code in (401, 403)
