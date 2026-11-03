"""Синтетический закупочный пакет в PDF для проверки вкладки «Проверка ЕПоЗ» (данные вымышленные).

Нужен, когда настоящие документы нельзя использовать для проверки (коммерческая тайна). Файлы
устроены как настоящие: договор, ТЗ, записка, расчёт НМЦ, запрос КП и коммерческие предложения.

Запуск на вашем компьютере (не в Docker: нужен шрифт с кириллицей), нужны reportlab и,
для режима --scan, pypdfium2 и pillow:

    python backend/epoz/make_sample_pack.py "C:/Temp/pack"              # хороший пакет, 3 КП
    python backend/epoz/make_sample_pack.py "C:/Temp/pack_bad" --bad    # пакет с ошибками
    python backend/epoz/make_sample_pack.py "C:/Temp/pack_scan" --scan  # сканы: проверка OCR

Параметры: --kp 2 (два КП, как в вашем образце), --font путь\\к\\шрифту.ttf.
Имена файлов повторяют ваш пакет: «1. Договорной документ.pdf» … «6. Коммерческое предложение №1.pdf».
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

SUPPLIERS = [
    ("ООО «Поставщик Один»", "7701234567", "770101001"),
    ("ООО «Поставщик Два»", "7702345678", "770201001"),
    ("ООО «Поставщик Три»", "7703456789", "770301001"),
]
DEFAULT_FONT = r"C:\Windows\Fonts\arial.ttf"


def build(out: Path, *, kp: int, bad: bool, font: str) -> list[Path]:
    pdfmetrics.registerFont(TTFont("F", font))
    st = ParagraphStyle("b", fontName="F", fontSize=10.5, leading=15, spaceAfter=6)
    h = ParagraphStyle("h", parent=st, fontSize=14, leading=18, spaceAfter=10)
    out.mkdir(parents=True, exist_ok=True)

    def table(rows: list[list[str]]) -> Table:
        cells = [
            [
                Paragraph(c, ParagraphStyle("c", parent=st, fontSize=9, leading=11, spaceAfter=0))
                for c in r
            ]
            for r in rows
        ]
        t = Table(cells, repeatRows=1)
        t.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEEEEE")),
                ]
            )
        )
        return t

    def save(
        name: str, title: str, paras: list[str], tables: list[list[list[str]]] | None = None
    ) -> Path:
        path = out / name
        story: list = [Paragraph(title, h)] + [Paragraph(p, st) for p in paras]
        for rows in tables or []:
            story += [Spacer(1, 6), table(rows)]
        SimpleDocTemplate(
            str(path), pagesize=A4, leftMargin=50, rightMargin=50, topMargin=50, bottomMargin=50
        ).build(story)
        return path

    qty_note = "12" if bad else "10"
    price = ["60 000,00", "62 000,00", "58 000,00"]
    total = ["600 000,00", "620 000,00", "580 000,00"]
    contract_price = "610 000,00" if bad else "600 000,00"
    nmc_total = "650 000,00" if bad else "600 000,00"
    code = "99.99.99.999" if bad else "26.20.11.110"
    files = [
        save(
            "1. Договорной документ.pdf",
            "Договор поставки № 15 от 10.11.2026",
            [
                f"{SUPPLIERS[0][0]}, ИНН {SUPPLIERS[0][1]}, именуемое Поставщик, и ООО «Покупатель», именуемое Покупатель.",
                "1. Предмет договора",
                "Поставщик обязуется поставить ноутбуки (ОКПД2 26.20.11.110), а Покупатель принять и оплатить товар.",
                f"Цена договора: {contract_price} руб., в том числе НДС.",
                "Оплата производится безналичным расчётом платёжным поручением на основании счёта в течение 7 (семи) рабочих дней после приёмки товара.",
                "Транспортно-заготовительные расходы включены в цену договора.",
                "Срок поставки: 30 дней с даты заключения договора. Место поставки: г. Москва, ул. Тестовая, д. 1. Приёмка товара осуществляется по товарной накладной.",
                "Срок действия договора: до 31.12.2026.",
            ],
        ),
        save(
            "2. Техническое задание.pdf",
            "Техническое задание на поставку ноутбуков",
            [
                f"Код ОКПД2: {code}",
                "Требования к продукции и характеристики: масса не более 2 кг, толщина не более 20 мм, мощность блока питания 65 Вт, гарантия 36 мес.",
                "Количество: 10 шт.",
                "Срок поставки: в течение 30 дней с даты заключения договора.",
                "Место поставки: г. Москва, ул. Тестовая, д. 1.",
            ],
            [[["Наименование", "Ед. изм.", "Количество"], ["Ноутбук", "шт", "10"]]],
        ),
        save(
            "3. Сопроводительная записка к заявке на приобретение.pdf",
            "Сопроводительная записка к заявке на приобретение",
            [
                "Прошу приобрести ноутбуки в соответствии с техническим заданием. Код ОКПД2: 26.20.11.110."
            ],
            [[["Наименование", "Ед. изм.", "Количество"], ["Ноутбук", "шт", qty_note]]],
        ),
        save(
            "4. Расчет НМЦ.pdf",
            "Расчет НМЦ методом сопоставимых рыночных цен (анализа рынка)",
            [
                "Основные требования к продукции: ноутбуки по техническому заданию.",
                "Место поставки: г. Москва. Срок начала и окончания исполнения договора: 30 дней.",
                "Условия оплаты: 7 рабочих дней. Порядок определения поставщиков: запрос цен.",
                "Перечень поставщиков, предоставивших ценовые предложения: три организации.",
                f"Рассчитанная величина НМЦ: {nmc_total} руб.",
            ],
            [
                [
                    [
                        "№",
                        "Наименование",
                        "Ед.изм.",
                        "Кол-во",
                        "Ставка НДС, %",
                        "Предложение №1",
                        "Предложение №2",
                        "Предложение №3",
                        "Средняя арифметическая цена за ед.изм.",
                        "Общая стоимость, руб. с НДС",
                    ],
                    [
                        "1",
                        "Ноутбук",
                        "шт",
                        "10",
                        "20",
                        "60 000,00",
                        "62 000,00",
                        "58 000,00",
                        "60 000,00",
                        nmc_total,
                    ],
                    ["", "Итого", "", "", "", "", "", "", "", nmc_total],
                ]
            ],
        ),
        save(
            "5. Запрос КП.pdf",
            "Запрос о предоставлении информации о ценах",
            [
                "Прошу направить коммерческое предложение на поставку ноутбуков.",
                "Требования к продукции: см. техническое задание. Место поставки: г. Москва. Срок поставки: 30 дней. Условия оплаты: в течение 7 рабочих дней.",
                "Срок предоставления информации: не позднее 10.11.2026, цена каждой единицы продукции обязательна.",
                "Проведение сбора информации не влечёт возникновения обязательств заказчика.",
                "Допуски и лицензии не требуются.",
                "Адресаты: ООО «Поставщик Один», ООО «Поставщик Два», ООО «Поставщик Три».",
            ],
        ),
    ]
    for n in range(1, kp + 1):
        name, inn, kpp = SUPPLIERS[n - 1]
        files.append(
            save(
                f"{5 + n}. Коммерческое предложение №{n}.pdf",
                f"Коммерческое предложение № {n} от 0{4 + n}.11.2026",
                [
                    f"{name}, ИНН {inn}, КПП {kpp}",
                    "Предлагаем поставить ноутбуки: масса не более 2 кг, толщина не более 20 мм, блок питания 65 Вт, гарантия 36 мес. Количество: 10 шт.",
                    "Срок поставки: 30 дней. Оплата: в течение 7 рабочих дней.",
                    f"Итого: {total[n - 1]} руб. с НДС",
                    "Подпись руководителя, М.П.",
                ],
                [
                    [
                        [
                            "Наименование",
                            "Ед. изм.",
                            "Кол-во",
                            "Цена за единицу, руб. с НДС",
                            "Сумма, руб. с НДС",
                        ],
                        ["Ноутбук", "шт", "10", price[n - 1], total[n - 1]],
                    ]
                ],
            )
        )
    return files


def to_scans(files: list[Path]) -> None:
    """Заменяет каждый PDF «сканом»: страницы превращаются в картинки без текстового слоя."""
    import pypdfium2 as pdfium
    from PIL import Image

    for f in files:
        doc = pdfium.PdfDocument(str(f))
        pages = [doc[i].render(scale=2.2).to_pil().convert("L") for i in range(len(doc))]
        doc.close()
        pages[0].save(
            str(f),
            "PDF",
            resolution=160.0,
            save_all=True,
            append_images=[Image.fromarray(p) if False else p for p in pages[1:]],
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("folder", type=Path)
    ap.add_argument("--kp", type=int, default=3, choices=(1, 2, 3))
    ap.add_argument(
        "--bad",
        action="store_true",
        help="пакет с ошибками (расхождения, арифметика, неверный код)",
    )
    ap.add_argument(
        "--scan", action="store_true", help="сделать сканы (картинки без текстового слоя)"
    )
    ap.add_argument("--font", default=DEFAULT_FONT, help="TTF-шрифт с кириллицей")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    files = build(args.folder, kp=args.kp, bad=args.bad, font=args.font)
    if args.scan:
        to_scans(files)
    for f in files:
        print(f"{f.name}  {f.stat().st_size // 1024} КБ")
    print("Готово:", args.folder)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
