"""Абзацы простого текста (из PDF или OCR) → блоки с заголовками и пунктами.

Заголовки в PDF не размечены, поэтому распознаём их по признакам:
- «РАЗДЕЛ 4», «Глава 2», «Приложение 1» в начале строки;
- короткая строка вида «4. Порядок допуска» (номер с точкой, текст с заглавной, без точки
  в конце) — раздел; «4.2. Допуск…» с длинным текстом — это пункт, а не заголовок;
- строка целиком ЗАГЛАВНЫМИ буквами до 120 символов — заголовок.
"""

import re

from app.ingest.blocks import Block

SECTION_WORD = re.compile(r"^(раздел|глава|часть|приложение)\s+[\dIVXLА-Я]", re.IGNORECASE)
_NUMBERED_TITLE = re.compile(r"^(\d{1,2})\.\s+([А-ЯЁA-Z][^.;:]{2,100})$")
_SUBNUMBERED_TITLE = re.compile(r"^(\d{1,2}\.\d{1,2})\.?\s+([А-ЯЁA-Z][^.;:]{2,100})$")
_POINT = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,3}){0,3})[.)]?\s+\S")


def heading_level(line: str) -> int:
    """0 — не заголовок, 2 — раздел, 3 — подраздел."""
    text = line.strip()
    if not text or len(text) > 120:
        return 0
    if SECTION_WORD.match(text):
        return 2
    if _NUMBERED_TITLE.match(text):
        return 2
    if _SUBNUMBERED_TITLE.match(text):
        return 3
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 4 and all(c.isupper() for c in letters) and not text.endswith(","):
        return 2
    return 0


def point_of(text: str) -> str | None:
    """«4.2. Допуск к работам…» → «4.2»."""
    m = _POINT.match(text)
    return m.group(1) if m else None


def merge_continuations(paragraphs: list[str]) -> list[str]:
    """Абзац, начинающийся со строчной буквы, — продолжение предыдущего (разрыв строки скана)."""
    out: list[str] = []
    for para in paragraphs:
        text = para.strip()
        if not text:
            continue
        if out and text[0].islower() and not out[-1].endswith((".", "!", "?", ":")):
            out[-1] = f"{out[-1]} {text}"
        else:
            out.append(text)
    return out


def paragraphs_to_blocks(paragraphs: list[str], page: int | None) -> list[Block]:
    blocks: list[Block] = []
    for text in merge_continuations(paragraphs):
        level = heading_level(text)
        if level:
            blocks.append(Block("heading", text, page=page, level=level))
        else:
            blocks.append(Block("text", text, page=page))
    return blocks
