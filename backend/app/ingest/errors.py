"""Ошибки загрузки документа с указанием этапа — их видит администратор в отчёте."""

from typing import Literal

Stage = Literal["fetch", "completeness", "parse", "ocr", "store"]

STAGE_TITLES: dict[str, str] = {
    "fetch": "Получение",
    "completeness": "Проверка полноты",
    "parse": "Парсинг",
    "ocr": "OCR",
    "store": "Сохранение",
}


class IngestError(Exception):
    def __init__(self, stage: Stage, message: str):
        super().__init__(message)
        self.stage = stage
        self.message = message


class ParseError(IngestError):
    def __init__(self, message: str, stage: Stage = "parse"):
        super().__init__(stage, message)
