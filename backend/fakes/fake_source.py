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

Тесты меняют состояние через объект SOURCE: touch() — документ изменился,
delete() — удалён в источнике, drop() — пропал совсем, unavailable — источник «лёг».
"""

import copy
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

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


class FakeSource:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.docs = _seed()
        self.unavailable = False
        # Подмена содержимого файла (например, «повредить» файл в тесте).
        self.file_override: dict[str, bytes] = {}

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

    def _files(self) -> dict[str, dict[str, Any]]:
        return {f["id"]: f for d in self.docs.values() for f in d["files"]}


SOURCE = FakeSource()
router = APIRouter(prefix="/source", tags=["source"])


def _check_up() -> None:
    if SOURCE.unavailable:
        raise HTTPException(503, "Источник временно недоступен")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


@router.get("/documents")
async def list_documents(
    modified_since: datetime | None = None,
    cursor: str | None = None,
    limit: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    _check_up()
    items = sorted(SOURCE.docs.items(), key=lambda kv: (kv[1]["modified_at"], kv[0]))
    if modified_since is not None:
        items = [kv for kv in items if kv[1]["modified_at"] >= modified_since]
    start = int(cursor or 0)
    page = items[start : start + limit]
    nxt = start + limit if start + limit < len(items) else None
    return {
        "items": [
            {"id": k, "modified_at": _iso(d["modified_at"]), "deleted_at": _iso(d["deleted_at"])}
            for k, d in page
        ],
        "next_cursor": str(nxt) if nxt is not None else None,
    }


@router.get("/documents/{doc_id}")
async def get_document(doc_id: str) -> dict[str, Any]:
    _check_up()
    d = SOURCE.docs.get(doc_id)
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
    from fastapi.responses import Response

    if file_id in SOURCE.file_override:
        return Response(SOURCE.file_override[file_id], media_type="application/octet-stream")
    f = SOURCE._files().get(file_id)
    if f is None:
        raise HTTPException(404, "Нет такого файла")
    return FileResponse(f["path"], media_type=f["mime"], filename=f["name"])


@router.get("/access/common")
async def common_ids() -> dict[str, list[str]]:
    _check_up()
    return {"ids": sorted(k for k, d in SOURCE.docs.items() if d["common"])}
