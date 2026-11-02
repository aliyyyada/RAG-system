"""Фейковый источник в MS SQL Server — только разработка и тесты.

Вместо настоящего SQL Server отдаёт те же две таблицы, что функция источника
(скриншот от 02.11.2026): метаданные документов и бинарники с типом файла.
Подключается как адаптер `mssql` с настройкой `"driver": "fake"`
(`python -m app.cli seed-fake-mssql`), в тестах — через set_runner_override.

| Документ                              | Что проверяем                                      |
|---------------------------------------|----------------------------------------------------|
| Приказ 537к (PDF)                     | текстовый слой, таблица в PDF                      |
| Протокол 151 (DOCX)                   | DOCX                                               |
| Указание 149 (TIFF, 2 страницы)       | скан-картинка → OCR постранично                    |
| Указание 159 (без файла)              | документ только из метаданных                      |
| Указание 162 (старый .doc)            | понятная ошибка «формат .doc не поддерживается»    |
| Приказ 655к (пустой BinaryFile)       | ошибка проверки полноты                            |
| Файл без строки в первой таблице      | лишний бинарник пропускается                       |

Права: если в настройках источника `access_mode = "sql"` и `access_sql` вызывает
`dbo.GetUserDocuments`, фейк отвечает по таблице ACCESS. Тесты меняют состояние через
FAKE_MSSQL: change() — документ изменился, remove() — пропал из ответа функции,
unavailable — сервер «лёг», calls — сколько раз вызывали функцию.
"""

import copy
import re
import threading
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from app.sources.mssql import ResultSet

FILES_DIR = Path(__file__).resolve().parent / "files"

META_COLUMNS = ["ID_GUID", "ID_Вид", "ID_Key", "Номер", "Дата", "Тип", "Тема", "Исполнитель"]
FILE_COLUMNS = ["ID_GUID", "BinaryFile", "FileType"]

# Заголовок старого Word (OLE) — разбирать его мы пока не умеем.
OLD_DOC = bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 504

G_537 = "9F7248C8-BEDC-4103-A896-F89E687A28BF"
G_151 = "6AA369A9-D214-43D7-BAC0-3EBFDD1A0FC2"
G_149 = "FFC08668-BA69-43B4-9C19-91C86DCA2821"
G_159 = "FD861EC4-59AA-4D13-BFC9-191A42927F47"
G_162 = "A6761DC4-AA40-452E-BC4D-2C3F3602F587"
G_655 = "41C3DB2E-1459-48EA-B977-E868870F31F4"
G_ORPHAN = "965E95A-E4C3-437C-8C1F-4FE7B7709BFB"


def _file(name: str) -> bytes:
    return (FILES_DIR / name).read_bytes()


