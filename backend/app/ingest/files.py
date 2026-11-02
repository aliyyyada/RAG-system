"""Определение типа файла по содержимому и разбор в блоки.

Тип определяем по первым байтам, а не по расширению: источник может прислать
«приказ.pdf», который на деле DOCX, или файл без расширения (бинарники из MS SQL
приходят вообще без имени). Поддерживаются PDF, DOCX, старый Word (.doc)
и сканы-картинки TIFF/PNG/JPEG.
"""

import zipfile
from pathlib import Path
from typing import Literal

from app.config import Settings
from app.ingest.doc import parse_doc
from app.ingest.docx import parse_docx
from app.ingest.errors import ParseError
from app.ingest.image import parse_image
from app.ingest.parsed import ParsedFile
from app.ingest.pdf import parse_pdf

FileType = Literal["pdf", "docx", "doc", "tiff", "png", "jpeg"]

MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "tiff": "image/tiff",
    "png": "image/png",
    "jpeg": "image/jpeg",
}
EXTENSIONS = {
    "pdf": "pdf",
    "docx": "docx",
    "doc": "doc",
    "tiff": "tif",
    "png": "png",
    "jpeg": "jpg",
}

# Составной документ OLE: старые .doc (а также .xls, .msg — их не разбираем).
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
    if kind == "ole":
        return "doc"  # не Word (.xls и т. п.) — понятная ошибка в parse_doc
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
        raise ParseError(
            f"Файл «{name}»: формат не поддерживается (нужен PDF, DOCX, DOC или скан TIFF/PNG/JPEG)"
        )
    if kind == "pdf":
        return kind, parse_pdf(path, settings)
    if kind == "docx":
        return kind, parse_docx(path)
    if kind == "doc":
        return kind, parse_doc(path)
    return kind, parse_image(path, settings)
