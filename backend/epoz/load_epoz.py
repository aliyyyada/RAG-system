"""Загрузка комплекта ЕПоЗ (Положение и приложения) из папки в черновик редакции.

Запуск на сервере (папка с файлами сначала копируется в контейнер):

    docker compose cp "C:\\ЕПоЗ" app:/tmp/epoz
    docker compose exec app python -m epoz.load_epoz /tmp/epoz --group "Закупки" --activate

Без --activate редакция остаётся черновиком: её отчёт виден в «Администрирование → ЕПоЗ».
`--group` — группа, которой открывается весь комплект (создаётся, если такой нет).
`--force` — активировать, даже если в отчёте есть блокирующие замечания (осознанно).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.db import dispose_engine, get_sessionmaker
from app.models import Group
from epoz import loader


async def run(folder: Path, group: str | None, do_activate: bool, force: bool) -> int:
    settings = get_settings()
    sm = get_sessionmaker()
    files = await asyncio.to_thread(
        lambda: sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".docx")
    )
    if not files:
        print(f"В папке {folder} нет файлов .docx")
        return 1
    async with sm() as s, s.begin():
        draft = await loader.create_draft(s, "cli")
    print(f"Черновик редакции №{draft.id}")
    for f in files:
        key = loader.detect_part(f.name)
        if key == loader.IGNORED:
            print(f"  пропущен (не загружается): {f.name}")
            continue
        if key is None:
            print(f"  не определена часть: {f.name}")
            continue
        try:
            async with sm() as s, s.begin():
                d = await loader.get_draft(s)
                part = await loader.add_part_file(s, settings, d, key, f, f.name, "cli")
            rep = part.report or {}
            print(
                f"  {key:6s} {rep.get('status', '?'):8s} {f.name}  {'; '.join(rep.get('messages', []))}"
            )
        except loader.LoaderError as exc:
            print(f"  {key:6s} ОШИБКА   {f.name}: {exc.message}")
    async with sm() as s, s.begin():
        d = await loader.get_draft(s)
        report = await loader.edition_report(s, d)
    print("Блокирующие замечания:", report["blockers"] or "нет")
    print("Предупреждения:", report["warnings"] or "нет")
    if group:
        async with sm() as s, s.begin():
            g = await s.scalar(select(Group).where(Group.name == group))
            if g is None:
                g = Group(name=group, description="Доступ к ЕПоЗ")
                s.add(g)
                await s.flush()
            await loader.set_groups(s, [*await loader.get_groups(s), g.id], "cli")
            print(f"Группа доступа: «{group}» (id {g.id})")
    if do_activate:
        async with sm() as s, s.begin():
            d = await loader.get_draft(s)
            try:
                await loader.activate(s, d, "cli", force=force)
            except loader.LoaderError as exc:
                print("Активация не выполнена:", exc.message)
                return 2
        print("Редакция активирована")
    await dispose_engine()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("folder", type=Path)
    ap.add_argument("--group", help="группа, которой открыть весь комплект")
    ap.add_argument(
        "--activate", action="store_true", help="сделать черновик действующей редакцией"
    )
    ap.add_argument("--force", action="store_true", help="активировать несмотря на замечания")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    return asyncio.run(run(args.folder, args.group, args.activate, args.force))


if __name__ == "__main__":
    raise SystemExit(main())
