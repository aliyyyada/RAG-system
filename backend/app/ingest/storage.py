"""Хранилище файлов документов на диске (том Docker `documents`).

Имя файла = SHA-256 содержимого: /data/documents/ab/abcdef0123…
Один и тот же файл, пришедший в двух документах, лежит на диске один раз,
а битый или подменённый файл сразу видно по несовпадению хэша.
Сначала файл пишется во временную папку tmp/, затем атомарно переносится на место.
"""

import hashlib
import os
import uuid
from pathlib import Path

from app.config import Settings


class FileStore:
    def __init__(self, settings: Settings):
        self.root = Path(settings.documents_dir)

    def tmp_path(self) -> Path:
        tmp = self.root / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        return tmp / uuid.uuid4().hex

    def path_for(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise ValueError("Неверный SHA-256")
        return self.root / sha256[:2] / sha256

    def commit(self, tmp: Path, sha256: str) -> Path:
        """Переносит временный файл на постоянное место (если такого ещё нет)."""
        dest = self.path_for(sha256)
        if dest.exists():
            tmp.unlink(missing_ok=True)
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, dest)
        return dest

    def save_bytes(self, data: bytes) -> tuple[str, Path]:
        sha = hashlib.sha256(data).hexdigest()
        tmp = self.tmp_path()
        tmp.write_bytes(data)
        return sha, self.commit(tmp, sha)
