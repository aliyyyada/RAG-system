# Проверка ЕПоЗ (`backend/epoz`)

Весь код функционала «Проверка ЕПоЗ» лежит здесь. В `backend/app/` остаются только
точки подключения (маршруты, задача worker'а, права, настройки, исключения из общих фильтров).

## Что внутри

| Файл | Назначение |
|------|-----------|
| `docx_parse.py` | разбор Word ЕПоЗ с восстановлением нумерации пунктов («10.3.4») |
| `loader.py` | редакции ЕПоЗ: черновик, загрузка файлов, отчёт, активация, группа доступа |
| `api.py` | API администрирования ЕПоЗ (`/api/admin/epoz`), право `epoz.manage` |
| `models.py` | таблицы `epoz_editions`, `epoz_parts`, `epoz_clauses` |
| `load_epoz.py` | загрузка комплекта из папки (скрипт) |
| `inspect_numbering.py` | диагностика нумерации файла без Docker и без записи в базу |

## Быстрые команды

Проверить, как разбирается файл (из папки `backend`, без Docker):

```
python -m epoz.inspect_numbering "ЕПоЗ_сент 2026.docx" --label 10.3 --label 11.3
```

Загрузить комплект на сервере:

```
docker compose cp "C:\ЕПоЗ" app:/tmp/epoz
docker compose exec app python -m epoz.load_epoz /tmp/epoz --group "Закупки" --activate
```

## Откат

Миграции этого функционала имеют `downgrade`; порядок возврата к версии без него:

```
docker compose exec app alembic downgrade 0008
git switch main
docker compose up -d --build
```
