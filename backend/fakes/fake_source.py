"""Фейковый источник документов («Документооборот») — только разработка и тесты.

Говорит на протоколе http_json (docs/source-protocol.md) и отдаёт набор документов,
на котором видны все ветки загрузки:

| Документ                                   | Что проверяем                                  |
|--------------------------------------------|------------------------------------------------|
| Приказ № 112 (PDF + «Ознакомлены», 46 стр.)| текстовый слой, таблица в PDF, табличная часть |
| Приказ № 45 (скан)                         | OCR                                            |
| Приказ № 140 (без файла)                   | документ только из шапки и табличной части     |
| ИОТ-044 (DOCX), Положение о СУОТ (DOCX)    | DOCX, общие документы (доступны без входа)     |
| Регламент ТО (PDF, таблица на 60 строк)    | большая таблица режется по строкам             |
| Приказ № 97 (PDF 0 байт)                   | ошибка парсинга                                |
| Журнал инструктажей (148 строк, пришло 120)| ошибка проверки полноты                        |
| Приказ № 12 (удалён)                       | мягкое удаление                                |

Права (этап 3): `GET /access/users/{табельный}` отдаёт id документов, доступных человеку
(таблица ACCESS ниже). Второй источник «Архив ОТ» (/archive, два документа без файлов)
нужен, чтобы проверить параллельный опрос источников и «один источник упал, другой работает».

Тесты меняют состояние через объекты SOURCE и ARCHIVE: touch() — документ изменился,
delete() — удалён в источнике, drop() — пропал совсем, unavailable — источник «лёг»,
access_delay — источник отвечает на запрос прав медленно, access_calls — сколько раз
нас спросили о правах (проверка кэша).
"""

import asyncio
import copy
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response

FILES_DIR = Path(__file__).resolve().parent / "files"
_NS = uuid.UUID("6f1c2a4e-1d1b-4c8e-9a51-0b7d2f3c9e10")

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def guid(name: str) -> str:
    return str(uuid.uuid5(_NS, name))


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def _acquainted(n: int) -> list[dict[str, Any]]:
    people = [
        ("20871", "Петров В. Н.", "Мастер участка"),
        ("10234", "Иванова А. С.", "Начальник службы ОТ и ПБ"),
        ("31455", "Орлова М. И.", "Слесарь-сборщик"),
    ]
    rows = []
    for i in range(n):
        tab, name, pos = people[i % len(people)]
        rows.append(
            {
                "personnel_number": f"{int(tab) + i // 3:05d}",
                "full_name": name if i < 3 else f"{name} ({i})",
                "position": pos,
                "acquainted_at": f"2024-03-{15 + i % 10:02d}",
            }
        )
    return rows


def _seed() -> dict[str, dict[str, Any]]:
    docs: list[dict[str, Any]] = [
        {
            "key": "prikaz-112",
            "header": {
                "doc_type": "Приказ",
                "number": "112",
                "date": "2024-03-14",
                "title": "О допуске персонала к работам на высоте",
                "topic": "Охрана труда",
                "department": "Служба ОТ и ПБ",
                "status": "Действует",
            },
            "modified_at": "2024-05-20T08:42:00",
            "parts": [{"name": "Ознакомлены", "rows": _acquainted(46)}],
            "files": [("prikaz_112.pdf", PDF)],
        },
        {
            "key": "prikaz-45",
            "header": {
                "doc_type": "Приказ",
                "number": "45",
                "date": "2022-01-10",
                "title": "О работах на высоте",
                "topic": "Охрана труда",
                "department": "Служба ОТ и ПБ",
                "status": "Отменён",
            },
            "modified_at": "2024-03-14T10:00:00",
            "files": [("prikaz_45_scan.pdf", PDF)],
        },
        {
            "key": "prikaz-140",
            "header": {
                "doc_type": "Приказ",
                "number": "140",
                "date": "2024-05-20",
                "title": "О внесении изменений в приказ № 112 от 14.03.2024",
                "topic": "Охрана труда",
                "department": "Служба ОТ и ПБ",
                "description": (
                    "Пункт 4.2 приказа № 112 изложить в новой редакции: наряд-допуск "
                    "выдаётся на срок не более 10 календарных дней."
                ),
            },
            "modified_at": "2024-05-20T09:00:00",
            "parts": [
                {
                    "name": "Изменения",
                    "rows": [
                        {"Пункт": "4.2", "Было": "15 календарных дней", "Стало": "10 дней"},
                    ],
                }
            ],
        },
        {
            "key": "iot-044",
            "common": True,
            "header": {
                "doc_type": "Инструкция",
                "number": "ИОТ-044",
                "date": "2025-02-01",
                "title": "Инструкция по охране труда для электросварщика",
                "topic": "Охрана труда",
            },
            "modified_at": "2026-09-17T12:02:00",
            "files": [("iot_044.docx", DOCX)],
        },
        {
            "key": "polozhenie-suot",
            "common": True,
            "header": {
                "doc_type": "Положение",
                "number": "П-01",
                "date": "2023-09-01",
                "title": "Положение о системе управления охраной труда",
                "topic": "Охрана труда",
            },
            "modified_at": "2025-01-10T07:00:00",
            "files": [("polozhenie_suot.docx", DOCX)],
        },
        {
            "key": "reglament-to",
            "header": {
                "doc_type": "Регламент",
                "number": "РТ-7",
                "date": "2025-06-02",
                "title": "Регламент технического обслуживания грузоподъёмного оборудования",
                "topic": "Техническое обслуживание",
                "department": "Литейный цех № 2",
            },
            "modified_at": "2025-06-03T06:30:00",
            "files": [("reglament_to.pdf", PDF)],
        },
        {
            "key": "prikaz-97",
            "header": {
                "doc_type": "Приказ",
                "number": "97",
                "date": "2019-07-03",
                "title": "О проведении внеплановых инструктажей",
                "topic": "Охрана труда",
            },
            "modified_at": "2025-08-12T07:14:00",
            "files": [("prikaz_97.pdf", PDF)],
        },
        {
            "key": "journal-f3",
            "header": {
                "doc_type": "Журнал",
                "number": "Ф-3",
                "date": "2026-01-09",
                "title": "Журнал регистрации инструктажей, форма 3",
                "topic": "Охрана труда",
            },
            "modified_at": "2026-10-01T06:30:00",
            # Источник объявил 148 строк, а отдал 120: обрезанная выгрузка.
            "parts": [{"name": "Строки", "rows": _acquainted(120), "row_count": 148}],
        },
        {
            "key": "prikaz-12",
            "header": {
                "doc_type": "Приказ",
                "number": "12",
                "date": "2020-02-01",
                "title": "Об утверждении графика отпусков",
                "topic": "Кадры",
            },
            "modified_at": "2021-01-01T00:00:00",
            "deleted_at": "2021-01-01T00:00:00",
        },
    ]
    return _build(docs)


