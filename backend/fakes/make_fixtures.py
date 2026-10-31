"""Генератор тестовых файлов фейкового источника (fakes/files/*.pdf, *.docx).

Готовые файлы лежат в репозитории, запускать этот скрипт нужно, только если меняете
содержимое тестовых документов:

    pip install reportlab==5.1.0          # BSD, только для генерации, в образ не входит
    python -m fakes.make_fixtures

Шрифт DejaVu Sans (свободная лицензия Bitstream Vera) берётся из системы: в PDF нужна
кириллица, а встроенные шрифты PDF её не содержат.
"""

import sys
from pathlib import Path

from docx import Document as new_docx
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent / "files"
FONT_DIRS = [Path("/usr/share/fonts/truetype/dejavu"), Path("C:/Windows/Fonts")]


def _font(name: str) -> Path:
    for d in FONT_DIRS:
        if (d / name).exists():
            return d / name
    sys.exit(f"Не найден шрифт {name}: установите fonts-dejavu-core")


def prikaz_112_pdf() -> None:
    """Приказ с текстовым слоем: разделы, пункты и таблица на второй странице."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table

    pdfmetrics.registerFont(TTFont("DejaVu", str(_font("DejaVuSans.ttf"))))
    pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(_font("DejaVuSans-Bold.ttf"))))
    body = ParagraphStyle("body", fontName="DejaVu", fontSize=11, leading=15, spaceAfter=8)
    h1 = ParagraphStyle("h1", fontName="DejaVu-Bold", fontSize=15, leading=20, spaceAfter=12)
    h2 = ParagraphStyle("h2", fontName="DejaVu-Bold", fontSize=12.5, leading=17, spaceAfter=8)

    story = [
        Paragraph("ПРИКАЗ № 112 от 14.03.2024", h1),
        Paragraph("О допуске персонала к работам на высоте", h2),
        Paragraph(
            "В целях обеспечения безопасности работ на высоте и во исполнение Правил по охране "
            "труда при работе на высоте ПРИКАЗЫВАЮ:",
            body,
        ),
        Paragraph("1. Общие положения", h2),
        Paragraph(
            "1.1. Настоящий приказ устанавливает порядок допуска работников к работам на "
            "высоте на всех участках предприятия.",
            body,
        ),
        Paragraph(
            "1.2. Приказ № 45 от 10.01.2022 «О работах на высоте» признать утратившим силу.",
            body,
        ),
        Paragraph("2. Ответственные", h2),
        Paragraph(
            "2.1. Ответственным за организацию работ на высоте назначить начальника службы "
            "ОТ и ПБ Иванову А. С.",
            body,
        ),
        Paragraph(
            "2.2. Ответственным руководителем работ в литейном цехе № 2 назначить мастера "
            "участка Петрова В. Н.",
            body,
        ),
        PageBreak(),
        Paragraph("4. Порядок допуска", h2),
        Paragraph(
            "4.1. К работам на высоте допускаются работники не моложе 18 лет, прошедшие "
            "медицинский осмотр, обучение и проверку знаний.",
            body,
        ),
        Paragraph(
            "4.2. Допуск к работам на высоте осуществляется по наряду-допуску (приложение 2), "
            "который подписывает ответственный руководитель работ. Наряд-допуск выдаётся на "
            "срок не более 15 календарных дней.",
            body,
        ),
        Paragraph("4.4. Периодичность обучения по группам безопасности:", body),
        Spacer(1, 6),
    ]
    table = Table(
        [
            ["Группа", "Кто", "Периодичность"],
            ["1", "Работники в составе бригады", "1 раз в 3 года"],
            ["2", "Мастера, бригадиры, стропальщики", "1 раз в 3 года"],
            ["3", "Ответственные руководители работ", "1 раз в 3 года"],
        ],
        colWidths=[60, 260, 120],
    )
    table.setStyle(
        [
            ("FONT", (0, 0), (-1, -1), "DejaVu", 10),
            ("GRID", (0, 0), (-1, -1), 0.7, colors.black),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]
    )
    story += [
        table,
        Spacer(1, 12),
        Paragraph("4.5. Контроль за исполнением приказа возложить на главного инженера.", body),
    ]
    SimpleDocTemplate(str(OUT / "prikaz_112.pdf"), pagesize=A4).build(story)


def reglament_to_pdf() -> None:
    """Большая таблица на несколько страниц: проверяем, что строки не режутся."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Table

    pdfmetrics.registerFont(TTFont("DejaVu", str(_font("DejaVuSans.ttf"))))
    h1 = ParagraphStyle("h1", fontName="DejaVu", fontSize=14, leading=19, spaceAfter=10)
    rows = [["№", "Оборудование", "Участок", "Вид ТО", "Периодичность"]]
    kinds = ["Осмотр", "ТО-1", "ТО-2", "Капитальный ремонт"]
    period = ["ежесменно", "1 раз в месяц", "1 раз в квартал", "1 раз в 3 года"]
    for i in range(1, 61):
        rows.append(
            [
                str(i),
                f"Кран мостовой КМ-{100 + i}",
                f"Участок {1 + i % 4}",
                kinds[i % 4],
                period[i % 4],
            ]
        )
    table = Table(rows, colWidths=[30, 170, 80, 110, 100], repeatRows=1)
    table.setStyle(
        [
            ("FONT", (0, 0), (-1, -1), "DejaVu", 9),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ]
    )
    doc = SimpleDocTemplate(str(OUT / "reglament_to.pdf"), pagesize=A4)
    doc.build(
        [Paragraph("РЕГЛАМЕНТ ТЕХНИЧЕСКОГО ОБСЛУЖИВАНИЯ ГРУЗОПОДЪЁМНОГО ОБОРУДОВАНИЯ", h1), table]
    )


