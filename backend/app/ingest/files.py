"""Определение типа файла по содержимому и разбор в блоки.

Тип определяем по первым байтам, а не по расширению: источник может прислать
«приказ.pdf», который на деле DOCX, или файл без расширения.
"""

import zipfile
from pathlib import Path
from typing import Literal

from app.config import Settings
from app.ingest.docx import parse_docx
from app.ingest.errors import ParseError
from app.ingest.parsed import ParsedFile
from app.ingest.pdf import parse_pdf

FileType = Literal["pdf", "docx"]

MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def detect_type(path: Path) -> FileType | None:
    with path.open("rb") as f:
        head = f.read(8)
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK"):
        try:
            with zipfile.ZipFile(path) as z:
                if "word/document.xml" in z.namelist():
                    return "docx"
        except zipfile.BadZipFile:
            return None
    return None


def parse_file(path: Path, name: str, settings: Settings) -> tuple[FileType, ParsedFile]:
    if path.stat().st_size == 0:
        raise ParseError(f"Файл «{name}» пустой (0 байт)")
    kind = detect_type(path)
    if kind is None:
        # Расширение подскажет, что это было, — для понятного сообщения.
        if name.lower().endswith(".pdf"):
            raise ParseError(f"PDF «{name}» не открывается: файл повреждён")
        if name.lower().endswith(".docx"):
            raise ParseError(f"DOCX «{name}» не открывается: файл повреждён")
        raise ParseError(f"Файл «{name}»: формат не поддерживается (нужен PDF или DOCX)")
    if kind == "pdf":
        return kind, parse_pdf(path, settings)
    return kind, parse_docx(path)
