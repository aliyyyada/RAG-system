"""Ссылки на документы в тексте — регулярными выражениями, без модели (шаг 1 графа).

Пример. Во фрагменте приказа № 112:

    1.2. Приказ № 45 от 10.01.2022 «О работах на высоте» признать утратившим силу.

`find_references(text)` найдёт ссылку «Приказ № 45 от 10.01.2022» и по словам рядом
(«признать утратившим силу») определит вид связи — cancels (отменяет).

Виды связей и слова-признаки (берётся ближайший к ссылке признак):

| kind     | по-русски        | признаки                                                    |
|----------|------------------|-------------------------------------------------------------|
| cancels  | отменяет         | утратившим силу, отменить, считать недействующим, об отмене |
| amends   | изменяет         | внести изменения в, о внесении изменений, в новой редакции  |
| executes | во исполнение    | во исполнение, в исполнение, в целях исполнения             |
| refers   | ссылается на     | всё остальное: «в соответствии с приказом № 7», «см. ИОТ-044»|

Ссылка распознаётся, если есть вид документа и номер: «приказ № 45», «приказа №45 от
10.01.2022», «распоряжение № 7-р», а также шифры вида «ИОТ-044». В тексте документа
знак «№» или дата обязательны («приказ 2024 года» — не ссылка); в вопросе пользователя
(`strict=False`) достаточно «приказ 45».
"""

import re
from dataclasses import dataclass
from datetime import date

# Основа слова → вид документа так, как он пишется в реестре.
_TYPE_STEMS: dict[str, str] = {
    "приказ": "Приказ",
    "распоряж": "Распоряжение",
    "постановл": "Постановление",
    "положени": "Положение",
    "инструкц": "Инструкция",
    "регламент": "Регламент",
    "стандарт": "Стандарт",
    "протокол": "Протокол",
    "акт": "Акт",
    "журнал": "Журнал",
    "решени": "Решение",
}
_TYPE_RE = (
    r"(?P<type>приказ\w*|распоряжени\w*|постановлени\w*|положени\w*|инструкци\w*"
    r"|регламент\w*|стандарт\w*|протокол\w*|акт|акта|акте|актом|журнал\w*|решени\w*)"
)
# Название в кавычках между видом и номером: «Положение «О СУОТ» № П-01».
_QUOTED = r"(?:\s+[«\"][^»\"\n]{1,160}[»\"])?"
_NUMBER = r"(?P<num>[0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё./\-]{0,30})"
_DATE = r"(?:\s+от\s+(?P<date>\d{1,2}\.\d{1,2}\.\d{2,4})(?:\s*г\.?)?)?"

_REF_STRICT = re.compile(
    _TYPE_RE + _QUOTED + r"\s*(?P<sign>№|N|No)\s*" + _NUMBER + _DATE, re.IGNORECASE
)
_REF_LOOSE = re.compile(
    _TYPE_RE + _QUOTED + r"\s*(?P<sign>№|N|No)?\s*(?P<num>\d[0-9A-Za-zА-Яа-яЁё./\-]{0,30})" + _DATE,
    re.IGNORECASE,
)
# Шифры документов: ИОТ-044, РИ-12, РТ-7, СТП-012. Вид документа — по шифру.
_CODE = re.compile(
    r"(?<![\w-])(?P<code>(?:ИОТ|ИПБ|РИ|РТ|СТП|СТО|ТИ)-\d{1,5}(?:-\d{2,4})?)(?![\w-])"
)
_CODE_TYPES = {"ИОТ": "Инструкция", "ИПБ": "Инструкция", "РИ": "Инструкция", "ТИ": "Инструкция"}

_SIGNS: dict[str, list[re.Pattern[str]]] = {
    "cancels": [
        re.compile(r"утратившими?\s+силу|утратил[аои]?\s+силу|утрачивает\s+силу", re.I),
        re.compile(r"\bотмен(ить|яется|яются|ён|ен|ена|ено|ены)\b|\bоб?\s+отмене\b", re.I),
        re.compile(r"(считать|признать)\s+недействующ", re.I),
    ],
    "amends": [
        re.compile(r"внес(ти|ении|ены|ение)\s+(следующие\s+)?изменени", re.I),
        re.compile(r"в\s+новой\s+редакции", re.I),
        re.compile(r"\bизменени[яй]\s+в\b", re.I),
        re.compile(r"\bдополнить\b|\bисключить\s+пункт", re.I),
    ],
    "executes": [
        re.compile(r"во\s+исполнение|в\s+исполнение|в\s+целях\s+исполнения", re.I),
    ],
}
# Признак относится к ссылке, только если стоит не дальше стольких символов от неё.
_SIGN_WINDOW = 120
# «Во исполнение приказа № 5» — признак стоит прямо перед ссылкой.
_EXECUTES_GAP = 12
_PRIORITY = {"cancels": 0, "amends": 1, "executes": 2}

