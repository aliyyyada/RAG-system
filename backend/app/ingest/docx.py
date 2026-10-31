"""Разбор DOCX (python-docx, MIT): абзацы, заголовки по стилям, таблицы по порядку.

В DOCX нет страниц (их считает Word при показе), поэтому у блоков page=None,
а место в документе задаётся разделом (заголовком) и номером пункта.
"""

import zipfile
from pathlib import Path

from docx import Document as open_docx
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.ingest.blocks import Block, clean_rows
from app.ingest.errors import ParseError
from app.ingest.parsed import ParsedFile
from app.ingest.text_blocks import heading_level


def _style_level(paragraph: Paragraph) -> int:
    name = (paragraph.style.name if paragraph.style is not None else "") or ""
    lowered = name.lower()
    if lowered in ("title", "название", "заголовок"):
        return 1
    for prefix in ("heading ", "заголовок "):
        if lowered.startswith(prefix):
            digits = lowered[len(prefix) :].strip()
            if digits.isdigit():
                return min(int(digits) + 1, 6)
    return 0


def parse_docx(path: Path) -> ParsedFile:
    if path.stat().st_size == 0:
        raise ParseError("DOCX не открывается: файл пустой (0 байт)")
    try:
        doc = open_docx(str(path))
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise ParseError(f"DOCX не открывается: {type(exc).__name__}: {exc}"[:500]) from exc
    except Exception as exc:  # noqa: BLE001 — повреждённый XML и т. п.
        raise ParseError(f"DOCX не открывается: {type(exc).__name__}"[:500]) from exc

    result = ParsedFile()
    for el in doc.element.body.iterchildren():
        if el.tag == qn("w:p"):
            p = Paragraph(el, doc)
            text = p.text.strip()
            if not text:
                continue
            level = _style_level(p) or heading_level(text)
            result.blocks.append(Block("heading" if level else "text", text, level=level))
        elif el.tag == qn("w:tbl"):
            table = Table(el, doc)
            rows = clean_rows([[cell.text for cell in row.cells] for row in table.rows])
            if rows:
                result.blocks.append(Block("table", rows=rows))
    if not any(b.text.strip() for b in result.blocks):
        raise ParseError("В DOCX нет текста")
    return result