def _seed() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    meta = [
        {
            "ID_GUID": G_537,
            "ID_Вид": 1,
            "ID_Key": 17,
            "Номер": "537к",
            "Дата": datetime(2011, 5, 12),
            "Тип": "ПРИКАЗ",
            "Тема": "О допуске персонала к работам на высоте",
            "Исполнитель": "Иванова А. С.",
        },
        {
            "ID_GUID": G_151,
            "ID_Вид": 1,
            "ID_Key": 18,
            "Номер": "151",
            "Дата": datetime(2011, 6, 2),
            "Тип": "ПРОТОКОЛ",
            "Тема": "Инструкция по охране труда для слесаря-ремонтника",
            "Исполнитель": "Петров В. Н.",
        },
        {
            "ID_GUID": G_149,
            "ID_Вид": 1,
            "ID_Key": 19,
            "Номер": "149",
            "Дата": datetime(2011, 5, 31),
            "Тип": "УКАЗАНИЕ",
            "Тема": "О проверке огнетушителей",
            "Исполнитель": "Орлова М. И.",
        },
        {
            "ID_GUID": G_159,
            "ID_Вид": 1,
            "ID_Key": 20,
            "Номер": "159",
            "Дата": datetime(2011, 6, 7),
            "Тип": "УКАЗАНИЕ",
            "Тема": "О выводе оборудования из аренды",
            "Исполнитель": None,
        },
        {
            "ID_GUID": G_162,
            "ID_Вид": 1,
            "ID_Key": 21,
            "Номер": "162",
            "Дата": datetime(2011, 6, 7),
            "Тип": "УКАЗАНИЕ",
            "Тема": "О ремонте кровли склада",
            "Исполнитель": "Смирнов О. Л.",
        },
        {
            "ID_GUID": G_655,
            "ID_Вид": 1,
            "ID_Key": 23,
            "Номер": "655к",
            "Дата": datetime(2011, 6, 7),
            "Тип": "ПРИКАЗ",
            "Тема": "О командировке",
            "Исполнитель": "Иванова А. С.",
        },
    ]
    files = [
        {"ID_GUID": G_537, "BinaryFile": _file("prikaz_112.pdf"), "FileType": "pdf"},
        {"ID_GUID": G_151, "BinaryFile": _file("iot_044.docx"), "FileType": "docx"},
        {"ID_GUID": G_149, "BinaryFile": _file("ukazanie_149_scan.tif"), "FileType": "tif"},
        {"ID_GUID": G_162, "BinaryFile": OLD_DOC, "FileType": "doc"},
        {"ID_GUID": G_655, "BinaryFile": b"", "FileType": "pdf"},
        {"ID_GUID": G_ORPHAN, "BinaryFile": _file("reglament_to.pdf"), "FileType": "pdf"},
    ]
    return meta, files


# Кто что видит при access_mode = "sql". Кого нет в таблице — пусто.
ACCESS: dict[str, set[str] | str] = {
    "T-0001": {G_537, G_149},
    "T-0002": "*",
    "T-0003": set(),
    "T-0004": "*",
}


class FakeMssql:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.meta, self.files = _seed()
            self.unavailable = False
            self.calls: list[tuple[str, dict[str, Any]]] = []
            self.access: dict[str, set[str] | str] = copy.deepcopy(ACCESS)

    # -- изменения из тестов ------------------------------------------------------

    def change(self, guid: str, **fields: Any) -> None:
        for row in self.meta:
            if row["ID_GUID"] == guid:
                row.update(fields)

    def replace_file(self, guid: str, data: bytes, file_type: str = "pdf") -> None:
        self.files = [f for f in self.files if f["ID_GUID"] != guid]
        self.files.append({"ID_GUID": guid, "BinaryFile": data, "FileType": file_type})

    def remove(self, guid: str) -> None:
        self.meta = [r for r in self.meta if r["ID_GUID"] != guid]
        self.files = [f for f in self.files if f["ID_GUID"] != guid]

    # -- SqlRunner -------------------------------------------------------------------

    def execute(self, sql: str, params: dict[str, Any]) -> Iterator[ResultSet]:
        with self._lock:
            self.calls.append((sql, dict(params)))
            if self.unavailable:
                from app.sources.base import SourceUnavailable

                raise SourceUnavailable("SQL Server не отвечает: TimeoutError")
            meta = copy.deepcopy(self.meta)
            files = list(self.files)
        if re.search(r"GetUserDocuments", sql, re.IGNORECASE):
            allowed = self.access.get(str(params.get("user")), set())
            ids = [r["ID_GUID"] for r in meta] if allowed == "*" else sorted(allowed)
            yield ResultSet(["ID_GUID"], [(i,) for i in ids])
            return
        if re.search(r"GetCommonDocuments", sql, re.IGNORECASE):
            yield ResultSet(["ID_GUID"], [(G_151,)])
            return
        yield ResultSet(META_COLUMNS, ([r[c] for c in META_COLUMNS] for r in meta))
        yield ResultSet(FILE_COLUMNS, ([f[c] for c in FILE_COLUMNS] for f in files))

    @property
    def main_calls(self) -> int:
        return sum(1 for sql, _ in self.calls if "GetDocuments" in sql)


FAKE_MSSQL = FakeMssql()
