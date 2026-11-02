"""Разбор старого Word (.doc, Word 97–2003): текст, заголовки, таблицы.

В источнике MS SQL встречаются документы в .doc (ответ разработчиков источника от
02.11.2026). Конвертер (LibreOffice) не берём: это большой образ и лицензия MPL. Формат
.doc описан Microsoft открыто ([MS-DOC]), а для текста нужна только его часть, поэтому
читаем его сами. Контейнер OLE открывает olefile (BSD).

Как устроен .doc (только то, что нам нужно):

    WordDocument — основной поток: заголовок FIB и сам текст кусками («pieces»);
    0Table/1Table — поток таблиц: в нём
        Clx       — таблица кусков: символы [cp, cp+n) лежат в WordDocument с позиции fc,
                    в UTF-16 или однобайтно (cp1252);
        PlcBtePapx — где лежат свойства абзацев (страницы FKP по 512 байт в WordDocument).

Из свойств абзаца берём два признака:
    istd 1–9           — встроенный стиль «Заголовок 1–9»;
    sprmPFTtp (0x2417) — абзац-«конец строки таблицы».

Служебные символы текста: \\r — конец абзаца, \\x07 — конец ячейки (или строки таблицы,
если абзац помечен TTP), \\x13…\\x14…\\x15 — поле (берём только результат, после \\x14),
\\x0b — перенос строки, \\x0c — разрыв страницы, \\x1e — неразрывный дефис.
Страниц в .doc нет (их считает Word при показе), как и в DOCX: page=None.
"""

import bisect
import struct
from dataclasses import dataclass
from pathlib import Path

import olefile

from app.ingest.blocks import Block, clean_rows
from app.ingest.errors import ParseError
from app.ingest.parsed import ParsedFile
from app.ingest.text_blocks import heading_level

FIB_MAGIC = 0xA5EC
# Смещения в FIB (Word 97+): флаги, длина основного текста, пары fc/lcb.
_OFF_FLAGS = 0x0A
_OFF_CCP_TEXT = 0x4C
_OFF_FC_PLCF_BTE_PAPX = 0x0102
_OFF_FC_CLX = 0x01A2
_F_ENCRYPTED = 0x0100
_F_WHICH_TBL = 0x0200
_SPRM_IN_TABLE = 0x2416
_SPRM_TTP = 0x2417
_SPRM_INNER_TTP = 0x244C  # конец строки вложенной таблицы
_SPRM_ITAP = 0x6649  # глубина вложенности таблицы (> 0 — абзац в таблице)


@dataclass(frozen=True)
class _Piece:
    cp: int
    cp_end: int
    fc: int
    compressed: bool


def _u16(b: bytes, off: int) -> int:
    return int(struct.unpack_from("<H", b, off)[0])


def _u32(b: bytes, off: int) -> int:
    return int(struct.unpack_from("<I", b, off)[0])


def _pieces(table: bytes, fc_clx: int, lcb_clx: int) -> list[_Piece]:
    clx = table[fc_clx : fc_clx + lcb_clx]
    i = 0
    while i < len(clx) and clx[i] == 0x01:  # Prc: форматирование, пропускаем
        i += 3 + struct.unpack_from("<h", clx, i + 1)[0]
    if i >= len(clx) or clx[i] != 0x02:
        raise ParseError("DOC: не найдена таблица кусков текста")
    lcb = _u32(clx, i + 1)
    plc = clx[i + 5 : i + 5 + lcb]
    n = (lcb - 4) // 12
    cps = [_u32(plc, k * 4) for k in range(n + 1)]
    out = []
    for k in range(n):
        fc_raw = _u32(plc, (n + 1) * 4 + k * 8 + 2)
        compressed = bool(fc_raw & 0x40000000)
        fc = (fc_raw & ~0x40000000) // 2 if compressed else fc_raw
        out.append(_Piece(cps[k], cps[k + 1], fc, compressed))
    return out


