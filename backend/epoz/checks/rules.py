"""Правила 20 критериев проверки. Каждое правило — функция `async def rule_X_Y(ctx) -> Res`.

Статусы результата и отдельных пунктов:
    ok     — соответствует;
    fail   — не соответствует;
    warn   — требует внимания (проверьте);
    manual — автоматически проверить нельзя (скан, нет данных, нужен внешний реестр).
Итог критерия — самый тяжёлый статус его пунктов: fail > warn > manual > ok.

Правила опираются на пункты ЕПоЗ (`epoz_refs` критерия): текст пункта берётся из базы по номеру
и показывается рядом с результатом. Результат всегда рекомендательный: решает специалист.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Chunk
from epoz import loader, okpd2
from epoz.checks.extract import okpd_codes, payment_days
from epoz.checks.llm import ask_json
from epoz.checks.pkg import FileData
from epoz.checks.roles import ROLES
from epoz.checks.textutil import fmt_money, md_tables, norm_ws, overlap, parse_ru_number
from epoz.models import CheckRun, EpozClause

OK, FAIL, WARN, MANUAL = "ok", "fail", "warn", "manual"
_RANK = {OK: 0, MANUAL: 1, WARN: 2, FAIL: 3}
UNIT_RE = re.compile(
    r"\d[\d\s]*(?:[.,]\d+)?\s*(?:мм|см|км|кг|мл|шт|квт|вт|гц|мпа|°c|°с|дн|мес|лет|мин|руб|%|м\b|г\b|т\b|л\b|в\b|а\b|ч\b)",
    re.I,
)
TOL = Decimal("0.01")


@dataclass
class Res:
    status: str = OK
    summary: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)
    quotes: list[dict[str, Any]] = field(default_factory=list)
    epoz: list[dict[str, Any]] = field(default_factory=list)
    values: dict[str, Any] = field(default_factory=dict)
    model: str | None = None
    ms: int | None = None

    def add(
        self, label: str, status: str, note: str = "", quote: dict[str, Any] | None = None
    ) -> None:
        item: dict[str, Any] = {"label": label, "status": status}
        if note:
            item["note"] = note
        if quote:
            item["quote"] = quote
        self.items.append(item)

    def finish(self, ok_text: str, bad_text: str | None = None) -> Res:
        """Итоговый статус по пунктам и краткое объяснение."""
        self.status = max((i["status"] for i in self.items), key=lambda s: _RANK[s], default=OK)
        if not self.summary:
            if self.status == OK:
                self.summary = ok_text
            elif self.status == MANUAL:
                # Нарушений нет, но кое-что автоматически проверить нельзя: говорим, что именно.
                left = [i["label"] for i in self.items if i["status"] == MANUAL][:3]
                self.summary = (
                    "Автоматически нарушений не найдено. Проверьте вручную: " + "; ".join(left)
                )
            else:
                self.summary = bad_text or ok_text
        return self


@dataclass
class Ctx:
    session: AsyncSession
    settings: Settings
    run: CheckRun
    files: list[FileData]
    customer_group: str | None
    cache: dict[str, Any] = field(default_factory=dict)

    def role(self, role: str) -> list[FileData]:
        return [f for f in self.files if f.role == role]

    def first_ok(self, role: str) -> FileData | None:
        return next((f for f in self.role(role) if f.ok), None)

    def winner(self) -> FileData | None:
        kps = [f for f in self.role("kp") if f.ok]
        marked = [f for f in kps if f.is_winner]
        if marked:
            return marked[0]
        return kps[0] if len(kps) == 1 else None

    async def cite(self, refs: list[tuple[str, str]], limit: int = 600) -> list[dict[str, Any]]:
        """Пункты ЕПоЗ по номеру: заголовок и начало текста."""
        out: list[dict[str, Any]] = []
        for part, label in refs:
            rows = await loader.get_clauses(self.session, part, label)
            if not rows:
                continue
            text = " ".join(norm_ws(r.text) for r in rows[:6])
            out.append(
                {
                    "part": part,
                    "part_title": loader.PARTS.get(part, part),
                    "label": label,
                    "text": text[:limit],
                }
            )
        return out

    async def find_clause(self, part: str, pattern: str) -> dict[str, Any] | None:
        """Первый пункт части комплекта, текст которого подходит под `pattern` (ILIKE-часть)."""
        active = await loader.get_active(self.session)
        if active is None:
            return None
        from epoz.models import EpozPart

        part_row = await self.session.scalar(
            select(EpozPart).where(EpozPart.edition_id == active.id, EpozPart.part_key == part)
        )
        if part_row is None:
            return None
        row = await self.session.scalar(
            select(EpozClause)
            .where(
                EpozClause.document_id == part_row.document_id,
                EpozClause.text.ilike(f"%{pattern}%"),
            )
            .order_by(EpozClause.ord)
            .limit(1)
        )
        if row is None:
            return None
        return {
            "part": part,
            "part_title": loader.PARTS.get(part, part),
            "label": row.label,
            "text": norm_ws(row.text)[:600],
        }

    async def app14_limit(self, code: str) -> dict[str, Any] | None:
        """Предельный срок оплаты по коду ОКПД2 из Приложения 14 (по началу кода)."""
        active = await loader.get_active(self.session)
        if active is None:
            return None
        from epoz.models import EpozPart

        part = await self.session.scalar(
            select(EpozPart).where(EpozPart.edition_id == active.id, EpozPart.part_key == "app14")
        )
        if part is None:
            return None
        chunks = await self.session.scalars(
            select(Chunk).where(Chunk.document_id == part.document_id).order_by(Chunk.ord)
        )
        digits = code.replace(".", "")
        best: tuple[int, dict[str, Any]] | None = None
        for table in md_tables("\n\n".join(c.text for c in chunks)):
            for row in table:
                if len(row) < 4:
                    continue
                rd = row[1].replace(".", "").strip()
                if not rd.isdigit() or not digits.startswith(rd):
                    continue
                m = re.search(r"(\d+)", row[3])
                if not m:
                    continue
                if best is None or len(rd) > best[0]:
                    best = (
                        len(rd),
                        {
                            "code": row[1].strip(),
                            "name": row[2].strip(),
                            "days": int(m.group(1)),
                            "term": row[3].strip(),
                        },
                    )
        return best[1] if best else None


Rule = Callable[[Ctx], Awaitable[Res]]


def _role_mismatch(res: Res, fd: FileData) -> None:
    if (
        fd.role_detected
        and fd.role_detected != fd.role
        and fd.role in ROLES
        and fd.role_detected in ROLES
    ):
        res.add(
            f"Файл «{fd.name}»",
            WARN,
            f"выбрана роль «{ROLES[fd.role]}», а по содержимому файл больше похож на «{ROLES[fd.role_detected]}»",
        )


def _unreadable(res: Res, fd: FileData) -> bool:
    if fd.ok:
        return False
    res.add(
        f"Файл «{fd.name}»",
        MANUAL,
        f"не удалось прочитать: {fd.parse_error or 'в файле нет текста'}",
    )
    return True


def _scan_note(res: Res, *files: FileData) -> None:
    scanned = [f.name for f in files if f.scanned]
    if scanned:
        res.add(
            "Скан", MANUAL, "распознано OCR, сверьте значения с оригиналом: " + ", ".join(scanned)
        )


# ---------------------------------------------------------------------------
# Блок 1. Комплектность
# ---------------------------------------------------------------------------


async def rule_1_1(ctx: Ctx) -> Res:
    res = Res(epoz=await ctx.cite([("main", "10.3")]))
    tzs = ctx.role("tz")
    if not tzs:
        res.add("Техническое задание", FAIL, "в пакете нет файла с ролью «Техническое задание»")
        return res.finish("")
    tz = tzs[0]
    _role_mismatch(res, tz)
    if _unreadable(res, tz):
        return res.finish("")
    res.add("Техническое задание", OK, f"найден файл «{tz.name}»")
    t = tz.text.lower()
    elements = (
        (
            "Характеристики продукции",
            r"характеристик|показател|параметр|требовани\w+ к (продукци|товар|работ|услуг)",
        ),
        ("Количество (объём)", r"количеств|кол-во|\bшт\b|объ[её]м"),
        ("Срок поставки / выполнения", r"срок\w* (поставк|выполнен|оказан|исполнен)|в течение \d+"),
        (
            "Место поставки / выполнения",
            r"место (поставк|выполнен|оказан)|адрес поставки|по адресу",
        ),
    )
    for label, pat in elements:
        m = re.search(pat, t)
        res.add(
            label,
            OK if m else WARN,
            ""
            if m
            else "не найдено в тексте ТЗ (п. 10.3: предмет описывается характеристиками, количеством, сроками)",
        )
    measurable = len(UNIT_RE.findall(tz.text))
    res.add(
        "Измеримые требования",
        OK if measurable >= 3 else WARN,
        f"найдено значений с единицами измерения: {measurable}"
        + (
            ""
            if measurable >= 3
            else ". Требования должны быть измеримыми: числа или «да/нет» (п. 10.3)"
        ),
    )
    _scan_note(res, tz)
    return res.finish(
        "ТЗ найдено, обязательные элементы описаны", "ТЗ найдено, но есть что проверить"
    )


async def rule_1_2(ctx: Ctx) -> Res:
    res = Res()
    cs = ctx.role("contract")
    if not cs:
        res.add(
            "Договорной документ",
            FAIL,
            "в пакете нет проекта договора, договорного письма или иного договорного документа",
        )
        return res.finish("")
    c = cs[0]
    _role_mismatch(res, c)
    if _unreadable(res, c):
        return res.finish("")
    res.add("Договорной документ", OK, f"найден файл «{c.name}»")
    tz = ctx.first_ok("tz")
    subject = c.extracted.get("subject")
    if not subject:
        res.add("Предмет договора", WARN, "в договоре не найден раздел «Предмет договора»")
        return res.finish("")
    res.quotes.append(c.quote(subject[:40]))
    if tz is None:
        res.add(
            "Сопоставление с предметом закупки",
            MANUAL,
            "нет технического задания, сопоставить нечего",
        )
        return res.finish("")
    basis = tz.extracted.get("subject") or tz.text[:1500]
    ov = overlap(subject, basis)
    if ov >= 0.5:
        res.add(
            "Предмет договора и предмет закупки",
            OK,
            f"совпадение по смыслу слов: {round(ov * 100)}%",
        )
    elif ov >= 0.2:
        data, ms = await ask_json(
            ctx.settings,
            "Ты проверяешь закупочный пакет. Скажи, относится ли договор к тому же предмету, что и техническое задание. "
            "Отвечай только JSON.",
            f"Предмет договора:\n{subject}\n\nТехническое задание (начало):\n{tz.text[:1200]}",
            {
                "type": "object",
                "properties": {"same_subject": {"type": "boolean"}, "reason": {"type": "string"}},
                "required": ["same_subject", "reason"],
            },
            max_tokens=200,
        )
        res.ms = ms or None
        if data is None:
            res.add(
                "Предмет договора и предмет закупки",
                WARN,
                f"совпадение слов лишь {round(ov * 100)}%: сопоставьте вручную",
            )
        else:
            res.model = ctx.settings.llm_model
            res.add(
                "Предмет договора и предмет закупки",
                OK if data.get("same_subject") else WARN,
                str(data.get("reason", ""))[:300],
            )
    else:
        res.add(
            "Предмет договора и предмет закупки",
            WARN,
            f"совпадение по словам всего {round(ov * 100)}%: предмет договора не похож на ТЗ",
        )
    _scan_note(res, c)
    return res.finish(
        "Договорной документ есть, предмет соответствует закупке",
        "Договорной документ есть, но предмет стоит проверить",
    )


async def rule_1_3(ctx: Ctx) -> Res:
    res = Res(epoz=await ctx.cite([("app5", "6.6"), ("app5", "6.9")]))
    kps = ctx.role("kp")
    n = len(kps)
    if n == 0:
        res.add(
            "Коммерческие предложения",
            FAIL,
            "в пакете нет файлов с ролью «Коммерческое предложение»",
        )
        return res.finish("")
    good = [k for k in kps if k.ok]
    for k in kps:
        _role_mismatch(res, k)
        _unreadable(res, k) if not k.ok else None
    if n >= 3:
        res.add("Число предложений", OK, f"{n} (рекомендуется не менее трёх, п. 6.9 Приложения 5)")
    else:
        justification = any(
            re.search(r"невозможност|обоснован", f.text, re.I)
            for f in ctx.files
            if f.role in ("note", "nmc") and f.ok
        )
        res.add(
            "Число предложений",
            WARN,
            f"приложено {n}; п. 6.9 Приложения 5 рекомендует не менее трёх цен от разных поставщиков, "
            + (
                "обоснование невозможности в пояснительной записке найдено"
                if justification
                else "нужны перечень действий и обоснование в пояснительной записке"
            ),
        )
    keys = [
        (k.extracted.get("inn") or norm_ws(k.extracted.get("supplier") or "").lower() or None)
        for k in good
    ]
    known = [x for x in keys if x]
    if len(set(known)) < len(known):
        res.add(
            "Разные поставщики",
            FAIL,
            "несколько предложений от одной организации: цены должны быть от разных поставщиков",
        )
    elif len(known) < len(good):
        res.add(
            "Разные поставщики",
            MANUAL,
            "у части предложений не определён поставщик, сравнить организации нельзя",
        )
    elif known:
        res.add("Разные поставщики", OK, f"поставщиков: {len(set(known))}")
    # п. 6.6 Приложения 5: что не принимается для расчёта НМЦ
    for k in good:
        ex = k.extracted
        if not ex.get("inn") and not ex.get("supplier"):
            res.add(
                f"«{k.name}»: поставщик",
                FAIL,
                "предложение анонимное (нет наименования и ИНН), п. 6.6(2): такая информация в расчёт не берётся",
            )
        if not ex.get("date"):
            res.add(f"«{k.name}»: дата", WARN, "дата предложения не найдена")
        if not ex.get("items") and not ex.get("total"):
            res.add(
                f"«{k.name}»: цены",
                WARN,
                "не найдена цена (п. 6.6(3): документ должен содержать запрошенные сведения)",
            )
    res.add(
        "Реестр недобросовестных поставщиков",
        MANUAL,
        "п. 6.6(1): информацию от лиц из реестра не используют. Автоматически не проверяется (нет доступа к реестру): проверьте поставщиков вручную",
    )
    _scan_note(res, *good)
    return res.finish(
        "Предложения приложены и оформлены", "Предложения есть, но есть что проверить"
    )


async def rule_1_4(ctx: Ctx) -> Res:
    res = Res(epoz=await ctx.cite([("app5", "6.15")]))
    ns = ctx.role("nmc")
    if not ns:
        res.add("Расчёт НМЦ", FAIL, "в пакете нет файла с ролью «Расчёт НМЦ»")
        return res.finish("")
    n = ns[0]
    _role_mismatch(res, n)
    if _unreadable(res, n):
        return res.finish("")
    res.add("Расчёт НМЦ", OK, f"найден файл «{n.name}»")
    nt = n.extracted.get("nmc_table")
    res.add(
        "Расчёт по форме 1.1",
        OK if nt else WARN,
        "найдена таблица с ценами поставщиков, средней ценой и общей стоимостью"
        if nt
        else "таблица расчёта по форме приложения 1.1 к Порядку не распознана",
    )
    kps = [k for k in ctx.role("kp") if k.ok]
    res.add(
        "Документы-источники цен",
        OK if kps else WARN,
        f"приложено коммерческих предложений: {len(kps)}"
        if kps
        else "п. 6.15: к пояснительной записке прикладываются документы, использованные при расчёте",
    )
    _scan_note(res, n)
    return res.finish("Расчёт НМЦ приложен по форме", "Расчёт НМЦ приложен, но есть что проверить")


# ---------------------------------------------------------------------------
# Блок 2. Номенклатура
# ---------------------------------------------------------------------------


async def rule_2_1(ctx: Ctx) -> Res:
    res = Res()
    ed = await okpd2.active_edition(ctx.session)
    if ed is None:
        res.add(
            "Справочник ОКПД2",
            MANUAL,
            "справочник не загружен: администратор загружает его в «Администрирование → Справочник ОКПД2»",
        )
        return res.finish("")
    res.values["okpd2_edition"] = ed.title
    tz = ctx.first_ok("tz")
    codes: list[str] = []
    source = ""
    for role in ("tz", "note", "nmc", "contract"):
        for f in ctx.role(role):
            if f.ok and f.extracted.get("okpd2"):
                codes = list(f.extracted["okpd2"])
                source = f.name
                break
        if codes:
            break
    if not codes:
        res.add("Код ОКПД2", FAIL, "код ОКПД2 в документах пакета не найден")
        return res.finish("")
    subject = " ".join(
        x
        for x in (
            (tz.extracted.get("subject") if tz else None),
            *[str(i.get("name")) for i in (tz.extracted.get("items", []) if tz else [])],
        )
        if x
    ) or (tz.text[:800] if tz else "")
    for code in codes[:3]:
        row = await okpd2.get_code(ctx.session, code)
        if row is None:
            res.add(f"Код {code}", FAIL, f"такого кода нет в справочнике ОКПД2 ({ed.title})")
            continue
        if row.level < 6:
            res.add(
                f"Код {code}",
                WARN,
                f"«{row.name}»: код неполный (уровень {row.level} из 6), нужен код до 9 знаков",
            )
        else:
            res.add(f"Код {code}", OK, f"«{row.name}»")
        if subject and overlap(row.name, subject) < 0.2:
            hits = await okpd2.search(ctx.session, subject, limit=3)
            hint = "; ".join(f"{o.code} {o.name[:60]}" for o, _ in hits)
            res.add(
                f"Код {code} и предмет закупки",
                WARN,
                "название кода слабо совпадает с предметом закупки"
                + (f". Похожие коды: {hint}" if hint else ""),
            )
    res.values["source_file"] = source
    return res.finish(
        "Код ОКПД2 есть в справочнике и соответствует предмету",
        "Код ОКПД2 найден, но есть что проверить",
    )


async def rule_2_2(ctx: Ctx) -> Res:
    res = Res(epoz=await ctx.cite([("main", "10.3")], limit=900))
    tz = ctx.first_ok("tz")
    if tz is None:
        res.add("Техническое задание", MANUAL, "ТЗ не загружено или не читается")
        return res.finish("")
    # 1. Измеримость значений в таблице требований
    rows = [r for t in tz.tables for r in t[1:] if len(r) >= 2]
    if rows:
        numeric = sum(
            1
            for r in rows
            if any(
                re.search(r"\d", c) or re.fullmatch(r"(да|нет|наличие|отсутствие)", c.strip(), re.I)
                for c in r
            )
        )
        share = numeric / len(rows)
        res.add(
            "Требования измеримы",
            OK if share >= 0.7 else WARN,
            f"в {numeric} из {len(rows)} строк требований есть числа или «да/нет»"
            + ("" if share >= 0.7 else "; п. 10.3: требования должны быть измеримыми"),
        )
    # 2. Товарные знаки без «или эквивалент»
    tm = re.search(r"товарн\w+ знак|торгов\w+ марк|бренд|фирменн\w+ наименован", tz.text, re.I)
    if tm and not re.search(r"эквивалент", tz.text, re.I):
        res.add(
            "Ограничение конкуренции",
            WARN,
            "указан товарный знак или бренд без слов «или эквивалент»",
            tz.quote(tm.group(0)),
        )
    else:
        res.add("Ограничение конкуренции", OK, "товарных знаков без «или эквивалент» не найдено")
    # 3. Смысловая проверка моделью по тексту п. 10.3
    clauses = await loader.get_clauses(ctx.session, "main", "10.3")
    if clauses:
        norm10_3 = "\n".join(norm_ws(c.text) for c in clauses)[:3500]
        data, ms = await ask_json(
            ctx.settings,
            "Ты проверяешь техническое задание на соответствие требованиям ЕПоЗ (раздел 10.3). "
            "Найди нарушения и процитируй точный фрагмент ТЗ дословно. Если нарушений нет, верни пустой список. Отвечай только JSON.",
            f"Требования ЕПоЗ:\n{norm10_3}\n\nТехническое задание:\n{tz.text[:3500]}",
            {
                "type": "object",
                "properties": {
                    "issues": {
                        "type": "array",
                        "maxItems": 5,
                        "items": {
                            "type": "object",
                            "properties": {
                                "problem": {"type": "string"},
                                "quote": {"type": "string"},
                            },
                            "required": ["problem", "quote"],
                        },
                    }
                },
                "required": ["issues"],
            },
            max_tokens=450,
        )
        if data is None:
            res.add(
                "Смысловая проверка по п. 10.3",
                MANUAL,
                "модель недоступна: сравните ТЗ с п. 10.3 вручную",
            )
        else:
            res.model, res.ms = ctx.settings.llm_model, ms or None
            verified = [
                i
                for i in data.get("issues", [])
                if i.get("quote")
                and norm_ws(str(i["quote"]))[:60].lower() in norm_ws(tz.text).lower()
            ]
            if verified:
                for i in verified[:4]:
                    res.add(
                        "Замечание по п. 10.3",
                        WARN,
                        str(i["problem"])[:300],
                        tz.quote(str(i["quote"])),
                    )
            else:
                res.add(
                    "Смысловая проверка по п. 10.3",
                    OK,
                    "модель не нашла нарушений с точной цитатой из ТЗ (результат рекомендательный)",
                )
    else:
        res.add(
            "Смысловая проверка по п. 10.3",
            MANUAL,
            "в загруженном ЕПоЗ не найден п. 10.3: сравнить ТЗ с требованиями нельзя",
        )
    _scan_note(res, tz)
    return res.finish("Требования ТЗ соответствуют п. 10.3", "В ТЗ есть замечания по п. 10.3")


def _item_qty(item: dict[str, Any]) -> Decimal | None:
    return parse_ru_number(str(item.get("qty"))) if item.get("qty") else None


async def rule_2_3(ctx: Ctx) -> Res:
    res = Res()
    tz, note = ctx.first_ok("tz"), ctx.first_ok("note")
    if tz is None or note is None:
        missing = [
            n
            for n, f in (("техническое задание", tz), ("сопроводительная записка", note))
            if f is None
        ]
        res.add(
            "Документы",
            FAIL if not ctx.role("note") else MANUAL,
            "нет или не читается: " + ", ".join(missing),
        )
        return res.finish("")
    _role_mismatch(res, note)
    ti, ni = tz.extracted.get("items") or [], note.extracted.get("items") or []
    if ti and ni:
        for it in ti:
            best = max(ni, key=lambda x: overlap(str(it["name"]), str(x["name"])))
            if overlap(str(it["name"]), str(best["name"])) < 0.5:
                res.add(f"Позиция «{str(it['name'])[:60]}»", FAIL, "нет в сопроводительной записке")
                continue
            q1, q2 = _item_qty(it), _item_qty(best)
            if q1 is not None and q2 is not None and q1 != q2:
                res.add(
                    f"Позиция «{str(it['name'])[:60]}»",
                    FAIL,
                    f"количество: в ТЗ {q1}, в записке {q2}",
                )
            else:
                res.add(
                    f"Позиция «{str(it['name'])[:60]}»", OK, "наименование и количество совпадают"
                )
        extra = [x for x in ni if max(overlap(str(x["name"]), str(it["name"])) for it in ti) < 0.5]
        for x in extra:
            res.add(f"Позиция «{str(x['name'])[:60]}»", FAIL, "есть в записке, но нет в ТЗ")
    else:
        # Позиции из таблиц не извлеклись: сверяем числа (количества, суммы) в обоих документах
        a = {str(parse_ru_number(m)) for m in re.findall(r"\d[\d\s]*(?:[.,]\d+)?", tz.text)} - {
            "None"
        }
        b = {str(parse_ru_number(m)) for m in re.findall(r"\d[\d\s]*(?:[.,]\d+)?", note.text)} - {
            "None"
        }
        big = lambda s: {x for x in s if Decimal(x) >= 2}  # noqa: E731
        only_tz, only_note = big(a) - big(b), big(b) - big(a)
        if len(big(a)) < 3 or len(big(b)) < 3:
            # Таблицы позиций не распознаны (скан или нестандартная форма), а чисел мало для сверки:
            # вердикт «расходятся» был бы случайным, поэтому просим человека.
            res.add(
                "Позиции и количества",
                MANUAL,
                "таблицы позиций не распознаны (скан или нестандартная форма): сравните ТЗ и записку вручную",
            )
        elif len(big(a) & big(b)) >= max(1, min(len(big(a)), len(big(b))) // 2) and not (
            len(only_tz) > 3 or len(only_note) > 3
        ):
            res.add("Числовые данные", OK, f"общих значений: {len(big(a) & big(b))}")
        else:
            res.add(
                "Числовые данные",
                WARN,
                "позиции из таблиц не распознаны; числа в документах расходятся. Только в ТЗ: "
                + ", ".join(sorted(only_tz)[:6])
                + "; только в записке: "
                + ", ".join(sorted(only_note)[:6]),
            )
    codes_tz, codes_note = set(tz.extracted.get("okpd2", [])), set(note.extracted.get("okpd2", []))
    if codes_tz and codes_note and codes_tz != codes_note:
        res.add(
            "Код ОКПД2",
            FAIL,
            f"в ТЗ {', '.join(sorted(codes_tz))}, в записке {', '.join(sorted(codes_note))}",
        )
    _scan_note(res, tz, note)
    return res.finish("ТЗ и записка совпадают", "ТЗ и записка расходятся")


# ---------------------------------------------------------------------------
# Блок 5. Условия исполнения договора
# ---------------------------------------------------------------------------


def _contract(ctx: Ctx, res: Res) -> FileData | None:
    cs = ctx.role("contract")
    if not cs:
        res.add(
            "Договорной документ",
            MANUAL,
            "договор не загружен: проверить условия нельзя (см. критерий 1.2)",
        )
        return None
    if _unreadable(res, cs[0]):
        return None
    return cs[0]


def _found(
    res: Res, label: str, c: FileData, pattern: str, bad_note: str, bad_status: str = FAIL
) -> bool:
    m = re.search(pattern, c.text, re.I)
    if m:
        res.add(label, OK, "", c.quote(m.group(0)))
        return True
    res.add(label, bad_status, bad_note)
    return False


async def rule_5_1(ctx: Ctx) -> Res:
    res = Res()
    c = _contract(ctx, res)
    if c is None:
        return res.finish("")
    if not _found(
        res,
        "Условие об оплате",
        c,
        r"оплат|расч[её]т(ы)? по договору",
        "в договоре не найдено условие об оплате",
    ):
        return res.finish("")
    pay = c.extracted.get("payment")
    if not pay:
        res.add("Срок оплаты", WARN, "срок оплаты в днях в договоре не найден")
        return res.finish("")
    days, kind = int(pay["days"]), pay["kind"]
    res.values["contract_payment"] = f"{days} {kind} дней"
    group = ctx.customer_group
    code = next(
        (
            x
            for f in ctx.files
            for x in f.extracted.get("okpd2", [])
            if f.ok and f.role in ("tz", "note", "nmc")
        ),
        None,
    )
    limit = await ctx.app14_limit(code) if code else None
    if limit:
        res.epoz.append(
            {
                "part": "app14",
                "part_title": loader.PARTS["app14"],
                "label": limit["code"],
                "text": f"{limit['name']}: {limit['term']}",
            }
        )
        cmp_days = days if kind == "календарных" else round(days * 7 / 5)
        res.add(
            "Срок оплаты и Приложение 14",
            OK if cmp_days <= limit["days"] else FAIL,
            f"в договоре {days} {kind} дн., предельный срок по коду ОКПД2 {limit['code']}: {limit['days']} дн."
            + ("" if kind == "календарных" else " (рабочие дни пересчитаны в календарные ×7/5)"),
            {"file": c.name, "page": c.page_of(pay["quote"][:40]), "text": pay["quote"][:200]},
        )
    elif group == "I":
        clause = await ctx.find_clause("main", "Срок оплаты заказчиком I группы")
        if clause:
            res.epoz.append(clause)
        cmp_days = days if kind == "рабочих" else round(days * 5 / 7)
        res.add(
            "Срок оплаты по Положению",
            OK if cmp_days <= 7 else FAIL,
            f"в договоре {days} {kind} дн.; для заказчика I группы — не более 7 рабочих дней с приёмки (если закон не устанавливает иное)",
            {"file": c.name, "page": c.page_of(pay["quote"][:40]), "text": pay["quote"][:200]},
        )
    elif group == "II":
        res.add(
            "Срок оплаты",
            MANUAL,
            "для заказчиков II группы предельный срок в Положении не задан: проверьте вручную",
        )
    else:
        res.add(
            "Срок оплаты",
            MANUAL,
            "не задана группа заказчика (Администрирование → ЕПоЗ): без неё предельный срок оплаты не определить"
            + (f". Код ОКПД2 {code} в Приложении 14 не найден" if code else ""),
        )
    _scan_note(res, c)
    return res.finish(
        "Условие об оплате есть и укладывается в предельный срок",
        "Условие об оплате есть, но срок нужно проверить",
    )


async def rule_5_2(ctx: Ctx) -> Res:
    res = Res()
    c = _contract(ctx, res)
    if c is None:
        return res.finish("")
    _found(
        res,
        "Условие о ТЗР",
        c,
        r"транспортно[\s-]*заготовительн|\bТЗР\b|транспортн\w+ расход|расходы на (доставку|транспорт)|стоимость доставки|доставка (включена|входит)|включая (транспортные|доставку)",
        "условие о ТЗР (включены ли транспортно-заготовительные расходы в цену или оплачиваются отдельно) не найдено",
        WARN,
    )
    return res.finish("Условие о ТЗР есть", "Условие о ТЗР не найдено")


async def rule_5_3(ctx: Ctx) -> Res:
    res = Res()
    c = _contract(ctx, res)
    if c is None:
        return res.finish("")
    s1 = _found(
        res,
        "Срок поставки",
        c,
        r"срок\w*\s+(поставки|выполнения|оказания)|поставка\s+(осуществляется|производится)[^.]{0,80}(течение|до)",
        "срок поставки не найден",
        WARN,
    )
    s2 = _found(
        res,
        "Место поставки",
        c,
        r"место\s+(поставки|выполнения|оказания)|адрес\s+поставки|(поставка|доставка)[^.]{0,60}по адресу|франко|самовывоз",
        "место поставки не найдено",
        WARN,
    )
    _found(
        res,
        "Порядок поставки и приёмки",
        c,
        r"приемк|приёмк|поставк\w+\s+(партиями|осуществляется|производится)|товарная накладная|акт приема",
        "порядок поставки и приёмки не найден",
        WARN,
    )
    if not s1 and not s2:
        for i in res.items:
            if i["status"] == WARN:
                i["status"] = FAIL
    return res.finish("Условия поставки описаны", "Условия поставки описаны не полностью")


async def rule_5_4(ctx: Ctx) -> Res:
    res = Res()
    c = _contract(ctx, res)
    if c is None:
        return res.finish("")
    _found(
        res,
        "Срок действия договора",
        c,
        r"срок\w*\s+действия\s+(настоящего\s+)?договора|договор\s+(вступает\s+в\s+силу|действует)|действует\s+до",
        "условие о сроке действия договора не найдено",
    )
    return res.finish("Срок действия договора указан")


async def rule_5_5(ctx: Ctx) -> Res:
    res = Res()
    c = _contract(ctx, res)
    if c is None:
        return res.finish("")
    parts = (
        (
            "Форма оплаты",
            r"безналичн|платежн\w+ поручени|аккредитив|наличн|перечислен",
            "не указана (безналичный расчёт, платёжное поручение и т. п.)",
        ),
        (
            "Порядок оплаты",
            r"аванс|предоплат|по факту|после\s+(подписания|приемки|приёмки|поставки)|на основании\s+(счета|акта|накладной|счета-фактуры)",
            "не указан (аванс, по факту поставки, на основании акта или счёта)",
        ),
        (
            "Сроки оплаты",
            r"в течение\s+\d+|не позднее|\d+\s*(?:\([^)]*\)\s*)?(рабочих|календарных|банковских)?\s*дн",
            "не указаны (в течение скольких дней)",
        ),
    )
    miss = 0
    for label, pat, bad in parts:
        if not _found(res, label, c, pat, bad, WARN):
            miss += 1
    if miss == len(parts):
        for i in res.items:
            i["status"] = FAIL
    return res.finish(
        "Форма, порядок и сроки оплаты указаны", "Условия оплаты описаны не полностью"
    )


async def rule_5_6(ctx: Ctx) -> Res:
    res = Res()
    c = _contract(ctx, res)
    if c is None:
        return res.finish("")
    kps = [k for k in ctx.role("kp") if k.ok]
    kp = ctx.winner()
    if not kps:
        res.add("Коммерческое предложение", MANUAL, "нет коммерческих предложений")
        return res.finish("")
    if kp is None:
        res.add(
            "Победитель",
            MANUAL,
            "в форме не выбран победитель среди нескольких КП: отметьте его и запустите проверку заново",
        )
        return res.finish("")
    res.values["winner"] = kp.name
    inn, name = kp.extracted.get("inn"), kp.extracted.get("supplier")
    if inn:
        res.add(
            "Поставщик",
            OK if inn in c.text else FAIL,
            f"ИНН {inn} "
            + (
                "есть в договоре" if inn in c.text else "поставщика-победителя в договоре не найден"
            ),
        )
    elif name:
        res.add("Поставщик", OK if overlap(name, c.text[:3000]) >= 0.5 else WARN, f"«{name}»")
    cp, kp_total = (
        parse_ru_number(str(c.extracted.get("price"))) if c.extracted.get("price") else None,
        parse_ru_number(str(kp.extracted.get("total"))) if kp.extracted.get("total") else None,
    )
    if cp is not None and kp_total is not None:
        diff = abs(cp - kp_total)
        res.add(
            "Цена",
            OK if diff <= max(TOL, kp_total * Decimal("0.001")) else FAIL,
            f"в договоре {fmt_money(cp)} руб., в КП {fmt_money(kp_total)} руб.",
        )
    else:
        res.add("Цена", MANUAL, "цену договора или КП автоматически определить не удалось")
    pc, pk = c.extracted.get("payment"), kp.extracted.get("payment")
    if pc and pk:
        res.add(
            "Срок оплаты",
            OK if pc["days"] == pk["days"] and pc["kind"] == pk["kind"] else FAIL,
            f"в договоре {pc['days']} {pc['kind']} дн., в КП {pk['days']} {pk['kind']} дн.",
        )
    dk = kp.extracted.get("delivery_days")
    if dk is not None:
        m = re.search(
            rf"(?<!\d){dk}\s*(?:\([^)]*\)\s*)?(рабоч\w+|календарн\w+)?\s*дн", c.text, re.I
        )
        res.add(
            "Срок поставки",
            OK if m else WARN,
            f"в КП {dk} дн.; "
            + ("в договоре тот же срок" if m else "в договоре такой срок не найден"),
        )
    return res.finish(
        "Условия договора соответствуют КП победителя", "Договор расходится с КП победителя"
    )


# ---------------------------------------------------------------------------
# Блок 6. Расчёт НМЦ
# ---------------------------------------------------------------------------


async def rule_6_1(ctx: Ctx) -> Res:
    res = Res(epoz=await ctx.cite([("app5", "6.5"), ("app5", "6.2.1")]))
    reqs = [r for r in ctx.role("request")]
    if not reqs:
        res.add(
            "Запросы коммерческих предложений",
            WARN,
            "в пакете нет запросов; п. 6.2.1 Приложения 5: запросы направляют не менее чем трём поставщикам (если выбран этот источник цен)",
        )
        return res.finish("")
    elements = (
        (
            "Требования к продукции",
            r"требовани\w+ к (продукции|товар\w*|работ\w*|услуг\w*)|техническ\w+ (задани|характеристик)|характеристик",
        ),
        (
            "Место, сроки поставки и условия оплаты",
            r"место\s+поставки|срок\w*\s+(поставки|выполнения)|условия\s+оплаты|оплат",
        ),
        (
            "Срок и формат ответа, цена каждой единицы",
            r"(срок\w*\s+(предоставлени|направлени)|не позднее|до\s+\d{1,2}\.\d{1,2}\.\d{4})",
        ),
        (
            "Оговорка об отсутствии обязательств",
            r"не\s+влеч[её]т|не\s+накладывает|без\s+обязательств|не\s+является\s+(офертой|обязательств)",
        ),
        ("Требования к допускам и лицензиям", r"допуск|лиценз|разрешительн|не\s+требуется"),
    )
    for r in reqs:
        _unreadable(res, r) if not r.ok else None
    ok_reqs = [r for r in reqs if r.ok]
    if ok_reqs:
        text = "\n".join(r.text for r in ok_reqs)
        for label, pat in elements:
            m = re.search(pat, text, re.I)
            res.add(
                label,
                OK if m else WARN,
                "" if m else "в запросе не найдено (п. 6.5 Приложения 5)",
                ok_reqs[0].quote(m.group(0)) if m else None,
            )
        price_each = re.search(r"цен\w+\s+(каждой|за\s+единиц)|каждой\s+единиц", text, re.I)
        res.add(
            "Цена каждой единицы продукции",
            OK if price_each else WARN,
            ""
            if price_each
            else "не найдено требование предоставить цену каждой единицы (п. 6.5(3))",
        )
        orgs = {o for r in ok_reqs for o in r.extracted.get("orgs", [])}
        n_addr = max(len(orgs), len(ok_reqs))
        res.add(
            "Адресаты запросов",
            OK if n_addr >= 3 else WARN,
            f"найдено адресатов: {n_addr}"
            + ("" if n_addr >= 3 else "; п. 6.2.1: не менее трёх поставщиков"),
        )
    res.add(
        "Подтверждение направления",
        MANUAL,
        "подтверждения отправки (письма, скриншоты) автоматически не проверяются",
    )
    _scan_note(res, *ok_reqs)
    return res.finish("Запросы оформлены по п. 6.5", "В запросах есть замечания")


async def rule_6_2(ctx: Ctx) -> Res:
    res = Res(epoz=await ctx.cite([("app5", "6.6")]))
    kps = ctx.role("kp")
    if not kps:
        res.add("Ответы поставщиков", FAIL, "нет коммерческих предложений")
        return res.finish("")
    for k in kps:
        if not k.ok:
            res.add(f"«{k.name}»", MANUAL, f"не удалось прочитать: {k.parse_error or 'нет текста'}")
            continue
        ex = k.extracted
        res.add(
            f"«{k.name}»: поставщик",
            OK if (ex.get("supplier") or ex.get("inn")) else FAIL,
            ex.get("supplier") or ex.get("inn") or "наименование и ИНН не найдены",
        )
        res.add(
            f"«{k.name}»: ИНН",
            OK if ex.get("inn") else WARN,
            "" if ex.get("inn") else "ИНН не найден",
        )
        res.add(
            f"«{k.name}»: дата", OK if ex.get("date") else WARN, ex.get("date") or "дата не найдена"
        )
        res.add(
            f"«{k.name}»: номер",
            OK if ex.get("number") else WARN,
            ex.get("number") or "исходящий номер не найден",
        )
        res.add(
            f"«{k.name}»: цена единицы",
            OK if ex.get("items") or ex.get("total") else WARN,
            "" if ex.get("items") or ex.get("total") else "цены не найдены",
        )
        res.add(
            f"«{k.name}»: подпись и печать",
            MANUAL,
            "в тексте есть упоминание подписи"
            + (" (но на скане проверьте вручную)" if k.scanned else "")
            if ex.get("signature_mention")
            else "упоминаний подписи нет, проверьте по оригиналу",
        )
    return res.finish(
        "Ответы поставщиков содержат дату, реквизиты и цены", "В ответах поставщиков есть замечания"
    )


def _best_item(items: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    if not items:
        return None
    best = max(items, key=lambda x: overlap(name, str(x.get("name", ""))))
    return best if overlap(name, str(best.get("name", ""))) >= 0.4 else None


async def rule_6_3(ctx: Ctx) -> Res:
    res = Res(epoz=await ctx.cite([("app5", "6.1")]))
    tz = ctx.first_ok("tz")
    kps = [k for k in ctx.role("kp") if k.ok]
    ti = (tz.extracted.get("items") if tz else None) or []
    if not kps or not ti:
        res.add(
            "Позиции",
            MANUAL,
            "нет КП или в ТЗ не распознана таблица позиций: сопоставьте наименования вручную",
        )
        return res.finish("")
    for k in kps:
        ki = k.extracted.get("items") or []
        if not ki:
            res.add(f"«{k.name}»", MANUAL, "таблица позиций в КП не распознана")
            continue
        for it in ti:
            b = _best_item(ki, str(it["name"]))
            res.add(
                f"«{k.name}»: «{str(it['name'])[:50]}»",
                OK if b else FAIL,
                f"сопоставлено с «{str(b['name'])[:60]}»" if b else "в КП нет сопоставимой позиции",
            )
    return res.finish("Наименования позиций сопоставимы", "Есть несопоставимые позиции")


def _specs(text: str) -> set[str]:
    return {re.sub(r"\s+", "", m.group(0).lower()) for m in UNIT_RE.finditer(text)}


async def rule_6_4(ctx: Ctx) -> Res:
    res = Res(epoz=await ctx.cite([("app5", "6.1")]))
    tz = ctx.first_ok("tz")
    kps = [k for k in ctx.role("kp") if k.ok]
    if tz is None or not kps:
        res.add("Характеристики", MANUAL, "нет ТЗ или КП")
        return res.finish("")
    specs = _specs(tz.text)
    if len(specs) < 3:
        res.add("Характеристики в ТЗ", MANUAL, "в ТЗ мало числовых характеристик для сопоставления")
        return res.finish("")
    for k in kps:
        have = _specs(k.text)
        share = len(specs & have) / len(specs)
        missing = sorted(specs - have)[:6]
        res.add(
            f"«{k.name}»",
            OK if share >= 0.7 else (WARN if share >= 0.4 else FAIL),
            f"числовых характеристик ТЗ найдено в КП: {round(share * 100)}%"
            + (f"; не найдены: {', '.join(missing)}" if missing and share < 0.7 else ""),
        )
    res.add(
        "Качественные и функциональные параметры",
        MANUAL,
        "числа сопоставлены автоматически; смысловое сходство описаний проверьте вручную",
    )
    return res.finish(
        "Характеристики предложений сопоставимы", "Характеристики предложений расходятся с ТЗ"
    )


async def rule_6_5(ctx: Ctx) -> Res:
    res = Res()
    tz = ctx.first_ok("tz")
    ti = (tz.extracted.get("items") if tz else None) or []
    kps = [k for k in ctx.role("kp") if k.ok]
    nmc = ctx.first_ok("nmc")
    if not ti or (not kps and nmc is None):
        res.add(
            "Объёмы", MANUAL, "нет таблицы позиций в ТЗ либо нет КП и расчёта НМЦ для сравнения"
        )
        return res.finish("")
    for k in kps:
        ki = k.extracted.get("items") or []
        for it in ti:
            b = _best_item(ki, str(it["name"]))
            q1, q2 = _item_qty(it), _item_qty(b) if b else None
            if b is None or q1 is None or q2 is None:
                continue
            res.add(
                f"«{k.name}»: «{str(it['name'])[:45]}»",
                OK if q1 == q2 else FAIL,
                f"в ТЗ {q1}, в КП {q2}",
            )
    nt = (nmc.extracted.get("nmc_table") if nmc else None) or {}
    for row in nt.get("rows", []):
        b = _best_item(ti, str(row["name"]))
        q1, q2 = (
            parse_ru_number(str(row["qty"])) if row.get("qty") else None,
            _item_qty(b) if b else None,
        )
        if b is not None and q1 is not None and q2 is not None:
            res.add(
                f"НМЦ: «{str(row['name'])[:45]}»",
                OK if q1 == q2 else FAIL,
                f"в расчёте НМЦ {q1}, в ТЗ {q2}",
            )
    if not res.items:
        res.add("Объёмы", MANUAL, "количества в документах не удалось сопоставить автоматически")
    return res.finish("Объёмы совпадают с потребностью", "Объёмы расходятся")


def _d(v: object) -> Decimal | None:
    return parse_ru_number(str(v)) if v not in (None, "None", "") else None


def _sum_status(diff: Decimal) -> str:
    return OK if diff <= TOL else (WARN if diff <= Decimal("1.00") else FAIL)


async def _arith(ctx: Ctx) -> Res:
    """Арифметика НМЦ и КП: средняя цена, сумма, правило 33% (п. 6.10–6.13 Приложения 5)."""
    if "arith" in ctx.cache:
        return ctx.cache["arith"]
    res = Res(
        epoz=await ctx.cite([("app5", "6.10"), ("app5", "6.11"), ("app5", "6.13")], limit=500)
    )
    nmc = ctx.first_ok("nmc")
    nt = nmc.extracted.get("nmc_table") if nmc else None
    if nmc is None:
        res.add("Расчёт НМЦ", MANUAL, "расчёт НМЦ не загружен")
    elif not nt:
        res.add(
            "Таблица расчёта",
            MANUAL,
            "таблица по форме 1.1 (цены, средняя цена, общая стоимость) не распознана: проверьте арифметику вручную",
        )
    else:
        total_rows = Decimal(0)
        for row in nt["rows"]:
            label = f"НМЦ: «{str(row['name'])[:45]}»"
            prices = [p for p in (_d(x) for x in row["prices"]) if p is not None]
            avg, total, qty = _d(row.get("avg")), _d(row.get("total")), _d(row.get("qty"))
            if not prices:
                res.add(label, MANUAL, "цены поставщиков в строке не найдены")
                continue
            mean = sum(prices) / len(prices)
            dev = max(abs(p - mean) / mean for p in prices) if mean else Decimal(0)
            used_mean = mean
            if dev > Decimal("0.33") and len(prices) > 2:
                worst = max(prices, key=lambda p: abs(p - mean))
                rest = [p for p in prices if p != worst] or prices
                used_mean = sum(rest) / len(rest)
                if avg is not None and abs(avg - used_mean) <= TOL and abs(avg - mean) > TOL:
                    res.add(
                        label + ": правило 33%",
                        OK,
                        f"цена {fmt_money(worst)} отклонялась от средней более чем на 33% и исключена верно (п. 6.10–6.11)",
                    )
                else:
                    res.add(
                        label + ": правило 33%",
                        FAIL,
                        f"цена {fmt_money(worst)} отклоняется от средней более чем на 33%: по п. 6.10–6.11 её исключают и пересчитывают НМЦ",
                    )
            if avg is not None:
                res.add(
                    label + ": средняя цена",
                    _sum_status(abs(avg - used_mean)),
                    f"указано {fmt_money(avg)}, по формуле {fmt_money(used_mean)}",
                )
            if avg is not None and qty is not None and total is not None:
                res.add(
                    label + ": общая стоимость",
                    _sum_status(abs(qty * avg - total)),
                    f"{qty} × {fmt_money(avg)} = {fmt_money(qty * avg)}, указано {fmt_money(total)}",
                )
            if total is not None:
                total_rows += total
        grand = _d(nt.get("grand_total")) or _d(nmc.extracted.get("nmc_total"))
        if grand is not None and total_rows:
            res.add(
                "Итоговая НМЦ",
                _sum_status(abs(grand - total_rows)),
                f"сумма строк {fmt_money(total_rows)}, указано {fmt_money(grand)}",
            )
        kps = [k for k in ctx.role("kp") if k.ok]
        for row in nt["rows"]:
            allowed = {
                p
                for k in kps
                for it in (k.extracted.get("items") or [])
                if overlap(str(row["name"]), str(it["name"])) >= 0.4
                for p in [_d(it.get("price"))]
                if p is not None
            }
            if allowed:
                for x in (_d(p) for p in row["prices"]):
                    if x is not None and all(abs(x - a) > TOL for a in allowed):
                        res.add(
                            f"НМЦ: «{str(row['name'])[:45]}»: цена из КП",
                            WARN,
                            f"цена {fmt_money(x)} в расчёте не совпадает ни с одной ценой в КП",
                        )
    for k in [k for k in ctx.role("kp") if k.ok]:
        items = k.extracted.get("items") or []
        s = Decimal(0)
        n_checked = 0
        for it in items:
            q, p, t = _d(it.get("qty")), _d(it.get("price")), _d(it.get("total"))
            if q is not None and p is not None and t is not None:
                n_checked += 1
                st = _sum_status(abs(q * p - t))
                if st != OK:
                    res.add(
                        f"КП «{k.name}»: «{str(it['name'])[:40]}»",
                        st,
                        f"{q} × {fmt_money(p)} = {fmt_money(q * p)}, указано {fmt_money(t)}",
                    )
            if t is not None:
                s += t
        total = _d(k.extracted.get("table_total")) or _d(k.extracted.get("total"))
        if items and total is not None and s:
            res.add(
                f"КП «{k.name}»: итог",
                _sum_status(abs(s - total)),
                f"сумма позиций {fmt_money(s)}, итог {fmt_money(total)}",
            )
        elif n_checked:
            res.add(f"КП «{k.name}»: строки", OK, f"проверено строк: {n_checked}")
        elif not items:
            res.add(f"КП «{k.name}»", MANUAL, "таблица позиций не распознана")
    _scan_note(res, *[f for f in ctx.files if f.role in ("nmc", "kp") and f.ok])
    ctx.cache["arith"] = res
    return res


async def rule_6_6(ctx: Ctx) -> Res:
    res = await _arith(ctx)
    return Res(status=res.status, summary="", items=res.items, epoz=res.epoz).finish(
        "Арифметика расчёта верна", "В расчёте есть ошибки или неясные места"
    )


async def rule_6_7(ctx: Ctx) -> Res:
    res = Res(epoz=await ctx.cite([("app5", "6.15"), ("app5", "6.13")]))
    nmc = ctx.first_ok("nmc")
    if nmc is None:
        res.add("Расчёт НМЦ", MANUAL, "расчёт НМЦ не загружен")
        return res.finish("")
    method = nmc.extracted.get("method")
    res.add(
        "Метод определения НМЦ",
        OK if method else FAIL,
        f"указан: {method}"
        if method
        else "метод определения НМЦ со ссылкой на пункты Порядка не найден",
    )
    if method and "сопоставим" in method.lower() or method and "анализ" in method.lower():
        res.add(
            "НМЦ — среднее арифметическое (п. 6.13)",
            OK if nmc.extracted.get("nmc_table") else MANUAL,
            "в расчёте использованы цены поставщиков и средняя арифметическая"
            if nmc.extracted.get("nmc_table")
            else "таблица расчёта не распознана",
        )
    fields = (
        ("Основные требования к продукции", r"основные\s+требования"),
        ("Место поставки", r"место\s+поставки"),
        ("Срок исполнения договора", r"срок\s+начала|срок\w*\s+исполнения"),
        ("Условия оплаты", r"условия\s+оплаты"),
        ("Перечень поставщиков", r"перечень\s+поставщиков|поставщиков,\s+предоставивших"),
        ("Рассчитанная величина НМЦ", r"рассчитанная\s+величина|нмц\s*[:=—-]"),
    )
    texts = "\n".join(f.text for f in ctx.files if f.role in ("nmc", "note") and f.ok)
    for label, pat in fields:
        res.add(
            f"Пояснительная записка: {label}",
            OK if re.search(pat, texts, re.I) else WARN,
            ""
            if re.search(pat, texts, re.I)
            else "в таблице «Основные показатели» (Приложение 5) не найдено",
        )
    arith = await _arith(ctx)
    bad = [i for i in arith.items if i["status"] == FAIL]
    res.add(
        "Арифметика расчёта",
        FAIL if bad else (OK if arith.items else MANUAL),
        f"ошибок в расчёте: {len(bad)} (см. критерий 6.6)" if bad else "ошибок не найдено",
    )
    kps = [k for k in ctx.role("kp") if k.ok]
    res.add(
        "Приложения к расчёту (п. 6.15)",
        OK if kps else WARN,
        f"копии КП приложены: {len(kps)}"
        if kps
        else "не приложены документы, использованные при расчёте",
    )
    res.add(
        "Скриншоты источников и реестровые номера",
        MANUAL,
        "наличие скриншотов сайтов и номеров договоров из реестра автоматически не проверяется",
    )
    return res.finish(
        "Расчёт НМЦ соответствует Приложению 5", "В расчёте НМЦ есть замечания по Приложению 5"
    )


RULES: dict[str, Rule] = {
    "1.1": rule_1_1,
    "1.2": rule_1_2,
    "1.3": rule_1_3,
    "1.4": rule_1_4,
    "2.1": rule_2_1,
    "2.2": rule_2_2,
    "2.3": rule_2_3,
    "5.1": rule_5_1,
    "5.2": rule_5_2,
    "5.3": rule_5_3,
    "5.4": rule_5_4,
    "5.5": rule_5_5,
    "5.6": rule_5_6,
    "6.1": rule_6_1,
    "6.2": rule_6_2,
    "6.3": rule_6_3,
    "6.4": rule_6_4,
    "6.5": rule_6_5,
    "6.6": rule_6_6,
    "6.7": rule_6_7,
}

__all__ = ["RULES", "Ctx", "Res", "okpd_codes", "payment_days"]
