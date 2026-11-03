"""Синтетические Word-файлы «в стиле ЕПоЗ» для тестов (настоящие ЕПоЗ в репозиторий не кладём).

Стили названы так же, как в настоящем Положении («[Ростех] … (Уровень N)»), оглавление — абзацы
«toc N» с номерами, набранными текстом, как у Word.
"""

from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.style import WD_STYLE_TYPE

S1 = "[Ростех] Наименование Главы (Уровень 1)"
S2 = "[Ростех] Наименование Раздела (Уровень 2)"
S3 = "[Ростех] Наименование Подраздела (Уровень 3)"
S4 = "[Ростех] Текст Пункта (Уровень 4)"
S5 = "[Ростех] Текст Подпункта (Уровень 5)"
S6 = "[Ростех] Текст Подпункта подпункта (Уровень 6)"


def _styles(doc: Document) -> None:
    for name in (S1, S2, S3, S4, S5, S6, "toc 1", "toc 2", "toc 3"):
        doc.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)


def make_main_docx(path: Path, *, edition: str = "A") -> Path:
    """Мини-Положение: 1 глава, 2 раздела, 3 подраздела, пункты, подпункты, ссылка «п. 1.1.2»."""
    d = Document()
    _styles(d)
    for style, text in (
        ("toc 1", "Глава 1.\tОбщие положения\t3"),
        ("toc 2", "1.\tПервый раздел\t3"),
        ("toc 3", "1.1\tТребования к продукции\t3"),
        ("toc 3", "1.2\tВторой подраздел\t4"),
        ("toc 2", "2.\tВторой раздел\t5"),
        ("toc 3", "2.1\tТретий подраздел\t5"),
    ):
        d.add_paragraph(text, style=style)
    d.add_paragraph("Общие положения", style=S1)
    d.add_paragraph("Первый раздел", style=S2)
    d.add_paragraph("Требования к продукции", style=S3)
    d.add_paragraph(
        f"Требования к продукции устанавливаются заказчиком. Редакция {edition}.", style=S4
    )
    d.add_paragraph("Срок оплаты поставленного товара не более семи рабочих дней.", style=S4)
    d.add_paragraph("первый подпункт срока оплаты", style=S5)
    d.add_paragraph("второй подпункт срока оплаты", style=S5)
    d.add_paragraph("вложенный подпункт", style=S6)
    d.add_paragraph("Второй подраздел", style=S3)
    d.add_paragraph("Правило срока оплаты описано в п. 1.1.2 настоящего Положения.", style=S4)
    d.add_paragraph("Второй раздел", style=S2)
    d.add_paragraph("Третий подраздел", style=S3)
    for i in range(1, 19):  # чтобы набралось больше 20 абзацев со стилями уровней
        d.add_paragraph(f"Условие номер {i} третьего подраздела.", style=S4)
    d.save(str(path))
    return path


def make_app_docx(path: Path, *, title: str = "Приложение", marker: str = "марка") -> Path:
    """Мини-приложение без оглавления: номера пунктов набраны в тексте."""
    d = Document()
    d.add_paragraph(f"{title}")
    d.add_paragraph("1. Общие положения")
    d.add_paragraph(f"1.1. Настоящий порядок применяется ко всем закупкам, {marker}.")
    d.add_paragraph("1.2. Для расчёта не используется информация из анонимных источников.")
    d.save(str(path))
    return path


def make_matrix_docx(path: Path) -> Path:
    """Таблица-матрица с объединёнными ячейками в шапке (как Приложение 2)."""
    d = Document()
    t = d.add_table(rows=4, cols=7)
    t.cell(0, 0).text = "Способ закупки"
    a = t.cell(0, 1).merge(t.cell(0, 3))
    a.text = "Конкурс"
    b = t.cell(0, 4).merge(t.cell(0, 6))
    b.text = "Аукцион"
    t.cell(1, 0).text = "Тип заказчика"
    t.cell(1, 1).text = "I группы"
    t.cell(1, 2).text = "II группы"
    t.cell(1, 3).text = "ИДО"
    t.cell(1, 4).text = "I группы"
    t.cell(1, 5).text = "II группы"
    t.cell(1, 6).text = "ИДО"
    t.cell(2, 0).text = "Минимальный срок подачи заявок"
    t.cell(2, 1).text = "15 дней"
    c = t.cell(2, 2).merge(t.cell(2, 3))
    c.text = "30 дней"
    t.cell(2, 4).text = "10 дней"
    d2 = t.cell(2, 5).merge(t.cell(2, 6))
    d2.text = "не применимо"
    t.cell(3, 0).text = "Обеспечение заявки"
    for j in range(1, 7):
        t.cell(3, j).text = "допускается"
    d.save(str(path))
    return path
