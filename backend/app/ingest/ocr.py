"""OCR сканов через Tesseract (Apache-2.0), языки rus+eng.

Tesseract — отдельная программа, установленная в образ backend (см. Dockerfile).
Запускаем её на изображение страницы и просим вывод в TSV: в нём каждое слово со
своей уверенностью (0–100). По средней уверенности предупреждаем о плохом скане.
"""

import io
import shutil
import subprocess  # noqa: S404 — запускаем только tesseract с фиксированными аргументами
from dataclasses import dataclass

from PIL import Image

from app.ingest.errors import ParseError
from app.ingest.text_blocks import point_of

# Ниже этого — «скан низкого качества» в предупреждениях документа.
LOW_CONFIDENCE = 60.0


@dataclass
class OcrResult:
    paragraphs: list[str]
    confidence: float  # средняя уверенность по словам, 0–100

    @property
    def text(self) -> str:
        return "\n\n".join(self.paragraphs)


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def ocr_image(image: Image.Image, languages: str, dpi: int, timeout: int) -> OcrResult:
    exe = shutil.which("tesseract")
    if exe is None:
        raise ParseError("OCR недоступен: в образе не установлен tesseract", stage="ocr")
    buf = io.BytesIO()
    image.convert("L").save(buf, format="PNG")
    try:
        proc = subprocess.run(  # noqa: S603 — аргументы фиксированы, без shell
            [exe, "stdin", "stdout", "-l", languages, "--dpi", str(dpi), "--psm", "3", "tsv"],
            input=buf.getvalue(),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ParseError(f"OCR не уложился в {timeout} с на страницу", stage="ocr") from None
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ParseError(f"OCR завершился с ошибкой: {err[-1] if err else proc.returncode}", "ocr")
    return parse_tsv(proc.stdout.decode("utf-8", "replace"))


def parse_tsv(tsv: str) -> OcrResult:
    """TSV Tesseract → абзацы. Строки абзаца склеиваем, перенос слова со «-» убираем."""
    paragraphs: dict[tuple[int, int], list[list[str]]] = {}
    confs: list[float] = []
    lines = tsv.splitlines()
    for line in lines[1:]:
        cols = line.split("\t")
        if len(cols) < 12 or cols[0] != "5":
            continue
        word = cols[11].strip()
        if not word:
            continue
        try:
            conf = float(cols[10])
        except ValueError:
            conf = -1
        if conf >= 0:
            confs.append(conf)
        block, par, line_no = int(cols[2]), int(cols[3]), int(cols[4])
        rows = paragraphs.setdefault((block, par), [])
        while len(rows) <= line_no:
            rows.append([])
        rows[line_no].append(word)

    out: list[str] = []
    for key in sorted(paragraphs):
        text = ""
        for words in paragraphs[key]:
            if not words:
                continue
            chunk = " ".join(words)
            if text and point_of(chunk) is not None:
                # Tesseract часто склеивает пункты «1. … 2. …» в один абзац — разделяем.
                out.append(text)
                text = ""
            if text.endswith("-") and len(text) > 1 and text[-2].isalpha():
                text = text[:-1] + chunk
            else:
                text = f"{text} {chunk}" if text else chunk
        if text:
            out.append(text)
    confidence = sum(confs) / len(confs) if confs else 0.0
    return OcrResult(paragraphs=out, confidence=confidence)