def _paragraph_marks(word: bytes, table: bytes, fc_bte: int, lcb_bte: int) -> dict[int, int]:
    """Конец абзаца (fc после последнего символа) → (istd << 2) | (в таблице << 1) | TTP."""
    marks: dict[int, int] = {}
    if lcb_bte < 4:
        return marks
    n = (lcb_bte - 4) // 8
    plc = table[fc_bte : fc_bte + lcb_bte]
    for k in range(n):
        pn = _u32(plc, (n + 1) * 4 + k * 4) & 0x3FFFFF
        page = word[pn * 512 : pn * 512 + 512]
        if len(page) < 512:
            continue
        crun = page[511]
        for r in range(crun):
            fc_end = _u32(page, (r + 1) * 4)
            b_off = page[(crun + 1) * 4 + r * 13]
            if b_off == 0:
                continue
            p = b_off * 2
            cb = page[p]
            if cb == 0:
                size = page[p + 1] * 2
                start = p + 2
            else:
                size = cb * 2 - 1
                start = p + 1
            papx = page[start : start + size]
            if len(papx) < 2:
                continue
            istd = _u16(papx, 0)
            in_table, ttp = _table_flags(papx[2:])
            marks[fc_end] = (istd << 2) | (int(in_table or ttp) << 1) | int(ttp)
    return marks


_SPRA_SIZE = {0: 1, 1: 1, 2: 2, 3: 4, 4: 2, 5: 2, 7: 3}


def _table_flags(grpprl: bytes) -> tuple[bool, bool]:
    """(абзац в таблице, абзац — конец строки таблицы)."""
    in_table = ttp = False
    i = 0
    while i + 2 <= len(grpprl):
        sprm = _u16(grpprl, i)
        i += 2
        spra = sprm >> 13
        if spra == 6:  # операнд переменной длины
            if i >= len(grpprl):
                break
            size = grpprl[i] + 1
            if sprm == 0xD608:  # sprmTDefTable: длина 2 байта
                size = _u16(grpprl, i) + 1 if i + 1 < len(grpprl) else 1
        else:
            size = _SPRA_SIZE.get(spra, 1)
        if i < len(grpprl) and grpprl[i]:
            if sprm in (_SPRM_TTP, _SPRM_INNER_TTP):
                ttp = True
            elif sprm in (_SPRM_IN_TABLE, _SPRM_ITAP):
                in_table = True
        i += size
    return in_table, ttp


@dataclass
class _Char:
    ch: str
    fc_end: int  # позиция в WordDocument сразу после символа


def _chars(word: bytes, pieces: list[_Piece], ccp_text: int) -> list[_Char]:
    out: list[_Char] = []
    for pc in pieces:
        if pc.cp >= ccp_text:
            break
        count = min(pc.cp_end, ccp_text) - pc.cp
        if pc.compressed:
            raw = word[pc.fc : pc.fc + count]
            text = raw.decode("cp1252", errors="replace")
            out += [_Char(c, pc.fc + i + 1) for i, c in enumerate(text)]
        else:
            raw = word[pc.fc : pc.fc + count * 2]
            text = raw.decode("utf-16-le", errors="replace")
            out += [_Char(c, pc.fc + (i + 1) * 2) for i, c in enumerate(text)]
    return out


_REPLACE = {"\x0b": " ", "\x0c": " ", "\x1e": "-", "\x1f": "", "\xa0": " ", "\t": " "}


class _Builder:
    """Собирает блоки: абзацы вне таблиц, строки таблиц из ячеек."""

    def __init__(self) -> None:
        self.blocks: list[Block] = []
        self.buf: list[str] = []
        self.cell_parts: list[str] = []  # абзацы текущей ячейки
        self.cells: list[str] = []  # готовые ячейки текущей строки
        self.rows: list[list[str]] = []

    def _take(self) -> str:
        text = " ".join("".join(self.buf).split())
        self.buf = []
        return text

    def paragraph(self, istd: int, in_table: bool) -> None:
        text = self._take()
        if in_table:  # абзац внутри ячейки: ячейка продолжается
            if text:
                self.cell_parts.append(text)
            return
        self.flush_table()
        if not text:
            return
        level = istd + 1 if 1 <= istd <= 5 else heading_level(text)
        self.blocks.append(Block("heading" if level else "text", text, level=level))

    def cell_end(self, row_end: bool) -> None:
        text = self._take()
        if row_end:
            if self.cells:
                self.rows.append(self.cells)
            self.cells, self.cell_parts = [], []
            return
        self.cells.append(" ".join([*self.cell_parts, text]).strip())
        self.cell_parts = []

    def flush_table(self) -> None:
        if self.cells:
            self.rows.append(self.cells)
            self.cells = []
        if self.rows:
            rows = clean_rows([list(r) for r in self.rows])
            if rows:
                self.blocks.append(Block("table", rows=rows))
        self.rows = []


