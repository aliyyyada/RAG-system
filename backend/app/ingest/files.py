"""Определение типа файла по содержимому и разбор в блоки.

Тип определяем по первым байтам, а не по расширению: источник может прислать
«приказ.pdf», который на деле DOCX, или файл без расширения (бинарники из MS SQL
приходят вообще без имени). Поддерживаются PDF, DOCX и сканы-картинки TIFF/PNG/JPEG.
Старый Word (.doc) узнаём, но пока не разбираем — понятная ошибка в отчёте.
"""

import zipfile
from pathlib import Path
from typing import Literal

from app.config import Settings
from app.ingest.docx import parse_docx
from app.ingest.errors import ParseError
from app.ingest.image import parse_image
from app.ingest.parsed import ParsedFile
from app.ingest.pdf import parse_pdf

FileType = Literal["pdf", "docx", "tiff", "png", "jpeg"]

MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "tiff": "image/tiff",
    "png": "image/png",
    "jpeg": "image/jpeg",
}
EXTENSIONS = {"pdf": "pdf", "docx": "docx", "tiff": "tif", "png": "png", "jpeg": "jpg"}

# Составной документ OLE: старые .doc, .xls, .msg. Разбирать их пока нечем.
OLE_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")


def sniff(head: bytes) -> str | None:
    """Тип по первым байтам, без открытия файла целиком. "zip" — надо смотреть внутрь."""
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(OLE_MAGIC):
        return "ole"
    if head.startswith(b"PK"):
        return "zip"
    return None


def detect_type(path: Path) -> FileType | None:
    with path.open("rb") as f:
        head = f.read(8)
    kind = sniff(head)
    if kind in ("pdf", "tiff", "png", "jpeg"):
        return kind  # type: ignore[return-value]
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
        with path.open("rb") as f:
            if f.read(8).startswith(OLE_MAGIC):
                raise ParseError(
                    f"Файл «{name}»: старый формат Word/Excel (.doc/.xls) пока не поддерживается"
                )
        # Расширение подскажет, что это было, — для понятного сообщения.
        if name.lower().endswith(".pdf"):
            raise ParseError(f"PDF «{name}» не открывается: файл повреждён")
        if name.lower().endswith(".docx"):
            raise ParseError(f"DOCX «{name}» не открывается: файл повреждён")
        raise ParseError(
            f"Файл «{name}»: формат не поддерживается (нужен PDF, DOCX или скан TIFF/PNG/JPEG)"
        )
    if kind == "pdf":
        return kind, parse_pdf(path, settings)
    if kind == "docx":
        return kind, parse_docx(path)
    return kind, parse_image(path, settings)
