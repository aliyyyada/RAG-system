"""Результат разбора одного файла."""

from dataclasses import dataclass, field

from app.ingest.blocks import Block


@dataclass
class ParsedFile:
    blocks: list[Block] = field(default_factory=list)
    pages: int | None = None
    ocr_pages: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        """Распознанный текст целиком (вкладка «Распознанный текст»)."""
        out: list[str] = []
        page: int | None = None
        for b in self.blocks:
            if b.page is not None and b.page != page:
                page = b.page
                out.append(f"--- стр. {page} ---")
            out.append(("#" * max(b.level, 1) + " " + b.text) if b.kind == "heading" else b.text)
        return "\n\n".join(out)
