"""Разбор файлов и нарезка на фрагменты — без БД."""

import shutil
from pathlib import Path

import pytest

from app.config import Settings
from app.ingest.assemble import assemble_header, document_caption, header_meta
from app.ingest.blocks import Block
from app.ingest.chunker import chunk_blocks
from app.ingest.errors import ParseError
from app.ingest.files import detect_type, parse_file
from app.ingest.ocr import parse_tsv
from app.ingest.text_blocks import heading_level, merge_continuations, point_of
from app.sources.base import TablePart
from fakes.fake_source import FILES_DIR

SETTINGS = Settings()


def _chunks(blocks: list[Block], target: int = 1500, maximum: int = 2500, kind: str = "text"):  # type: ignore[no-untyped-def]
    return chunk_blocks(blocks, target=target, maximum=maximum, kind=kind)


def test_header_and_table_parts_become_markdown() -> None:
    header = {
        "id": "7c1d",
        "doc_type": "Приказ",
        "number": "112",
        "date": "2024-03-14",
        "title": "О допуске персонала к работам на высоте",
        "topic": "Охрана труда",
        "modified_at": "2024-05-20T08:42:00+00:00",
        "Участок": "Литейный цех № 2",
    }
    rows = [{"document_id": "7c1d", "personnel_number": "20871", "full_name": "Петров В. Н."}]
    blocks = assemble_header(header, [TablePart("Ознакомлены", rows, 1)])
    text = _chunks(blocks, kind="header")[0].text
    assert (
        text.splitlines()[0]
        == "# Приказ № 112 от 14.03.2024 «О допуске персонала к работам на высоте»"
    )
    assert "Тема: Охрана труда" in text and "Участок: Литейный цех № 2" in text
    assert "modified_at" not in text and "7c1d" not in text  # технические поля не индексируем
    assert "## Ознакомлены" in text
    assert "| Таб. № | ФИО |\n|---|---|\n| 20871 | Петров В. Н. |" in text
    meta = header_meta(header)
    assert str(meta.doc_date) == "2024-03-14" and document_caption(meta).startswith("Приказ № 112")


def test_big_table_is_split_by_rows_and_repeats_header() -> None:
    rows = [["№", "Оборудование", "Срок"]] + [
        [str(i), f"Кран КМ-{i}", "1 раз в год"] for i in range(200)
    ]
    chunks = _chunks([Block("table", rows=rows)], target=400, maximum=600)
    assert len(chunks) > 5
    numbers: list[str] = []
    for c in chunks:
        assert len(c.text) <= 600 and c.kind == "table"
        lines = c.text.splitlines()
        assert lines[:2] == ["| № | Оборудование | Срок |", "|---|---|---|"]
        numbers += [ln.split(" | ")[0][2:] for ln in lines[2:]]
    assert numbers == [str(i) for i in range(200)]


def test_sections_points_and_long_paragraphs() -> None:
    blocks = [
        Block("heading", "4. Порядок допуска", level=2),
        Block("text", "4.1. Первый пункт.", page=3),
        Block("heading", "4.2 Наряд-допуск", level=3),
        Block("text", "4.2. " + "Очень длинное предложение про наряд. " * 80, page=3),
        Block("heading", "5. Контроль", level=2),
        Block("text", "5.1. Контроль возложить на главного инженера.", page=4),
    ]
    chunks = _chunks(blocks, target=500, maximum=800)
    assert chunks[0].section == "4. Порядок допуска" and chunks[0].point == "4.1"
    long = [c for c in chunks if c.section == "4. Порядок допуска › 4.2 Наряд-допуск"]
    assert len(long) > 2 and all(len(c.text) <= 800 for c in long)
    assert long[0].point == "4.2" and long[0].page_from == 3
    assert chunks[-1].section == "5. Контроль" and chunks[-1].page_from == 4


