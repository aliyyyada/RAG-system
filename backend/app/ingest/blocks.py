"""Блок — кусок структуры документа: заголовок, абзац или таблица.

Все разборщики (шапка источника, PDF, DOCX, OCR) выдают одинаковый список блоков,
а нарезка на фрагменты (chunker.py) работает только с ним. Пример для PDF приказа:

    Block("heading", "4. Порядок допуска", page=3, level=2)
    Block("text", "4.2. Допуск к работам на высоте осуществляется по наряду-допуску…", page=3)
    Block("table", rows=[["Группа", "Кто", "Периодичность"], ["1", "Работники…", "1 раз в 3 года"]])
"""

import re
from dataclasses import dataclass, field
from typing import Literal

BlockKind = Literal["heading", "text", "table"]


@dataclass
class Block:
    kind: BlockKind
    text: str = ""
    page: int | None = None
    # Уровень заголовка: 1 — название документа, 2 — раздел, 3 — подраздел.
    level: int = 0
    # Для таблиц: строки ячеек, первая строка — шапка таблицы.
    rows: list[list[str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.kind == "table" and not self.text:
            self.text = table_to_markdown(self.rows)


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace("|", "\\|")


def clean_rows(rows: list[list[object]]) -> list[list[str]]:
    """Ячейки → строки без переводов строк; пустые строки таблицы выбрасываем."""
    out = [[_cell(c) for c in row] for row in rows]
    out = [row for row in out if any(row)]
    if not out:
        return out
    width = max(len(r) for r in out)
    return [r + [""] * (width - len(r)) for r in out]


def table_to_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    header, *body = rows
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)