def scan_pdf() -> None:
    """Скан без текстового слоя: картинка страницы. Текст достаётся только OCR."""
    font = ImageFont.truetype(str(_font("DejaVuSans.ttf")), 34)
    bold = ImageFont.truetype(str(_font("DejaVuSans-Bold.ttf")), 40)
    img = Image.new("L", (1654, 2339), 255)  # A4, 200 dpi
    draw = ImageDraw.Draw(img)
    y = 160
    draw.text((160, y), "ПРИКАЗ № 45 от 10.01.2022", font=bold, fill=0)
    y += 110
    lines = [
        "О работах на высоте",
        "",
        "1. Работы на высоте выполнять только по наряду-допуску.",
        "2. Наряд-допуск выдаётся на срок не более 10 дней.",
        "3. Ответственным за работы на высоте назначить",
        "главного инженера Смирнова О. Л.",
    ]
    for line in lines:
        draw.text((160, y), line, font=font, fill=0)
        y += 60
    img.save(OUT / "prikaz_45_scan.pdf", "PDF", resolution=200.0)


def iot_docx() -> None:
    doc = new_docx()
    doc.add_heading("ИОТ-044. Инструкция по охране труда для электросварщика", level=0)
    doc.add_heading("1. Общие требования охраны труда", level=1)
    doc.add_paragraph(
        "1.1. К самостоятельной работе электросварщиком допускаются лица не моложе 18 лет, "
        "имеющие группу по электробезопасности не ниже II."
    )
    doc.add_paragraph(
        "1.2. Электросварщик обязан применять средства индивидуальной защиты: щиток "
        "с светофильтром, брезентовый костюм, рукавицы, ботинки."
    )
    doc.add_heading("2. Требования перед началом работы", level=1)
    doc.add_paragraph(
        "2.1. Проверить исправность сварочного аппарата, изоляции проводов и заземления."
    )
    doc.add_heading("3. Нормы выдачи СИЗ", level=1)
    t = doc.add_table(rows=1, cols=3)
    t.rows[0].cells[0].text = "СИЗ"
    t.rows[0].cells[1].text = "Количество"
    t.rows[0].cells[2].text = "Срок носки"
    for name, qty, term in [
        ("Костюм брезентовый", "1 шт.", "12 месяцев"),
        ("Рукавицы брезентовые", "12 пар", "12 месяцев"),
        ("Щиток защитный", "1 шт.", "до износа"),
    ]:
        cells = t.add_row().cells
        cells[0].text, cells[1].text, cells[2].text = name, qty, term
    doc.add_heading("4. Требования по окончании работы", level=1)
    doc.add_paragraph("4.1. Отключить сварочный аппарат от сети и убрать рабочее место.")
    doc.save(OUT / "iot_044.docx")


def polozhenie_docx() -> None:
    doc = new_docx()
    doc.add_heading("Положение о системе управления охраной труда", level=0)
    doc.add_heading("1. Назначение", level=1)
    doc.add_paragraph(
        "1.1. Положение определяет порядок функционирования системы управления охраной труда "
        "(СУОТ) на предприятии и обязательно для всех работников."
    )
    doc.add_heading("2. Обязанности работников", level=1)
    doc.add_paragraph(
        "2.1. Работник обязан соблюдать требования охраны труда, проходить обучение, "
        "инструктажи и медицинские осмотры."
    )
    doc.add_paragraph(
        "2.2. О каждом несчастном случае работник немедленно сообщает непосредственному "
        "руководителю."
    )
    doc.save(OUT / "polozhenie_suot.docx")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    prikaz_112_pdf()
    reglament_to_pdf()
    scan_pdf()
    iot_docx()
    polozhenie_docx()
    (OUT / "prikaz_97.pdf").write_bytes(b"")  # битый файл: 0 байт
    print("Готово:", ", ".join(sorted(p.name for p in OUT.iterdir())))


if __name__ == "__main__":
    main()
