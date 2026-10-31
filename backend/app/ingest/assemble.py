"""Сборка документа из строк источника: шапка + табличные части → структурированный текст.

Пример. Источник отдал шапку
    {"id": "7c1d…", "doc_type": "Приказ", "number": "112", "date": "2024-03-14",
     "title": "О допуске персонала к работам на высоте", "topic": "Охрана труда",
     "department": "Служба ОТ и ПБ"}
и табличную часть «Ознакомлены» на 46 строк. Получаем блоки:

    # Приказ № 112 от 14.03.2024 «О допуске персонала к работам на высоте»
    Тема: Охрана труда
    Подразделение: Служба ОТ и ПБ
    ## Ознакомлены
    | Таб. № | ФИО | Дата ознакомления |
    |---|---|---|
    | 20871 | Петров В. Н. | 15.03.2024 |

Этот текст индексируется как отдельные фрагменты (kind=header) вместе с текстом файлов.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from app.ingest.blocks import Block, clean_rows
from app.sources.base import TablePart

# Подписи известных полей шапки. Неизвестные поля выводятся с исходным именем:
# если источник добавит поле, оно попадёт в поиск без доработки кода.
FIELD_LABELS: dict[str, str] = {
    "doc_type": "Вид документа",
    "number": "Номер",
    "date": "Дата",
    "title": "Заголовок",
    "topic": "Тема",
    "department": "Подразделение",
    "author": "Автор",
    "status": "Статус",
    "description": "Описание",
    "personnel_number": "Таб. №",
    "full_name": "ФИО",
    "position": "Должность",
    "acquainted_at": "Дата ознакомления",
    "responsible": "Ответственный",
    "deadline": "Срок",
}
# Технические поля: в текст для поиска не идут.
TECHNICAL_FIELDS = {"id", "guid", "modified_at", "deleted_at", "created_at", "version"}
# Поля, которые уже есть в заголовке документа.
TITLE_FIELDS = {"doc_type", "number", "date", "title", "description"}


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


@dataclass
class HeaderMeta:
    number: str | None
    title: str
    doc_type: str | None
    topic: str | None
    doc_date: date | None


def _str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    text = _str(value)
    if not text:
        return None
    for parse in (date.fromisoformat, lambda s: datetime.fromisoformat(s).date()):
        try:
            return parse(text)
        except ValueError:
            pass
    try:
        return datetime.strptime(text, "%d.%m.%Y").date()
    except ValueError:
        return None


def header_meta(header: dict[str, Any]) -> HeaderMeta:
    return HeaderMeta(
        number=(_str(header.get("number")) or "")[:100] or None,
        title=_str(header.get("title")) or "",
        doc_type=(_str(header.get("doc_type")) or "")[:100] or None,
        topic=(_str(header.get("topic")) or "")[:200] or None,
        doc_date=parse_date(header.get("date")),
    )


def document_caption(meta: HeaderMeta) -> str:
    """«Приказ № 112 от 14.03.2024 «О допуске…»» — так документ называется везде в интерфейсе."""
    parts: list[str] = []
    if meta.doc_type:
        parts.append(meta.doc_type)
    if meta.number:
        parts.append(f"№ {meta.number}")
    if meta.doc_date:
        parts.append(f"от {meta.doc_date:%d.%m.%Y}")
    head = " ".join(parts)
    if meta.title:
        return f"{head} «{meta.title}»" if head else meta.title
    return head or "Документ без названия"


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, list):
        return ", ".join(_format_value(v) for v in value)
    if isinstance(value, dict):
        return "; ".join(f"{k}: {_format_value(v)}" for k, v in value.items())
    if isinstance(value, str) and _ISO_DATE.match(value):
        d = parse_date(value)
        if d is not None:
            return f"{d:%d.%m.%Y}"
    return str(value).strip()


def part_to_rows(part: TablePart) -> list[list[str]]:
    """Строки табличной части → таблица. Колонки — объединение ключей в порядке появления."""
    columns: list[str] = []
    for row in part.rows:
        for key in row:
            if key not in columns and key not in TECHNICAL_FIELDS and key != "document_id":
                columns.append(key)
    header = [FIELD_LABELS.get(c, c) for c in columns]
    body = [
        [_format_value(row.get(c)) if row.get(c) is not None else "" for c in columns]
        for row in part.rows
    ]
    return clean_rows([header, *body])


def assemble_header(header: dict[str, Any], parts: list[TablePart]) -> list[Block]:
    meta = header_meta(header)
    blocks = [Block("heading", document_caption(meta), level=1)]

    fields = []
    for key, value in header.items():
        if key in TECHNICAL_FIELDS or key in TITLE_FIELDS or value in (None, "", [], {}):
            continue
        fields.append(f"{FIELD_LABELS.get(key, key)}: {_format_value(value)}")
    if fields:
        blocks.append(Block("text", "\n".join(fields)))
    description = _str(header.get("description"))
    if description:
        blocks.append(Block("text", description))

    for part in parts:
        if not part.rows:
            continue
        blocks.append(Block("heading", part.name, level=2))
        blocks.append(Block("table", rows=part_to_rows(part)))
    return blocks
