"""Загрузка справочника ОКПД2 из Excel (команда для администратора).

    docker compose cp "C:/okpd.xlsx" app:/tmp/okpd.xlsx
    docker compose exec app python -m epoz.import_okpd2 /tmp/okpd.xlsx

То же самое делает кнопка в «Администрирование → Справочник ОКПД2». Загрузка идёт одной
транзакцией; если такая редакция уже загружена, ничего не дублируется.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.db import dispose_engine, get_sessionmaker
from epoz import okpd2


async def run(path: Path) -> int:
    try:
        async with get_sessionmaker()() as s, s.begin():
            report = await okpd2.import_okpd2(s, path, "cli")
    except okpd2.Okpd2Error as exc:
        print("Ошибка:", exc.message)
        return 1
    finally:
        await dispose_engine()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("file", type=Path)
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    return asyncio.run(run(args.file))


if __name__ == "__main__":
    raise SystemExit(main())
