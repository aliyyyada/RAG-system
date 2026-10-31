"""Разбор PDF: текстовый слой (pdfplumber, MIT), таблицы, OCR сканов.

На каждой странице:
1. Ищем таблицы. Их текст выводим Markdown-таблицей, а из обычного текста страницы
   выкидываем символы, попавшие внутрь таблицы (иначе таблица задвоится).
2. Строки текста склеиваем в абзацы: новый абзац — по большому вертикальному отступу,
   по номеру пункта в начале строки («4.2.») или после заголовка.
3. Если на странице почти нет текста (скан), рисуем её в картинку и распознаём Tesseract.
Блоки страницы идут в порядке сверху вниз, таблицы — на своём месте среди абзацев.
"""

import logging
import statistics
from pathlib import Path
from typing import Any

import pdfplumber
from pdfminer.pdfparser import PDFSyntaxError

from app.config import Settings
from app.ingest.blocks import Block, clean_rows
from app.ingest.errors import ParseError
from app.ingest.ocr import LOW_CONFIDENCE, ocr_image
from app.ingest.parsed import ParsedFile
from app.ingest.text_blocks import SECTION_WORD as _SECTION_START
from app.ingest.text_blocks import heading_level, paragraphs_to_blocks, point_of

log = logging.getLogger(__name__)

# pdfminer очень разговорчив на битых файлах.
logging.getLogger("pdfminer").setLevel(logging.ERROR)


def _inside(obj: dict[str, Any], boxes: list[tuple[float, float, float, float]]) -> bool:
    cx = (obj["x0"] + obj["x1"]) / 2
    cy = (obj["top"] + obj["bottom"]) / 2
    return any(x0 <= cx <= x1 and top <= cy <= bottom for x0, top, x1, bottom in boxes)


def _lines_to_paragraphs(lines: list[dict[str, Any]]) -> list[tuple[float, str, bool]]:
    """Строки страницы → (top, текст абзаца, крупный шрифт?)."""
    if not lines:
        return []
    heights = [ln["bottom"] - ln["top"] for ln in lines if ln["bottom"] > ln["top"]]
    line_h = statistics.median(heights) if heights else 12.0
    sizes = [c.get("size", 0) for ln in lines for c in ln.get("chars", [])]
    body_size = statistics.median(sizes) if sizes else 0

    out: list[tuple[float, str, bool]] = []
    cur: list[str] = []
    cur_top = 0.0
    cur_big = False
    prev_bottom: float | None = None
    for ln in lines:
        text = ln["text"].strip()
        if not text:
            continue
        size = statistics.median([c.get("size", 0) for c in ln.get("chars", [])] or [0])
        big = body_size > 0 and size >= body_size * 1.15
        gap = ln["top"] - prev_bottom if prev_bottom is not None else 0
        # Заголовок это или нет, решаем по абзацу целиком (_parse_page): первая строка
        # длинного пункта «1.1. Настоящий приказ устанавливает…» без точки похожа на заголовок.
        new_par = (
            not cur
            or gap > line_h * 0.9
            or point_of(text) is not None
            or big != cur_big
            or _SECTION_START.match(text) is not None
        )
        if new_par and cur:
            out.append((cur_top, _join(cur), cur_big))
            cur = []
        if not cur:
            cur_top, cur_big = ln["top"], big
        cur.append(text)
        prev_bottom = ln["bottom"]
    if cur:
        out.append((cur_top, _join(cur), cur_big))
    return out


def _join(lines: list[str]) -> str:
    text = ""
    for line in lines:
        if text.endswith("-") and len(text) > 1 and text[-2].isalpha():
            text = text[:-1] + line
        else:
            text = f"{text} {line}" if text else line
    return text


def parse_pdf(path: Path, settings: Settings) -> ParsedFile:
    if path.stat().st_size == 0:
        raise ParseError("PDF не открывается: файл пустой (0 байт)")
    result = ParsedFile()
    try:
        with pdfplumber.open(path) as pdf:
            result.pages = len(pdf.pages)
            for page_no, page in enumerate(pdf.pages, start=1):
                _parse_page(page, page_no, settings, result)
    except ParseError:
        raise
    except (PDFSyntaxError, ValueError, KeyError, TypeError, OSError) as exc:
        raise ParseError(f"PDF не открывается: {type(exc).__name__}: {exc}"[:500]) from exc
    except Exception as exc:  # noqa: BLE001 — pdfminer бросает что угодно на битых файлах
        raise ParseError(f"PDF не открывается: {type(exc).__name__}"[:500]) from exc
    if not any(b.text.strip() for b in result.blocks):
        raise ParseError("В PDF не найдено текста (ни текстового слоя, ни распознанного)")
    return result


def _parse_page(page: Any, page_no: int, settings: Settings, result: ParsedFile) -> None:
    tables = page.find_tables()
    boxes = [t.bbox for t in tables]
    body = page.filter(lambda o: not _inside(o, boxes)) if boxes else page
    lines = body.extract_text_lines(return_chars=True)
    text_chars = sum(len(ln["text"].strip()) for ln in lines)

    if text_chars < settings.ocr_min_chars and not tables:
        _ocr_page(page, page_no, settings, result)
        return

    items: list[tuple[float, Block]] = []
    for top, text, big in _lines_to_paragraphs(lines):
        level = heading_level(text) or (2 if big and len(text) <= 150 else 0)
        kind = "heading" if level else "text"
        items.append((top, Block(kind, text, page=page_no, level=level)))
    for t in tables:
        rows = clean_rows(t.extract())
        if rows:
            items.append((t.bbox[1], Block("table", rows=rows, page=page_no)))
    items.sort(key=lambda x: x[0])
    result.blocks.extend(b for _, b in items)


def _ocr_page(page: Any, page_no: int, settings: Settings, result: ParsedFile) -> None:
    image = page.to_image(resolution=settings.ocr_dpi).original
    ocr = ocr_image(image, settings.ocr_languages, settings.ocr_dpi, settings.ocr_timeout_seconds)
    result.ocr_pages += 1
    chars = sum(len(p) for p in ocr.paragraphs)
    if chars < 20:
        result.warnings.append(f"стр. {page_no}: при распознавании почти не найдено текста")
    elif ocr.confidence < LOW_CONFIDENCE:
        result.warnings.append(
            f"стр. {page_no}: скан низкого качества (уверенность OCR {ocr.confidence:.0f} %)"
        )
    result.blocks.extend(paragraphs_to_blocks(ocr.paragraphs, page_no))
