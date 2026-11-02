"""Разбор сканов-картинок (TIFF, PNG, JPEG): каждая страница распознаётся Tesseract.

Источник в MS SQL отдаёт бинарники как есть: среди PDF и DOCX встречаются сканы,
сохранённые картинкой. Многостраничный TIFF (обычный формат сканера) — это несколько
страниц, поэтому ссылка на источник ведёт на нужную страницу так же, как у PDF.
"""

from pathlib import Path

from PIL import Image, ImageSequence, UnidentifiedImageError

from app.config import Settings
from app.ingest.errors import ParseError
from app.ingest.ocr import LOW_CONFIDENCE, ocr_image
from app.ingest.parsed import ParsedFile
from app.ingest.text_blocks import paragraphs_to_blocks

# Защита от «картинки-бомбы»: скан A4 в 600 dpi — около 35 млн точек.
MAX_PIXELS = 120_000_000
MAX_PAGES = 500


def parse_image(path: Path, settings: Settings) -> ParsedFile:
    result = ParsedFile()
    try:
        with Image.open(path) as img:
            if img.width * img.height > MAX_PIXELS:
                raise ParseError(f"Картинка слишком большая: {img.width}×{img.height} точек")
            dpi = img.info.get("dpi", (settings.ocr_dpi, settings.ocr_dpi))[0] or settings.ocr_dpi
            for page_no, frame in enumerate(ImageSequence.Iterator(img), start=1):
                if page_no > MAX_PAGES:
                    result.warnings.append(f"распознаны только первые {MAX_PAGES} страниц")
                    break
                ocr = ocr_image(
                    frame.copy(), settings.ocr_languages, int(dpi), settings.ocr_timeout_seconds
                )
                result.pages = page_no
                result.ocr_pages += 1
                if sum(len(p) for p in ocr.paragraphs) < 20:
                    result.warnings.append(
                        f"стр. {page_no}: при распознавании почти не найдено текста"
                    )
                elif ocr.confidence < LOW_CONFIDENCE:
                    result.warnings.append(
                        f"стр. {page_no}: скан низкого качества "
                        f"(уверенность OCR {ocr.confidence:.0f} %)"
                    )
                result.blocks.extend(paragraphs_to_blocks(ocr.paragraphs, page_no))
    except ParseError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ParseError(f"Картинка не открывается: {type(exc).__name__}: {exc}"[:500]) from exc
    if not any(b.text.strip() for b in result.blocks):
        raise ParseError("На скане не найдено текста", stage="ocr")
    return result
