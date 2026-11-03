"""Данные из документов пакета: реквизиты, позиции, цены, условия договора, таблица расчёта НМЦ.

Всё делается правилами (регулярные выражения и разбор таблиц), без модели: быстро и воспроизводимо.
Результат сохраняется в `check_files.extracted`, чтобы критерии и интерфейс видели одни и те же
значения. Если из документа что-то не извлеклось (скан плохого качества, нестандартная форма),
соответствующий критерий получит статус «Нужна ручная проверка», а не неверный вердикт.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from epoz.checks.pkg import FileData
from epoz.checks.textutil import (
    INN_RE,
    KPP_RE,
    ORG_RE,
    first,
    items_from_tables,
    money_near,
    norm_ws,
    parse_date,
    parse_ru_number,
    sq,
)

OKPD_CTX = re.compile(r"ОКПД\s*2?\s*(?:\([^)]*\))?\s*[:№\-–]?\s*(\d{2}(?:\.\d{1,3}){0,3})", re.I)
OKPD_FULL = re.compile(r"(?<![\d.])(\d{2}\.\d{2}\.\d{2}(?:\.\d{3})?)(?![\d.])")
DAYS_RE = re.compile(
    r"(?<!\d)(\d{1,4})\s*(?:\([^)]{2,60}\)\s*)?(рабоч\w+|календарн\w+|банковск\w+)?\s*(?:рабоч\w+\s+|календарн\w+\s+)?дн",
    re.I,
)
SIGN_RE = re.compile(r"подпис|м\.\s?п\.|печат", re.I)


def okpd_codes(text: str) -> list[str]:
    seen: list[str] = []
    for m in list(OKPD_CTX.finditer(text)) + list(OKPD_FULL.finditer(text)):
        code = m.group(1)
        if code not in seen:
            seen.append(code)
    return seen


def payment_clauses(text: str) -> list[str]:
    """Предложения договора про оплату."""
    sentences = re.split(r"(?<=[.;])\s+|\n+", text)
    return [
        norm_ws(s)
        for s in sentences
        if re.search(r"оплат|расч[её]т(ы)? по договору|платеж", s, re.I) and len(s) > 15
    ]


def payment_days(text: str) -> dict[str, Any] | None:
    """Срок оплаты в днях из предложений про оплату: (дни, рабочие/календарные, цитата)."""
    for s in payment_clauses(text):
        for m in DAYS_RE.finditer(s):
            kind_raw = (m.group(2) or "").lower()
            kind = "рабочих" if kind_raw.startswith("рабоч") else "календарных"
            if kind_raw.startswith("банковск"):
                kind = "рабочих"
            # срок оплаты — это «в течение N дней», а не срок действия: оставляем первый
            return {"days": int(m.group(1)), "kind": kind, "quote": s[:300]}
    return None


def contract_subject(text: str) -> str | None:
    m = re.search(
        r"предмет\s+договора\s*[:.]?\s*(.{20,600}?)(?:\n\s*\n|\n\s*\d+\.\s|$)", text, re.I | re.S
    )
    if m:
        return norm_ws(m.group(1))[:400]
    m = re.search(r"(поставщик\s+обязуется[^.]{10,400}\.)", text, re.I)
    return norm_ws(m.group(1)) if m else None


def tz_subject(text: str) -> str | None:
    m = re.search(r"техническое\s+задание\s+(?:на|по)\s+([^\n.]{5,200})", text, re.I)
    if m:
        return norm_ws(m.group(1))
    m = re.search(
        r"(?:предмет\s+закупки|наименование\s+(?:товара|закупки|работ|услуг))\s*[:\-–]\s*([^\n]{5,200})",
        text,
        re.I,
    )
    return norm_ws(m.group(1)) if m else None


def supplier_of(text: str) -> str | None:
    head = text[:1500]
    m = first(
        r"(?:поставщик|исполнитель|подрядчик|наименование организации|от)\s*[:\-–]?\s*((?:ООО|АО|ПАО|ЗАО|НАО|ОАО|ИП)[^\n,;]{2,90})",
        head,
    )
    if m:
        return norm_ws(m.group(1))
    m = ORG_RE.search(head)
    return norm_ws(m.group(1)) if m else None


def _num(v: object) -> Decimal | None:
    return parse_ru_number(str(v)) if v is not None else None


def nmc_table(tables: list[list[list[str]]]) -> dict[str, Any] | None:
    """Таблица расчёта НМЦ методом анализа рынка (форма 1.1): позиции, цены поставщиков,
    средняя арифметическая цена и общая стоимость. None — такой таблицы нет."""
    for table in tables:
        if len(table) < 2:
            continue
        head_cells = [
            norm_ws(" ".join(row[j] for row in table[:2] if j < len(row)))
            for j in range(max(len(r) for r in table[:2]))
        ]
        low = [sq(c) for c in head_cells]
        avg = next((j for j, c in enumerate(low) if "средн" in c), None)
        total = next((j for j, c in enumerate(low) if "общаястоимость" in c or "итого" in c), None)
        if avg is None or total is None:
            continue
        qty = next((j for j, c in enumerate(low) if re.search(r"кол-?во|количеств", c)), None)
        name = next((j for j, c in enumerate(low) if "наименован" in c), 0)
        vat = next((j for j, c in enumerate(low) if "ндс" in c), None)
        start = max(x for x in (qty, vat, name) if x is not None) + 1
        price_cols = list(range(start, avg))
        if not price_cols:
            continue
        rows: list[dict[str, Any]] = []
        grand: Decimal | None = None
        for row in table[1:]:
            cells = row + [""] * (len(low) - len(row))
            label = norm_ws(cells[name]) if name < len(cells) else ""
            if re.fullmatch(r"[\d\s.]*", "".join(cells).replace("…", "")) and not label:
                continue  # строка нумерации столбцов «1 2 3 …» или пустая
            prices = [p for p in (_num(cells[j]) for j in price_cols) if p is not None]
            avg_v, total_v = _num(cells[avg]), _num(cells[total])
            if re.match(r"\s*(итого|всего|нмц|общая)", label, re.I):
                grand = total_v if total_v is not None else grand
                continue
            if not label and not prices:
                continue
            rows.append(
                {
                    "name": label,
                    "qty": str(_num(cells[qty]))
                    if qty is not None and _num(cells[qty]) is not None
                    else None,
                    "prices": [str(p) for p in prices],
                    "avg": str(avg_v) if avg_v is not None else None,
                    "total": str(total_v) if total_v is not None else None,
                }
            )
        if rows:
            return {
                "rows": rows,
                "grand_total": str(grand) if grand is not None else None,
                "columns": len(price_cols),
            }
    return None


def extract_file(fd: FileData) -> dict[str, Any]:
    """Данные одного файла по его роли (и общие реквизиты)."""
    if not fd.ok:
        return {}
    text = fd.text
    out: dict[str, Any] = {}
    inns = [m.group(1) for m in INN_RE.finditer(text)]
    if inns:
        out["inn"] = inns[0]
        out["inns"] = list(dict.fromkeys(inns))
    kpp = KPP_RE.search(text)
    if kpp:
        out["kpp"] = kpp.group(1)
    date = parse_date(text[:2500])
    if date:
        out["date"] = date
    num = first(r"(?:№|N)\s*([A-Za-zА-Яа-я0-9][\w\-/]{0,24})", text[:1500])
    if num:
        out["number"] = num.group(1)
    out["signature_mention"] = bool(SIGN_RE.search(text))
    items, grand = items_from_tables(fd.tables, money=fd.role not in ("tz", "note"))
    if items:
        out["items"] = items
    if grand is not None:
        out["table_total"] = str(grand)
    codes = okpd_codes(text)
    if codes:
        out["okpd2"] = codes
    if fd.role == "tz":
        out["subject"] = tz_subject(text)
    elif fd.role == "contract":
        out["subject"] = contract_subject(text)
        price = money_near(
            text,
            r"(цена\s+договора|общая\s+(?:цена|сумма)|сумма\s+договора|стоимость\s+(?:товара|договора|работ|услуг))",
        )
        if price is not None:
            out["price"] = str(price)
        pd = payment_days(text)
        if pd:
            out["payment"] = pd
        out["supplier"] = supplier_of(text)
    elif fd.role == "kp":
        out["supplier"] = supplier_of(text)
        total = money_near(
            text, r"(итого|всего|общая\s+(?:цена|стоимость|сумма)|сумма\s+предложения)"
        )
        if total is not None:
            out["total"] = str(total)
        out["vat"] = bool(re.search(r"ндс", text, re.I))
        pd = payment_days(text)
        if pd:
            out["payment"] = pd
        dl = first(
            r"срок\w*\s+(?:поставки|выполнения|оказания)[^\d]{0,60}(\d{1,3})\s*(?:\([^)]*\)\s*)?(рабоч\w+|календарн\w+)?\s*дн",
            text,
        )
        if dl:
            out["delivery_days"] = int(dl.group(1))
    elif fd.role == "nmc":
        nt = nmc_table(fd.tables)
        if nt:
            out["nmc_table"] = nt
        total = money_near(text, r"(?:нмц|начальная\s+\(максимальная\)\s+цена)[^\d]{0,80}")
        if total is not None:
            out["nmc_total"] = str(total)
        m = first(
            r"(сопоставимых рыночных цен|анализа рынка|нормативн\w+ метод|тарифн\w+ метод|проектно-сметн\w+|затратн\w+ метод)",
            text,
        )
        if m:
            out["method"] = m.group(1)
    elif fd.role == "request":
        orgs = list(dict.fromkeys(norm_ws(m.group(1)) for m in ORG_RE.finditer(text)))
        out["orgs"] = orgs[:20]
    return out
