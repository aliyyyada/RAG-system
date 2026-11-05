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

    def __reduce__(self):  # type: ignore[no-untyped-def]
        # Разбор файла идёт в отдельном процессе (parse_pool), ошибка возвращается оттуда
        # через pickle. Без этого pickle вызвал бы IngestError(message) и потерял этап.
        return _rebuild, (type(self), self.stage, self.message)


class ParseError(IngestError):
    def __init__(self, message: str, stage: Stage = "parse"):
        super().__init__(stage, message)


def _rebuild(cls: type[IngestError], stage: Stage, message: str) -> IngestError:
    err = cls.__new__(cls)
    IngestError.__init__(err, stage, message)
    return err