def test_heading_and_point_detection() -> None:
    assert heading_level("4. Порядок допуска") == 2
    assert heading_level("4.2 Наряд-допуск") == 3
    assert heading_level("РАЗДЕЛ 2") == 2
    assert heading_level("ОБЩИЕ ПОЛОЖЕНИЯ") == 2
    assert heading_level("4.2. Допуск осуществляется по наряду-допуску.") == 0
    assert point_of("4.2. Допуск…") == "4.2" and point_of("Допуск 4.2") is None
    assert merge_continuations(["3. Назначить", "главного инженера."]) == [
        "3. Назначить главного инженера."
    ]


def test_parse_tsv_joins_lines_and_splits_points() -> None:
    header = "\t".join(
        "level page_num block_num par_num line_num word_num left top width height conf text".split()
    )
    words = [
        (1, 1, 1, "1.", 95),
        (1, 1, 1, "Работы", 92),
        (1, 1, 1, "выпол-", 90),
        (1, 1, 2, "нять", 91),
        (1, 1, 3, "2.", 96),
        (1, 1, 3, "Наряд", 30),
    ]
    tsv = "\n".join(
        [header] + [f"5\t1\t{b}\t{p}\t{ln}\t1\t0\t0\t0\t0\t{c}\t{w}" for b, p, ln, w, c in words]
    )
    res = parse_tsv(tsv)
    assert res.paragraphs == ["1. Работы выполнять", "2. Наряд"]
    assert 80 < res.confidence < 85


def test_pdf_with_text_layer_and_table() -> None:
    kind, parsed = parse_file(FILES_DIR / "prikaz_112.pdf", "prikaz_112.pdf", SETTINGS)
    assert kind == "pdf" and parsed.pages == 2 and parsed.ocr_pages == 0
    tables = [b for b in parsed.blocks if b.kind == "table"]
    assert (
        tables and tables[0].rows[0] == ["Группа", "Кто", "Периодичность"] and tables[0].page == 2
    )
    # Текст таблицы не задваивается обычным текстом страницы.
    assert sum("Мастера, бригадиры" in b.text for b in parsed.blocks) == 1
    assert "--- стр. 2 ---" in parsed.text


def test_docx_headings_and_table_in_order() -> None:
    kind, parsed = parse_file(FILES_DIR / "iot_044.docx", "iot_044.docx", SETTINGS)
    assert kind == "docx"
    kinds = [(b.kind, b.text[:20]) for b in parsed.blocks]
    i_head = kinds.index(("heading", "3. Нормы выдачи СИЗ"))
    assert kinds[i_head + 1][0] == "table"
    assert parsed.blocks[i_head + 1].rows[1] == ["Костюм брезентовый", "1 шт.", "12 месяцев"]


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract не установлен")
def test_scanned_pdf_goes_through_ocr() -> None:
    _, parsed = parse_file(FILES_DIR / "prikaz_45_scan.pdf", "scan.pdf", SETTINGS)
    assert parsed.ocr_pages == 1
    assert "наряду-допуску" in parsed.text


def test_broken_files_give_clear_errors(tmp_path: Path) -> None:
    with pytest.raises(ParseError, match="пустой"):
        parse_file(FILES_DIR / "prikaz_97.pdf", "prikaz_97.pdf", SETTINGS)
    bad_pdf = tmp_path / "bad.pdf"
    bad_pdf.write_bytes(b"%PDF-1.4\n garbage garbage")
    with pytest.raises(ParseError, match="PDF"):
        parse_file(bad_pdf, "bad.pdf", SETTINGS)
    gif = tmp_path / "x.bin"
    gif.write_bytes(b"GIF89a.......")
    assert detect_type(gif) is None
    with pytest.raises(ParseError, match="формат не поддерживается"):
        parse_file(gif, "x.bin", SETTINGS)
    # DOCX под видом PDF определяется по содержимому.
    disguised = tmp_path / "prikaz.pdf"
    disguised.write_bytes((FILES_DIR / "iot_044.docx").read_bytes())
    assert detect_type(disguised) == "docx"
