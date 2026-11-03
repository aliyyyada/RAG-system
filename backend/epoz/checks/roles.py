"""Роли файлов пакета и их определение по имени и по содержимому."""

from __future__ import annotations

import re

ROLES: dict[str, str] = {
    "contract": "Договорной документ",
    "tz": "Техническое задание",
    "note": "Сопроводительная записка к заявке",
    "nmc": "Расчёт НМЦ",
    "request": "Запрос КП",
    "kp": "Коммерческое предложение",
    "other": "Другое",
}

# Порядок важен: «Запрос КП» должен определиться раньше, чем «КП».
_NAME_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("request", re.compile(r"запрос", re.I)),
    ("note", re.compile(r"сопроводит|записк", re.I)),
    ("nmc", re.compile(r"нмц|начальн\w+ \(?максимальн", re.I)),
    ("tz", re.compile(r"техническ\w+ задани|(^|[\s._\-])тз($|[\s._\-])", re.I)),
    ("contract", re.compile(r"договор|контракт", re.I)),
    ("kp", re.compile(r"коммерческ|(^|[\s._\-])кп($|[\s._\-\d№])|ценово\w+ предложени", re.I)),
)

# Признаки по содержимому (начало документа): (роль, шаблон, вес)
_TEXT_RULES: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("tz", re.compile(r"техническое задание", re.I), 3),
    (
        "contract",
        re.compile(
            r"предмет договора|стороны договорились|договор\s+(поставки|оказания|подряда)", re.I
        ),
        3,
    ),
    ("kp", re.compile(r"коммерческое предложение|ценовое предложение", re.I), 3),
    (
        "nmc",
        re.compile(
            r"расч[её]т\s+(начальной|нмц)|метод\w* (сопоставимых рыночных цен|анализа рынка)", re.I
        ),
        3,
    ),
    (
        "request",
        re.compile(
            r"прошу (вас )?(направить|предоставить)|запрос\s+(о предоставлении )?(информации|коммерческ|цен)",
            re.I,
        ),
        3,
    ),
    ("note", re.compile(r"сопроводительная записка|заявк\w+ на приобретение", re.I), 3),
)


def guess_role(filename: str) -> str:
    """Роль по имени файла: «6. Коммерческое предложение №1.pdf» → kp."""
    name = re.sub(r"\.[a-z0-9]{2,4}$", "", filename.strip(), flags=re.I)
    for role, pat in _NAME_RULES:
        if pat.search(name):
            return role
    return "other"


def detect_role(text: str) -> str | None:
    """Роль по содержимому (начало документа). None — уверенно определить не удалось."""
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    title, head = (lines[0][:200] if lines else ""), text[:4000]  # заголовок — первая строка
    scores: dict[str, int] = {}
    for role, pat, w in _TEXT_RULES:
        if pat.search(title):
            scores[role] = (
                scores.get(role, 0) + w * 3
            )  # заголовок документа — самый сильный признак
        elif pat.search(head):
            scores[role] = scores.get(role, 0) + w
    if not scores:
        return None
    best = max(scores, key=lambda r: scores[r])
    ties = [r for r, s in scores.items() if s == scores[best]]
    return best if len(ties) == 1 else None
