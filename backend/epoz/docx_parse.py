"""Разбор Word-файлов ЕПоЗ (Положение и приложения) с восстановлением нумерации пунктов.

Зачем отдельный разбор. В Положении почти все абзацы (3255 из 3285) нумеруются автоматически:
в самом тексте файла номера «10.3» или «6.6.2» нет, их подставляет Word при показе. Обычный
разбор DOCX (`app/ingest/docx.py`) читает только текст абзацев, поэтому номера пунктов терял бы,
а критерии проверки ссылаются именно на пункты («п. 10.3 Положения», «п. 6.6 Приложения 5»).

Как восстанавливаем номера (два способа, выбирается по файлу):

1. **По оглавлению** (`method="toc"`). Оглавление Word хранит номера заголовков в виде обычного
   текста («10.3<TAB>Требования к продукции<TAB>98»): их Word уже вычислил сам. Заголовки в теле
   документа сопоставляются с записями оглавления по порядку и названию, а номера пунктов
   считаются внутри заголовка: «10.3» → «10.3.1», «10.3.2»… Подпункты — «(1)», «(а)».
   Способ не зависит от внутреннего устройства списков Word (в Положении раздел «1.» и
   остальные разделы лежат в разных списках, которые Word сам склеивает в сквозную нумерацию).
2. **Эмуляция нумерации Word** (`method="numbering"`) для файлов без оглавления (например,
   Приложение 5): читаем `numbering.xml`, ведём счётчики уровней с учётом переопределений
   стартового номера.

Проверка результата (`ParsedEpoz.report`): сколько заголовков сопоставлено с оглавлением и какая
доля ссылок вида «п. 11.3.1» внутри файла находит пункт с таким номером. Если доля мала,
администратор увидит это в отчёте черновика редакции и не сделает её действующей по ошибке.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from docx import Document as open_docx
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.ingest.blocks import Block, clean_rows
from app.ingest.errors import ParseError
from app.ingest.text_blocks import heading_level, point_of

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

_STYLE_LEVEL = re.compile(r"\(Уровень\s*(\d)\)")
_HEADING_STYLE = re.compile(r"^(heading|заголовок)\s*(\d)$", re.IGNORECASE)
_TOC_LINE = re.compile(r"^(?P<head>.*?)\t(?P<page>\d{1,4})\s*$", re.DOTALL)
_REF = re.compile(r"п(?:п)?\.\s*(\d{1,2}(?:\.\d{1,3}){1,3})(?![\d.]*\d)", re.IGNORECASE)
_NUM_ONLY = re.compile(r"^\d+(?:\.\d+)*\.?$")
_RUSSIAN_LOWER = "абвгдежзиклмнопрстуфхцчшщэюя"


@dataclass
class Clause:
    """Пункт документа: то, на что ссылаются критерии проверки."""

    label: str  # «10.3.4», «6.2.1(1)» или «» у текста без номера
    level: int  # 1 глава, 2 раздел, 3 подраздел, 4 пункт, 5 подпункт, 6 подподпункт, 0 текст
    text: str
    is_heading: bool = False


@dataclass
class ParsedEpoz:
    blocks: list[Block] = field(default_factory=list)
    clauses: list[Clause] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        out: list[str] = []
        for b in self.blocks:
            out.append(("#" * max(b.level, 1) + " " + b.text) if b.kind == "heading" else b.text)
        return "\n\n".join(out)


def norm(s: str) -> str:
    s = s.replace("\xa0", " ").replace("\t", " ")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s.rstrip(".").strip()


# ---------------------------------------------------------------------------
# Уровни абзацев по стилям
# ---------------------------------------------------------------------------


def style_level(name: str) -> int | None:
    """«[Ростех] Текст Пункта (Уровень 4)» → 4; «Heading 2» → 2; иначе None."""
    m = _STYLE_LEVEL.search(name or "")
    if m:
        return int(m.group(1))
    m = _HEADING_STYLE.match((name or "").strip())
    if m:
        return int(m.group(2))
    return None


# ---------------------------------------------------------------------------
# Способ 1: заголовки по оглавлению
# ---------------------------------------------------------------------------


@dataclass
class TocEntry:
    number: str  # «10.3», «10», «Глава 5.» или ""
    title: str
    key: str  # нормализованное название для сопоставления


def parse_toc(paragraphs: list[Paragraph]) -> list[TocEntry]:
    entries: list[TocEntry] = []
    for p in paragraphs:
        sname = (p.style.name if p.style is not None else "") or ""
        if not sname.lower().startswith("toc"):
            continue
        raw = p.text.replace("\r", "")
        m = _TOC_LINE.match(raw)
        head = (m.group("head") if m else raw).strip()
        if not head:
            continue
        if "\t" in head:
            number, title = head.split("\t", 1)
            number, title = number.strip(), title.strip()
        else:
            mm = re.match(r"^(Глава\s+\d+\.?|\d+(?:\.\d+)*\.?)\s+(.+)$", head)
            number, title = (mm.group(1), mm.group(2)) if mm else ("", head)
        entries.append(TocEntry(number.strip(), title, norm(title)))
    return entries


def _align(
    headings: list[tuple[int, str]], toc: list[TocEntry], window: int = 6
) -> dict[int, TocEntry]:
    """Сопоставляет заголовки тела документа (индекс → запись оглавления) по порядку и названию."""
    result: dict[int, TocEntry] = {}
    j = 0
    for idx, title in headings:
        key = norm(title)
        for k in range(j, min(j + window, len(toc))):
            if toc[k].key == key:
                result[idx] = toc[k]
                j = k + 1
                break
    return result


# ---------------------------------------------------------------------------
# Способ 2: эмуляция нумерации Word
# ---------------------------------------------------------------------------


def _fmt_number(n: int, kind: str) -> str:
    if kind in ("decimal", "decimalZero"):
        return str(n)
    if kind == "lowerLetter":
        return chr(ord("a") + (n - 1) % 26)
    if kind == "upperLetter":
        return chr(ord("A") + (n - 1) % 26)
    if kind == "russianLower":
        return _RUSSIAN_LOWER[(n - 1) % len(_RUSSIAN_LOWER)]
    if kind == "russianUpper":
        return _RUSSIAN_LOWER[(n - 1) % len(_RUSSIAN_LOWER)].upper()
    if kind in ("lowerRoman", "upperRoman"):
        vals = [(10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]
        out, x = "", n
        for v, s in vals:
            while x >= v:
                out += s
                x -= v
        return out.upper() if kind == "upperRoman" else out
    return str(n)


class NumberingEmulator:
    """Восстанавливает видимый номер абзаца по numbering.xml (счётчики по уровням списка)."""

    def __init__(self, path: Path, doc: Any) -> None:
        self.levels: dict[str, dict[int, dict[str, Any]]] = {}
        self.nums: dict[str, tuple[str, dict[int, int]]] = {}
        self.style_num: dict[str, tuple[str | None, int]] = {}
        self.counters: dict[str, list[int]] = {}
        self.started: set[tuple[str, int]] = set()
        try:
            with zipfile.ZipFile(path) as z:
                if "word/numbering.xml" not in z.namelist():
                    return
                # файл загружает администратор; expat не ходит по внешним сущностям
                root = ET.fromstring(z.read("word/numbering.xml"))  # noqa: S314
        except (zipfile.BadZipFile, ET.ParseError):
            return
        for a in root.findall(W + "abstractNum"):
            lv: dict[int, dict[str, Any]] = {}
            for lvl in a.findall(W + "lvl"):
                i = int(lvl.get(W + "ilvl", "0"))

                def val(tag: str, default: str = "", lvl: Any = lvl) -> str:
                    el = lvl.find(W + tag)
                    return el.get(W + "val", default) if el is not None else default

                lv[i] = {
                    "start": int(val("start", "1") or 1),
                    "fmt": val("numFmt", "decimal"),
                    "text": val("lvlText"),
                    "restart": val("lvlRestart", ""),
                }
            self.levels[a.get(W + "abstractNumId", "")] = lv
        for n in root.findall(W + "num"):
            abstract = n.find(W + "abstractNumId")
            overrides: dict[int, int] = {}
            for o in n.findall(W + "lvlOverride"):
                so = o.find(W + "startOverride")
                if so is not None:
                    overrides[int(o.get(W + "ilvl", "0"))] = int(so.get(W + "val", "1"))
            if abstract is not None:
                self.nums[n.get(W + "numId", "")] = (abstract.get(W + "val", ""), overrides)
        for st in doc.styles:
            el = st.element
            npr = el.find(qn("w:pPr") + "/" + qn("w:numPr")) if el is not None else None
            if npr is not None:
                nid = npr.find(qn("w:numId"))
                il = npr.find(qn("w:ilvl"))
                self.style_num[st.name] = (
                    nid.get(qn("w:val")) if nid is not None else None,
                    int(il.get(qn("w:val"))) if il is not None else 0,
                )

    def peek(self, p: Paragraph) -> tuple[str, int] | None:
        """(формат номера, уровень списка) у нумерованного абзаца, не двигая счётчики."""
        npr = p._p.find(qn("w:pPr") + "/" + qn("w:numPr"))
        if npr is None:
            return None
        n = npr.find(qn("w:numId"))
        lv = npr.find(qn("w:ilvl"))
        nid = n.get(qn("w:val")) if n is not None else None
        il = int(lv.get(qn("w:val"))) if lv is not None else 0
        if not nid or nid == "0" or nid not in self.nums:
            return None
        level = self.levels.get(self.nums[nid][0], {}).get(il)
        return (level["fmt"], il) if level else None

    def label(self, p: Paragraph) -> tuple[str, int | None]:
        """(видимый номер, уровень списка) или ("", None), если абзац не нумерован."""
        npr = p._p.find(qn("w:pPr") + "/" + qn("w:numPr"))
        nid: str | None = None
        il = 0
        if npr is not None:
            n = npr.find(qn("w:numId"))
            lv = npr.find(qn("w:ilvl"))
            nid = n.get(qn("w:val")) if n is not None else None
            il = int(lv.get(qn("w:val"))) if lv is not None else 0
        elif p.style is not None and p.style.name in self.style_num:
            nid, il = self.style_num[p.style.name]
        if not nid or nid == "0" or nid not in self.nums:
            return "", None
        abstract_id, overrides = self.nums[nid]
        levels = self.levels.get(abstract_id, {})
        if il not in levels:
            return "", None
        counters = self.counters.setdefault(abstract_id, [0] * 10)
        if il in overrides and (nid, il) not in self.started:
            counters[il] = overrides[il]
            self.started.add((nid, il))
        else:
            counters[il] = levels[il]["start"] if counters[il] == 0 else counters[il] + 1
        for j in range(il + 1, 10):
            if levels.get(j, {}).get("restart") != "0":
                counters[j] = 0
        text = levels[il]["text"]
        if not text or levels[il]["fmt"] in ("bullet", "none"):
            return "", il

        def sub(m: re.Match[str]) -> str:
            k = int(m.group(1)) - 1
            lvl = levels.get(k, {"fmt": "decimal", "start": 1})
            return _fmt_number(counters[k] or lvl["start"], lvl["fmt"])

        return re.sub(r"%(\d)", sub, text).strip(), il


def _norm_sub(local: str) -> str:
    """«1)» и «(1)» → «(1)»."""
    core = local.strip().strip("()").rstrip(").").strip()
    return f"({core})"


# ---------------------------------------------------------------------------
# Таблицы
# ---------------------------------------------------------------------------


def _table_rows(table: Table) -> list[list[str]]:
    """Строки таблицы без повторов от объединённых ячеек."""
    rows: list[list[str]] = []
    for row in table.rows:
        cells: list[str] = []
        prev = None
        for cell in row.cells:
            if prev is not None and cell._tc is prev:
                continue  # объединённая по горизонтали ячейка возвращается несколько раз
            prev = cell._tc
            cells.append(cell.text)
        rows.append(cells)
    return clean_rows(rows)  # type: ignore[arg-type]


def _grid(table: Table) -> list[list[tuple[str, int]]]:
    """Сетка таблицы: для каждой строки список (текст, id ячейки) по столбцам сетки.

    Объединённая ячейка встречается в нескольких столбцах с одним id: так заголовки и значения
    остаются выровненными по столбцам (если схлопнуть повторы, значения «съедут»)."""
    out: list[list[tuple[str, int]]] = []
    for row in table.rows:
        cells = []
        for cell in row.cells:
            text = re.sub(r"\s+", " ", cell.text.replace("\xa0", " ")).strip()
            cells.append((text, id(cell._tc)))
        if any(t for t, _ in cells):
            out.append(cells)
    width = max((len(r) for r in out), default=0)
    return [r + [("", -1)] * (width - len(r)) for r in out]


def table_blocks(table: Table) -> list[Block]:
    """Узкая таблица остаётся таблицей. Широкая «матрица» (Приложения 2–4: способ закупки ×
    условие) превращается в предложения «Условие: …; Конкурс / Заказчики I группы: …» — так её
    понимает и поиск, и модель (из таблицы в 12 столбцов по частям смысл не восстановить)."""
    grid = _grid(table)
    if not grid:
        return []
    width = len(grid[0])
    if width < 6:
        rows = _table_rows(table)
        return [Block("table", rows=rows)] if rows else []
    # Шапка: первая строка плюс следующая, если она тоже «шапочная» (подписи столбцов,
    # а не данные): у Приложения 2 вторая строка — «Тип заказчика».
    header_rows = 1
    if len(grid) > 2 and grid[1][0][0].lower().startswith(("тип ", "вид ")):
        header_rows = 2
    cols: list[str] = []
    for j in range(width):
        parts: list[str] = []
        for r in grid[:header_rows]:
            v = r[j][0]
            if v and v not in parts:
                parts.append(v)
        cols.append(" / ".join(parts))
    first = "Условие" if header_rows == 2 else (cols[0] or "Строка")
    out: list[Block] = []
    for r in grid[header_rows:]:
        distinct = {cid for t, cid in r if t}
        if len(distinct) <= 1:
            out.append(Block("text", r[0][0]))  # строка-раздел внутри матрицы
            continue
        pairs: list[str] = []
        j = 1
        while j < width:
            text, cid = r[j]
            k = j
            while k + 1 < width and r[k + 1][1] == cid:
                k += 1
            if text:
                labels: list[str] = []
                for jj in range(j, k + 1):
                    lab = cols[jj] or f"столбец {jj + 1}"
                    if lab not in labels:
                        labels.append(lab)
                pairs.append(f"{' и '.join(labels)}: {text}")
            j = k + 1
        out.append(Block("text", f"{first}: {r[0][0]}. " + "; ".join(pairs) + "."))
    return out


# ---------------------------------------------------------------------------
# Основной разбор
# ---------------------------------------------------------------------------


def _typed_label(text: str) -> str:
    p = point_of(text)
    return p or ""


def parse_epoz_docx(path: Path) -> ParsedEpoz:
    if path.stat().st_size == 0:
        raise ParseError("DOCX не открывается: файл пустой (0 байт)")
    try:
        doc = open_docx(str(path))
    except Exception as exc:  # noqa: BLE001
        raise ParseError(f"DOCX не открывается: {type(exc).__name__}"[:500]) from exc

    items: list[tuple[str, Any]] = []  # ("p", Paragraph) | ("t", Table) в порядке документа
    for el in doc.element.body.iterchildren():
        if el.tag == qn("w:p"):
            items.append(("p", Paragraph(el, doc)))
        elif el.tag == qn("w:tbl"):
            items.append(("t", Table(el, doc)))
    paragraphs = [x for k, x in items if k == "p"]
    toc = parse_toc(paragraphs)

    def sname(p: Paragraph) -> str:
        return (p.style.name if p.style is not None else "") or ""

    body = [p for p in paragraphs if p.text.strip() and not sname(p).lower().startswith("toc")]
    levelled = [p for p in body if style_level(sname(p)) is not None]
    use_toc = len(toc) >= 5 and len(levelled) > 20 and any("Уровень" in sname(p) for p in levelled)

    result = ParsedEpoz()
    report: dict[str, Any] = {
        "paragraphs": len(body),
        "tables": sum(1 for k, _ in items if k == "t"),
        "method": "toc" if use_toc else "numbering",
        "toc_entries": len(toc),
    }

    # Заголовки (уровни 1-3), которые надо сопоставить с оглавлением
    head_idx: list[tuple[int, str]] = []
    if use_toc:
        for i, (k, x) in enumerate(items):
            if k == "p" and x.text.strip() and not sname(x).lower().startswith("toc"):
                lv = style_level(sname(x))
                if lv is not None and lv <= 3:
                    head_idx.append((i, x.text.strip()))
    aligned = _align(head_idx, toc) if use_toc else {}
    if use_toc:
        report["headings"] = len(head_idx)
        report["headings_matched"] = len(aligned)

    emu = NumberingEmulator(path, doc)

    section = subsection = ""  # номера текущих раздела и подраздела (способ toc)
    cnt = [0, 0, 0]  # пункты / подпункты / подподпункты
    point_label = ""
    last_numeric = ""  # способ numbering: последний «полный» номер
    labels: set[str] = set()

    def add_clause(label: str, level: int, text: str, is_heading: bool) -> None:
        result.clauses.append(Clause(label, level, text, is_heading))
        if label and not label.endswith(")"):
            labels.add(label)

    for i, (k, x) in enumerate(items):
        if k == "t":
            for b in table_blocks(x):
                result.blocks.append(b)
            continue
        text = x.text.strip()
        name = sname(x)
        if not text or name.lower().startswith("toc"):
            continue
        lv = style_level(name)
        label_full, display = "", ""
        if use_toc and lv is None:
            # Абзац в стиле «Простой текст», который Word всё же нумерует как пункт
            # (уровни списка 2-4): считаем его пунктом, иначе нумерация ниже сдвинется.
            pk = emu.peek(x)
            if pk and pk[0] not in ("bullet", "none") and pk[1] in (2, 3, 4):
                lv = pk[1] + 2
        if use_toc:
            if lv is not None and lv <= 3:
                e = aligned.get(i)
                if e is not None:
                    num = e.number.strip()
                    display = (
                        f"{num} {text}".strip()
                        if num and not text.lower().startswith(num.lower())
                        else text
                    )
                    plain = num.rstrip(".")
                    if lv == 2:
                        section, subsection = plain, ""
                    elif lv == 3:
                        subsection = plain
                    elif lv == 1:
                        section = subsection = ""
                    cnt = [0, 0, 0]
                    point_label = ""
                    label_full = plain if lv >= 2 else ""
                else:
                    display = text
                    cnt = [0, 0, 0]
                    point_label = ""
                result.blocks.append(Block("heading", display, level=max(lv, 1) if lv else 2))
                add_clause(label_full, lv, display, True)
                continue
            if lv == 4:
                cnt[0] += 1
                cnt[1] = cnt[2] = 0
                base = subsection or (f"{section}.1" if section else "")
                point_label = f"{base}.{cnt[0]}" if base else ""
                label_full, display = (
                    point_label,
                    (f"{point_label}. {text}" if point_label else text),
                )
            elif lv == 5:
                cnt[1] += 1
                cnt[2] = 0
                loc = f"({cnt[1]})"
                label_full = f"{point_label}{loc}" if point_label else loc
                display = f"{loc} {text}"
            elif lv == 6:
                cnt[2] += 1
                loc = f"({_RUSSIAN_LOWER[(cnt[2] - 1) % len(_RUSSIAN_LOWER)]})"
                label_full = f"{point_label}({cnt[1]}){loc}" if point_label and cnt[1] else loc
                display = f"{loc} {text}"
            else:
                display = text
            result.blocks.append(Block("text", display))
            add_clause(label_full, lv or 0, display, False)
            continue

        # Способ 2: эмуляция нумерации Word + номера, набранные в тексте
        auto, ilvl = emu.label(x) if emu else ("", None)
        is_head = False
        level = 0
        if lv is not None:
            level = lv
            is_head = lv <= 3
        elif (
            auto
            and ilvl == 0
            and _NUM_ONLY.match(auto)
            and len(text) < 200
            and not text.endswith((";", ","))
        ):
            level, is_head = 2, True
        elif not auto:
            hl = heading_level(text)
            if hl:
                level, is_head = hl, True
        if auto:
            if _NUM_ONLY.match(auto):
                last_numeric = auto.rstrip(".")
                label_full = last_numeric
                display = f"{auto} {text}"
            else:
                label_full = f"{last_numeric}{_norm_sub(auto)}" if last_numeric else _norm_sub(auto)
                display = f"{auto} {text}"
        else:
            typed = _typed_label(text)
            if typed:
                last_numeric = typed
                label_full = typed
            display = text
        result.blocks.append(
            Block("heading" if is_head else "text", display, level=level if is_head else 0)
        )
        add_clause(label_full, level, display, is_head)

    if not any(b.text.strip() for b in result.blocks):
        raise ParseError("В DOCX нет текста")

    # Проверка: какая доля ссылок «п. 11.3.1» внутри файла находит пункт с таким номером
    refs: set[str] = set()
    for c in result.clauses:
        for m in _REF.finditer(c.text):
            refs.add(m.group(1))
    resolved = {r for r in refs if r in labels}
    report["clauses"] = len(result.clauses)
    report["numbered"] = sum(1 for c in result.clauses if c.label)
    report["refs_total"] = len(refs)
    report["refs_resolved"] = len(resolved)
    report["refs_rate"] = round(len(resolved) / len(refs), 3) if refs else None
    result.report = report
    return result