KIND_LABELS = {
    "cancels": "отменяет",
    "amends": "изменяет",
    "executes": "во исполнение",
    "refers": "ссылается на",
}
INCOMING_LABELS = {
    "cancels": "отменён документом",
    "amends": "изменён документом",
    "executes": "исполняется документом",
    "refers": "упоминается в документе",
}


@dataclass(frozen=True)
class Reference:
    kind: str
    doc_type: str | None
    number: str
    number_norm: str
    doc_date: date | None
    label: str
    quote: str
    start: int
    end: int


def normalize_number(number: str) -> str:
    """«045» → «45», «7-Р» → «7-р», «П-01» → «п-01»: для сравнения номеров."""
    n = re.sub(r"\s+", "", number).lower().replace("ё", "е").strip(".,;:")
    if n.isdigit():
        n = n.lstrip("0") or "0"
    return n


def type_from_word(word: str) -> str | None:
    w = word.lower()
    for stem, name in _TYPE_STEMS.items():
        if w.startswith(stem):
            return name
    return None


def type_matches(ref_type: str | None, doc_type: str | None) -> bool:
    """Вид в ссылке совпадает с видом документа в реестре (или один из них неизвестен)."""
    if not ref_type or not doc_type:
        return True
    return type_from_word(doc_type) == ref_type or doc_type.lower().startswith(ref_type.lower())


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        d, m, y = (int(x) for x in value.split("."))
        if y < 100:
            y += 2000 if y < 70 else 1900
        return date(y, m, d)
    except ValueError:
        return None


def _sentence(text: str, start: int, end: int) -> str:
    """Предложение вокруг ссылки: от начала строки/пункта до точки с пробелом/конца строки."""
    left = text.rfind("\n", 0, start) + 1
    right = text.find("\n", end)
    right = len(text) if right < 0 else right
    s = text[left:right].strip()
    if len(s) > 400:
        a = max(0, start - left - 180)
        s = ("…" if a else "") + s[a : a + 400].strip() + "…"
    return s


def _classify(text: str, start: int, end: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line_end = len(text) if line_end < 0 else line_end
    best: tuple[int, int, str] | None = None  # (расстояние, приоритет, вид)
    for kind, patterns in _SIGNS.items():
        for pat in patterns:
            for m in pat.finditer(text, max(line_start, start - _SIGN_WINDOW), line_end):
                if m.end() <= start:
                    dist = start - m.end()
                elif m.start() >= end:
                    dist = m.start() - end
                else:
                    dist = 0
                if dist > _SIGN_WINDOW:
                    continue
                if kind == "executes" and not (m.end() <= start and dist <= _EXECUTES_GAP):
                    continue
                # Признак прямо перед ссылкой («отменить приказ № 7», «во исполнение
                # приказа № 5») связан с ней сильнее, чем признак после неё.
                if m.end() <= start and dist <= _EXECUTES_GAP:
                    dist = -1
                cand = (dist, _PRIORITY[kind], kind)
                if best is None or cand < best:
                    best = cand
    return best[2] if best else "refers"


def _label(doc_type: str | None, number: str, d: date | None) -> str:
    head = f"{doc_type} № {number}" if doc_type else number
    return f"{head} от {d:%d.%m.%Y}" if d else head


def find_references(text: str, *, strict: bool = True) -> list[Reference]:
    """Все ссылки на документы в тексте, по порядку. Пересекающиеся совпадения — одно."""
    found: list[Reference] = []
    taken: list[tuple[int, int]] = []
    pattern = _REF_STRICT if strict else _REF_LOOSE
    for m in pattern.finditer(text):
        number = m.group("num").rstrip(".,;:-/")
        if not number or not any(ch.isdigit() for ch in number):
            continue
        doc_type = type_from_word(m.group("type"))
        d = _parse_date(m.group("date"))
        found.append(
            Reference(
                kind=_classify(text, m.start(), m.end()),
                doc_type=doc_type,
                number=number,
                number_norm=normalize_number(number),
                doc_date=d,
                label=_label(doc_type, number, d),
                quote=_sentence(text, m.start(), m.end()),
                start=m.start(),
                end=m.end(),
            )
        )
        taken.append((m.start(), m.end()))
    for m in _CODE.finditer(text):
        if any(a <= m.start() < b for a, b in taken):
            continue
        code = m.group("code")
        doc_type = _CODE_TYPES.get(code.split("-")[0])
        found.append(
            Reference(
                kind=_classify(text, m.start(), m.end()),
                doc_type=doc_type,
                number=code,
                number_norm=normalize_number(code),
                doc_date=None,
                label=code,
                quote=_sentence(text, m.start(), m.end()),
                start=m.start(),
                end=m.end(),
            )
        )
    found.sort(key=lambda r: r.start)
    return found