def _build(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for d in docs:
        doc_id = guid(d["key"])
        files = []
        for name, mime in d.get("files", []):
            path = FILES_DIR / name
            files.append(
                {
                    "id": guid("file:" + name),
                    "name": name,
                    "size": path.stat().st_size,
                    "mime": mime,
                    "path": path,
                }
            )
        parts = [
            {
                "name": p["name"],
                "row_count": p.get("row_count", len(p["rows"])),
                "rows": [{"document_id": doc_id, **r} for r in p["rows"]],
            }
            for p in d.get("parts", [])
        ]
        out[doc_id] = {
            "key": d["key"],
            "header": {"id": doc_id, **d["header"]},
            "modified_at": _ts(d["modified_at"]),
            "deleted_at": _ts(d["deleted_at"]) if d.get("deleted_at") else None,
            "common": d.get("common", False),
            "parts": parts,
            "files": files,
        }
    return out


# Кто какие документы видит в «Документообороте». "*" — все. Общие документы (ИОТ-044,
# Положение о СУОТ) видны всем и без этого списка. Кого нет в таблице — источник его не знает.
DOCFLOW_ACCESS: dict[str, set[str] | str] = {
    "T-0001": {"prikaz-112", "prikaz-140", "reglament-to"},  # обычный пользователь
    "T-0002": "*",  # аналитик
    "T-0003": set(),  # «без доступа к источнику X»
    "T-0004": "*",  # администратор (права на документы ему тоже даёт источник)
    "10234": {"prikaz-112", "prikaz-45", "prikaz-140", "prikaz-97", "journal-f3"},  # ОТ и ПБ
    "00017": "*",  # отдел ИТ
    "20871": {"prikaz-112", "reglament-to"},  # мастер литейного цеха
    "31455": set(),  # слесарь: только общие документы
}


def _seed_archive() -> dict[str, dict[str, Any]]:
    docs = [
        {
            "key": "archive-ri-12",
            "header": {
                "doc_type": "Инструкция",
                "number": "РИ-12",
                "date": "2020-11-23",
                "title": "Рабочая инструкция машиниста мостового крана",
                "topic": "Грузоподъёмные механизмы",
                "description": "Машинист проверяет тормоза и концевые выключатели перед сменой.",
            },
            "modified_at": "2021-02-01T08:00:00",
            "parts": [
                {
                    "name": "Проверки перед сменой",
                    "rows": [
                        {"Узел": "Тормоза", "Что проверить": "Работу на холостом ходу"},
                        {"Узел": "Концевые выключатели", "Что проверить": "Срабатывание"},
                    ],
                }
            ],
        },
        {
            "key": "archive-akt-5",
            "header": {
                "doc_type": "Акт",
                "number": "5",
                "date": "2019-04-12",
                "title": "Акт проверки знаний стропальщиков",
                "topic": "Охрана труда",
                "description": "Комиссия проверила знания 12 стропальщиков литейного цеха № 2.",
            },
            "modified_at": "2019-04-12T10:00:00",
        },
    ]
    return _build(docs)


ARCHIVE_ACCESS: dict[str, set[str] | str] = {
    "T-0001": {"archive-ri-12"},
    "T-0002": "*",
    "T-0004": "*",
    "10234": "*",
    "00017": "*",
}


class FakeSource:
    def __init__(self, seed: Any, access: dict[str, set[str] | str]) -> None:
        self._seed = seed
        self._access = access
        self.reset()

    def reset(self) -> None:
        self.docs = self._seed()
        self.unavailable = False
        # Подмена содержимого файла (например, «повредить» файл в тесте).
        self.file_override: dict[str, bytes] = {}
        self.access = copy.deepcopy(self._access)
        self.access_delay = 0.0
        self.access_calls = 0

    def id_of(self, key: str) -> str:
        return guid(key)

    def touch(self, key: str, *, at: datetime | None = None, **header: Any) -> None:
        doc = self.docs[guid(key)]
        doc["modified_at"] = at or max(datetime.now(UTC), doc["modified_at"] + timedelta(seconds=1))
        doc["header"].update(header)

    def delete(self, key: str) -> None:
        doc = self.docs[guid(key)]
        doc["deleted_at"] = doc["modified_at"] = datetime.now(UTC)

    def drop(self, key: str) -> None:
        self.docs.pop(guid(key))

    def allowed_ids(self, personnel_number: str) -> list[str] | None:
        rule = self.access.get(personnel_number)
        if rule is None:
            return None
        if rule == "*":
            return sorted(self.docs)
        return sorted(guid(k) for k in rule if guid(k) in self.docs)

    def _files(self) -> dict[str, dict[str, Any]]:
        return {f["id"]: f for d in self.docs.values() for f in d["files"]}


SOURCE = FakeSource(_seed, DOCFLOW_ACCESS)
ARCHIVE = FakeSource(_seed_archive, ARCHIVE_ACCESS)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def make_router(state: FakeSource, prefix: str) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=[prefix.strip("/")])

    def _check_up() -> None:
        if state.unavailable:
            raise HTTPException(503, "Источник временно недоступен")

    @router.get("/documents")
    async def list_documents(
        modified_since: datetime | None = None,
        cursor: str | None = None,
        limit: int = Query(500, ge=1, le=5000),
    ) -> dict[str, Any]:
        _check_up()
        items = sorted(state.docs.items(), key=lambda kv: (kv[1]["modified_at"], kv[0]))
        if modified_since is not None:
            items = [kv for kv in items if kv[1]["modified_at"] >= modified_since]
        start = int(cursor or 0)
        page = items[start : start + limit]
        nxt = start + limit if start + limit < len(items) else None
        return {
            "items": [
                {
                    "id": k,
                    "modified_at": _iso(d["modified_at"]),
                    "deleted_at": _iso(d["deleted_at"]),
                }
                for k, d in page
            ],
            "next_cursor": str(nxt) if nxt is not None else None,
        }

    @router.get("/documents/{doc_id}")
    async def get_document(doc_id: str) -> dict[str, Any]:
        _check_up()
        d = state.docs.get(doc_id)
        if d is None:
            raise HTTPException(404, "Нет такого документа")
        header = copy.deepcopy(d["header"])
        header["modified_at"] = _iso(d["modified_at"])
        header["deleted_at"] = _iso(d["deleted_at"])
        return {
            "header": header,
            "parts": copy.deepcopy(d["parts"]),
            "files": [{k: v for k, v in f.items() if k != "path"} for f in d["files"]],
        }

    @router.get("/files/{file_id}", response_model=None)
    async def get_file(file_id: str) -> Any:
        _check_up()
        if file_id in state.file_override:
            return Response(state.file_override[file_id], media_type="application/octet-stream")
        f = state._files().get(file_id)
        if f is None:
            raise HTTPException(404, "Нет такого файла")
        return FileResponse(f["path"], media_type=f["mime"], filename=f["name"])

    @router.get("/access/common")
    async def common_ids() -> dict[str, list[str]]:
        _check_up()
        return {"ids": sorted(k for k, d in state.docs.items() if d["common"])}

    @router.get("/access/users/{personnel_number}")
    async def user_ids(personnel_number: str) -> dict[str, list[str]]:
        state.access_calls += 1
        if state.access_delay:
            await asyncio.sleep(state.access_delay)
        _check_up()
        ids = state.allowed_ids(personnel_number)
        if ids is None:
            raise HTTPException(404, "Сотрудник не найден")
        return {"ids": ids}

    return router


router = make_router(SOURCE, "/source")
archive_router = make_router(ARCHIVE, "/archive")