def parse_doc(path: Path) -> ParsedFile:
    if path.stat().st_size == 0:
        raise ParseError("DOC не открывается: файл пустой (0 байт)")
    try:
        with olefile.OleFileIO(str(path)) as ole:
            if not ole.exists("WordDocument"):
                raise ParseError("Файл OLE, но не документ Word (.doc): возможно, .xls или .msg")
            word = ole.openstream("WordDocument").read()
            if len(word) < 0x01AA or _u16(word, 0) != FIB_MAGIC:
                raise ParseError("DOC не открывается: повреждён заголовок документа")
            flags = _u16(word, _OFF_FLAGS)
            if flags & _F_ENCRYPTED:
                raise ParseError("DOC защищён паролем — текст недоступен")
            tname = "1Table" if flags & _F_WHICH_TBL else "0Table"
            if not ole.exists(tname):
                raise ParseError(f"DOC не открывается: нет потока {tname}")
            table = ole.openstream(tname).read()
    except ParseError:
        raise
    except (OSError, ValueError, struct.error) as exc:
        raise ParseError(f"DOC не открывается: {type(exc).__name__}: {exc}"[:500]) from exc

    try:
        ccp_text = _u32(word, _OFF_CCP_TEXT)
        fc_clx, lcb_clx = _u32(word, _OFF_FC_CLX), _u32(word, _OFF_FC_CLX + 4)
        fc_bte, lcb_bte = (
            _u32(word, _OFF_FC_PLCF_BTE_PAPX),
            _u32(word, _OFF_FC_PLCF_BTE_PAPX + 4),
        )
        pieces = _pieces(table, fc_clx, lcb_clx)
        marks = _paragraph_marks(word, table, fc_bte, lcb_bte)
        chars = _chars(word, pieces, ccp_text)
    except (struct.error, IndexError) as exc:
        raise ParseError(f"DOC не открывается: повреждена структура ({exc})"[:500]) from exc

    mark_ends = sorted(marks)

    def props(fc_end: int) -> int:
        """Свойства абзаца, в который входит символ, кончающийся на fc_end."""
        i = bisect.bisect_left(mark_ends, fc_end)
        return marks[mark_ends[i]] if i < len(mark_ends) else 0

    b = _Builder()
    skip_code: list[bool] = []  # внутри поля: True — код поля (до \x14), не выводим
    for c in chars:
        ch = c.ch
        if ch == "\x13":
            skip_code.append(True)
            continue
        if ch == "\x14":
            if skip_code:
                skip_code[-1] = False
            continue
        if ch == "\x15":
            if skip_code:
                skip_code.pop()
            continue
        if skip_code and skip_code[-1]:
            continue
        if ch == "\r":
            p = props(c.fc_end)
            b.paragraph(p >> 2, in_table=bool(p & 2))
        elif ch == "\x07":
            b.cell_end(row_end=bool(props(c.fc_end) & 1))
        elif ch in ("\x01", "\x08", "\x05", "\x02", "\x03", "\x04"):
            continue  # картинки, объекты, сноски — без текста
        else:
            b.buf.append(_REPLACE.get(ch, ch))
    if b.buf:
        b.paragraph(0, in_table=False)
    b.flush_table()

    result = ParsedFile(blocks=b.blocks)
    if not any(x.text.strip() for x in result.blocks):
        raise ParseError("В DOC нет текста")
    return result
