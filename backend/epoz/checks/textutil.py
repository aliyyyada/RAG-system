"""Разбор текста документов пакета: числа, даты, таблицы, токены для сопоставления, цитаты."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
INN_RE = re.compile(r"ИНН\s*[:№]?\s*(\d{10}|\d{12})\b", re.I)
KPP_RE = re.compile(r"КПП\s*[:№]?\s*(\d{9})\b", re.I)
OKPD_RE = re.compile(r"\b(\d{2}\.\d{1,2}(?:\.\d{1,2}(?:\.\d{1,3})?)?)\b")
ORG_RE = re.compile(
    r"((?:ООО|АО|ПАО|ЗАО|НАО|ОАО|ИП|ФГУП|ГУП|МУП|АНО)\s*[«\"“]?[A-Za-zА-Яа-яЁё0-9][^\n,;]{1,80}?[»\"”]?)(?=\s*(?:,|;|\n|\(|ИНН|КПП|ОГРН|$))",
)


def _clean_number_text(s: str) -> str:
    s = s.replace("\xa0", " ").replace("−", "-").strip()
    # «1 234 567,89» → «1234567,89»: пробел между группами цифр — разделитель тысяч
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"(?<=\d) (?=\d{3}(?!\d))", "", s)
    return s


def parse_ru_number(value: str | None) -> Decimal | None:
    """«1 234 567,89 руб.» → Decimal('1234567.89'); None, если числа нет."""
    if not value:
        return None
    s = _clean_number_text(str(value))
    m = re.search(r"-?\d+(?:[.,]\d+)?", s)
    if not m:
        return None
    try:
        return Decimal(m.group(0).replace(",", "."))
    except InvalidOperation:
        return None


def money_near(text: str, pattern: str, *, window: int = 120) -> Decimal | None:
    """Сумма в рублях после слов `pattern` (например, «Итого», «цена договора»)."""
    for m in re.finditer(pattern, text, re.I):
        tail = text[m.end() : m.end() + window]
        for num in re.finditer(r"\d[\d\s ]*(?:[.,]\d{1,2})?", tail):
            v = parse_ru_number(num.group(0))
            if v is not None and v >= 1:
                return v
    return None


def md_tables(text: str) -> list[list[list[str]]]:
    """Таблицы Markdown (как их собирает разбор файлов) → списки строк из ячеек."""
    tables: list[list[list[str]]] = []
    cur: list[list[str]] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|") and s.endswith("|") and len(s) > 2:
            cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s)[1:-1]]
            if cells and all(re.fullmatch(r":?-{3,}:?", c) for c in cells if c):
                continue  # строка-разделитель |---|---|
            cur.append(cells)
        else:
            if cur:
                tables.append(cur)
                cur = []
    if cur:
        tables.append(cur)
    return tables


def stem(word: str) -> str:
    return word.lower().replace("ё", "е")[:5]


def tokens(text: str) -> set[str]:
    """Грубые «основы» слов (первые 5 букв) для сопоставления названий без морфологии."""
    return {stem(w) for w in re.findall(r"[A-Za-zА-Яа-яЁё]{3,}", text or "")}


def overlap(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def snippet(text: str, pos: int, width: int = 160) -> str:
    start = max(0, pos - width // 4)
    return norm_ws(text[start : start + width]) + ("…" if start + width < len(text) else "")


def first(pattern: str, text: str, flags: int = re.I) -> re.Match[str] | None:
    return re.search(pattern, text, flags)


def parse_date(text: str) -> str | None:
    m = DATE_RE.search(text or "")
    return f"{int(m.group(1)):02d}.{int(m.group(2)):02d}.{m.group(3)}" if m else None


def fmt_money(v: Decimal | float | None) -> str:
    if v is None:
        return "—"
    d = Decimal(str(v)).quantize(Decimal("0.01"))
    s = f"{d:,.2f}".replace(",", " ").replace(".", ",")
    return s


def sq(cell: str) -> str:
    """Шапка без пробелов и в нижнем регистре: PDF переносит слова внутри узких ячеек
    («Наименова ние», «Кол- во»), поэтому признаки столбцов ищем в «склеенном» тексте."""
    return re.sub(r"\s+", "", (cell or "").lower().replace("ё", "е"))


# Слова-признаки для таблиц позиций (ищутся в склеенной шапке, см. sq)
_COL = {
    "name": re.compile(r"наименован|товар|работ|услуг|позици|описание|предмет", re.I),
    "qty": re.compile(r"кол-?во|количеств|объем", re.I),
    "unit": re.compile(r"ед\.?изм|единиц|^ед\.?$", re.I),
    "price": re.compile(r"цена|стоимостьза", re.I),
    "total": re.compile(r"сумма|стоимост|итого|всего", re.I),
    "vat": re.compile(r"ндс", re.I),
}


def _is_total_row(name: str) -> bool:
    return bool(re.match(r"\s*(итого|всего|в том числе|общая|нмц|сумма)", name, re.I))


def items_from_tables(
    tables: list[list[list[str]]], *, money: bool = True
) -> tuple[list[dict[str, object]], Decimal | None]:
    """Позиции (наименование, количество, единица, цена, сумма) из таблиц документа и общий итог.

    money=True (КП, договор): в шапке должны быть «наименование» и «цена» или «сумма».
    money=False (ТЗ, записка): достаточно «наименование» и «количество»."""
    for table in tables:
        if len(table) < 2:
            continue
        head = table[0]
        idx: dict[str, int] = {}
        for j, cell in enumerate(head):
            for key in ("name", "qty", "unit", "vat", "price", "total"):
                if key not in idx and _COL[key].search(sq(cell)):
                    # «Цена за единицу» — цена, а не сумма: «стоимость за» проверяется выше
                    idx[key] = j
                    break
        if "name" not in idx:
            continue
        if money and not ({"price", "total"} & set(idx)):
            continue
        if not money and "qty" not in idx:
            continue
        items: list[dict[str, object]] = []
        grand: Decimal | None = None
        for row in table[1:]:
            name = norm_ws(row[idx["name"]]) if idx["name"] < len(row) else ""
            if _is_total_row(name) or (not name and any(row)):
                for key in ("total", "price"):
                    if key in idx and idx[key] < len(row):
                        v = parse_ru_number(row[idx[key]])
                        if v is not None and (name or "").lower().startswith(
                            ("итого", "всего", "общая", "сумма")
                        ):
                            grand = v
                continue
            if not name or re.fullmatch(r"\d+\.?", name):
                continue
            item: dict[str, object] = {"name": name}
            for key in ("qty", "price", "total"):
                if key in idx and idx[key] < len(row):
                    v = parse_ru_number(row[idx[key]])
                    item[key] = str(v) if v is not None else None
            if "unit" in idx and idx["unit"] < len(row):
                item["unit"] = norm_ws(row[idx["unit"]]) or None
            items.append(item)
        if items:
            return items, grand
    return [], None
