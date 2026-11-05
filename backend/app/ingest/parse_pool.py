"""Разбор файлов в отдельных процессах, которые время от времени перезапускаются.

Зачем. Разбор PDF (pdfplumber) и OCR сканов (картинка страницы в 300 dpi — около 25 МБ)
занимают в памяти сотни мегабайт. Python возвращает такую память операционной системе
плохо: после разбора большого скана worker так и остаётся «толстым», а за сотни документов
расход растёт до лимита контейнера. Если разбирать файл в дочернем процессе и после
`parse_tasks_per_process` файлов заменять процесс новым, вся его память гарантированно
возвращается системе.

Второй плюс: разбор (CPU) не держит цикл worker'а, и пока один документ разбирается,
worker в это же время отправляет в видеокарту фрагменты предыдущего (см. worker/main.py).

    kind, parsed = await parse_file_async(path, "Приказ 12.pdf", settings)

`parse_processes = 0` — разбор в потоке текущего процесса, как раньше (так работают тесты).
"""

import asyncio
import logging
import multiprocessing
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

from app.config import Settings
from app.ingest.errors import ParseError
from app.ingest.files import FileType, parse_file
from app.ingest.parsed import ParsedFile

log = logging.getLogger(__name__)

_pool: ProcessPoolExecutor | None = None
_pool_key: tuple[int, int] | None = None
_lock = threading.Lock()


def _get_pool(settings: Settings) -> ProcessPoolExecutor:
    global _pool, _pool_key
    key = (settings.parse_processes, settings.parse_tasks_per_process)
    with _lock:
        if _pool is None or _pool_key != key:
            if _pool is not None:
                _pool.shutdown(wait=False, cancel_futures=False)
            # spawn, а не fork: дочерний процесс не наследует соединения с БД и потоки
            # worker'а, а max_tasks_per_child с fork не работает.
            _pool = ProcessPoolExecutor(
                max_workers=settings.parse_processes,
                mp_context=multiprocessing.get_context("spawn"),
                max_tasks_per_child=settings.parse_tasks_per_process,
            )
            _pool_key = key
        return _pool


def _reset_pool() -> None:
    global _pool, _pool_key
    with _lock:
        if _pool is not None:
            _pool.shutdown(wait=False, cancel_futures=True)
        _pool, _pool_key = None, None


def shutdown_pool() -> None:
    """Останавливает процессы разбора (при остановке worker'а)."""
    _reset_pool()


async def parse_file_async(
    path: Path, name: str, settings: Settings
) -> tuple[FileType, ParsedFile]:
    if settings.parse_processes <= 0:
        return await asyncio.to_thread(parse_file, path, name, settings)
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_get_pool(settings), parse_file, path, name, settings)
    except BrokenProcessPool as exc:
        # Процесс разбора убит — обычно ядром из-за нехватки памяти (огромный скан).
        # Пул пересоздаём, документ получает понятную ошибку и кнопку «Повторить».
        _reset_pool()
        log.warning("процесс разбора файла «%s» аварийно завершился", name)
        raise ParseError(
            f"Разбор файла «{name}» прерван: процессу разбора не хватило памяти "
            "или он аварийно завершился"
        ) from exc
