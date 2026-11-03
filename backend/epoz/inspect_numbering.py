"""Диагностика разбора файлов ЕПоЗ: как восстановлена нумерация и можно ли доверять ссылкам.

Запуск (без Docker, из папки backend):

    python -m epoz.inspect_numbering "C:\\путь\\ЕПоЗ_сент 2026.docx" [--label 10.3 --label 11.3]

Ничего не сохраняет и не отправляет: только печатает отчёт и несколько пунктов для просмотра.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from epoz.docx_parse import parse_epoz_docx


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("file", type=Path)
    ap.add_argument("--label", action="append", default=[], help="показать пункт с этим номером")
    ap.add_argument("--head", type=int, default=0, help="показать первые N пунктов с номерами")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    parsed = parse_epoz_docx(args.file)
    print(json.dumps(parsed.report, ensure_ascii=False, indent=2))
    wanted = set(args.label)
    for c in parsed.clauses:
        if c.label in wanted:
            print(f"[{c.label}] (уровень {c.level}) {c.text[:200]}")
    for c in [c for c in parsed.clauses if c.label][: args.head]:
        print(f"[{c.label}] {c.text[:110]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
