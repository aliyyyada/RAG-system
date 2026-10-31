"""Нарезка блоков на фрагменты (чанки) с учётом структуры документа.

Правила:
- фрагмент не переходит через заголовок раздела: «4. Порядок допуска» начинает новый фрагмент,
  а сам заголовок запоминается в поле section («4. Порядок допуска › 4.2 Наряд-допуск»);
- абзацы копятся, пока фрагмент меньше chunk_target_chars; длинный абзац режется по
  предложениям, а очень длинное предложение — по словам;
- таблица не режется посередине строки. Маленькая таблица идёт вместе с абзацем перед ней
  (обычно это её подпись), большая делится по строкам, и в каждой части повторяется
  шапка таблицы — иначе в середине таблицы непонятно, что значат колонки;
- у фрагмента запоминаются страницы (для PDF) и первый номер пункта («4.2»).
"""

import math
import re
from dataclasses import dataclass, field

from app.ingest.blocks import Block, table_to_markdown
from app.ingest.text_blocks import point_of

_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+")


@dataclass
class ChunkDraft:
    kind: str  # header | text | table
    text: str
    section: str | None
    point: str | None
    page_from: int | None
    page_to: int | None

    @property
    def token_estimate(self) -> int:
        # bge-m3 (токенизатор XLM-R) на русском тексте даёт ~1 токен на 3–4 символа.
        return max(1, math.ceil(len(self.text) / 3.5))


@dataclass
class _Buffer:
    parts: list[str] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)
    point: str | None = None
    has_table: bool = False

    @property
    def size(self) -> int:
        return sum(len(p) for p in self.parts) + 2 * max(len(self.parts) - 1, 0)

    def add(self, text: str, page: int | None, *, table: bool = False) -> None:
        self.parts.append(text)
        if page is not None:
            self.pages.append(page)
        if self.point is None and not table:
            self.point = point_of(text)
        self.has_table = self.has_table or table


def _split_long(text: str, limit: int) -> list[str]:
    """Длинный абзац → куски не длиннее limit по границам предложений (или слов)."""
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    cur = ""
    for sentence in _SENTENCE_END.split(text):
        if len(sentence) > limit:
            words = sentence.split(" ")
            for w in words:
                if cur and len(cur) + 1 + len(w) > limit:
                    pieces.append(cur)
                    cur = w
                else:
                    cur = f"{cur} {w}" if cur else w
            continue
        if cur and len(cur) + 1 + len(sentence) > limit:
            pieces.append(cur)
            cur = sentence
        else:
            cur = f"{cur} {sentence}" if cur else sentence
    if cur:
        pieces.append(cur)
    return pieces


def _split_table(rows: list[list[str]], limit: int) -> list[str]:
    """Большая таблица → части целыми строками, в каждой части своя шапка."""
    header, *body = rows
    if not body:
        return [table_to_markdown(rows)]
    head_md = table_to_markdown([header])
    parts: list[str] = []
    group: list[list[str]] = []
    size = len(head_md)
    for row in body:
        row_md_len = len(table_to_markdown([row])) + 1
        if group and size + row_md_len > limit:
            parts.append(table_to_markdown([header, *group]))
            group, size = [], len(head_md)
        group.append(row)
        size += row_md_len
    if group:
        parts.append(table_to_markdown([header, *group]))
    return parts


def chunk_blocks(
    blocks: list[Block],
    *,
    target: int,
    maximum: int,
    kind: str = "text",
) -> list[ChunkDraft]:
    """kind='header' — для текста, собранного из шапки источника; иначе text/table."""
    chunks: list[ChunkDraft] = []
    headings: list[tuple[int, str]] = []
    buf = _Buffer()

    def section(hs: list[tuple[int, str]] | None = None) -> str | None:
        # Название документа (уровень 1) в раздел не пишем: оно и так есть у документа.
        hs = headings if hs is None else hs
        names = [t for lvl, t in hs if lvl > 1]
        if not names and kind != "header":
            names = [t for _, t in hs]
        return " › ".join(names) if names else None

    def flush(hs: list[tuple[int, str]] | None = None) -> None:
        nonlocal buf
        if not buf.parts:
            return
        out_kind = kind if kind == "header" else ("table" if buf.has_table else "text")
        chunks.append(
            ChunkDraft(
                kind=out_kind,
                text="\n\n".join(buf.parts),
                section=section(hs),
                point=buf.point,
                page_from=min(buf.pages) if buf.pages else None,
                page_to=max(buf.pages) if buf.pages else None,
            )
        )
        buf = _Buffer()

    for block in blocks:
        if block.kind == "heading":
            level = block.level or 2
            if kind == "header":
                # Шапку источника держим вместе: название документа и его поля — в тексте
                # фрагмента, чтобы поиск «приказ 112» находил его.
                if buf.parts and buf.size >= target:
                    flush()
                buf.add("#" * level + " " + block.text.strip(), block.page)
            else:
                flush()
            headings = [h for h in headings if h[0] < level]
            headings.append((level, block.text.strip()))
            continue

        if block.kind == "table":
            md = block.text
            if buf.size + len(md) + 2 <= maximum:
                buf.add(md, block.page, table=True)
                if buf.size >= target:
                    flush()
                continue
            # Заголовок табличной части («## Ознакомлены») не отрываем от её таблицы.
            carry = None
            if kind == "header" and buf.parts and buf.parts[-1].startswith("#"):
                carry = buf.parts.pop()
            flush(headings[:-1] if carry else None)
            room = maximum - (len(carry) + 2 if carry else 0)
            for i, part in enumerate(_split_table(block.rows, room)):
                if carry and i == 0:
                    buf.add(carry, block.page)
                buf.add(part, block.page, table=True)
                flush()
            continue

        for piece in _split_long(block.text.strip(), maximum):
            if buf.parts and buf.size + len(piece) + 2 > target:
                flush()
            buf.add(piece, block.page)
    flush()

    # Блок из одного заголовка без текста (например, пустой раздел) — не теряем его.
    if not chunks and headings:
        chunks.append(ChunkDraft(kind, headings[0][1], None, None, None, None))
    return chunks
