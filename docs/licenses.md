# Лицензии компонентов

Правило проекта: только лицензии, допускающие коммерческое использование (Apache-2.0, MIT, BSD и аналогичные permissive). Любой новый компонент сначала добавляется в эту таблицу.

## Модели

| Модель | Назначение | Лицензия | Статус |
|--------|-----------|----------|--------|
| Qwen2.5-7B-Instruct (`qwen2.5:7b-instruct-q4_K_M`) | Основная LLM | Apache-2.0 | ✅ |
| Qwen3-4B (`qwen3:4b`) | Запасная LLM | Apache-2.0 | ✅ |
| BAAI bge-m3 | Эмбеддинги | MIT | ✅ |
| BAAI bge-reranker-v2-m3 | Реранкер (этап 4) | Apache-2.0 | ✅ |
| ~~Qwen2.5-3B~~ | — | Qwen Research License | ❌ не используем |

## Готовые программы (образы Docker)

| Компонент | Лицензия | Статус |
|-----------|----------|--------|
| PostgreSQL 16 | PostgreSQL License (BSD-подобная) | ✅ |
| pgvector | PostgreSQL License | ✅ |
| nginx | BSD-2-Clause | ✅ |
| Ollama | MIT | ✅ |
| Python (образ `python:3.12-slim`) | PSF License (permissive) | ✅ |
| Docker Engine | Apache-2.0 | ✅ |
| Docker Desktop | Проприетарная, платная для крупных компаний | ⚠️ см. open-questions #2 |
| Tesseract OCR (этап 2) | Apache-2.0 | ✅ |
| Ory Hydra (этап 7а) | Apache-2.0 | ✅ |

## Backend (Python)

| Библиотека | Лицензия | Примечание |
|-----------|----------|-----------|
| FastAPI, Starlette | MIT / BSD-3 | |
| Uvicorn | BSD-3 | |
| SQLAlchemy, Alembic | MIT | |
| **asyncpg** | Apache-2.0 | Выбран вместо psycopg 3 — тот под LGPL-3.0, это не входит в наш список. |
| Pydantic, pydantic-settings | MIT | |
| httpx | BSD-3 | |
| argon2-cffi | MIT | Хэши паролей локальных и тестовых учёток |
| PyJWT | MIT | Access-токены сессии |
| pdfplumber, pdfminer.six | MIT | Текст и таблицы из PDF (этап 2). PyMuPDF не берём: AGPL-3.0. |
| pypdfium2 (PDFium) | Apache-2.0 / BSD-3 | Отрисовка страницы скана в картинку для OCR |
| Pillow | MIT-CMU (HPND, permissive) | Картинки для OCR |
| python-docx, lxml | MIT / BSD-3 | Разбор DOCX |
| cryptography, charset-normalizer | Apache-2.0 или BSD / MIT | Зависимости pdfminer.six (зашифрованные PDF, кодировки) |
| python-multipart | Apache-2.0 | Приём файла при ручной загрузке |
| tzdata | Apache-2.0 | Часовой пояс завода для ночной сверки |
| reportlab | BSD | Только генерация тестовых PDF (`fakes/make_fixtures.py`), в образ не входит |
| pytest, pytest-asyncio | MIT / Apache-2.0 | только разработка |
| Ruff | MIT | только разработка |

## Frontend

| Библиотека | Лицензия |
|-----------|----------|
| Vue 3, Vue Router, Pinia, Vite | MIT |
| TypeScript | Apache-2.0 |
| Tailwind CSS | MIT |
| Reka UI (основа shadcn-vue) | MIT |
| Playwright | Apache-2.0 (только тесты) |
| IBM Plex Sans / Mono (шрифт, пакеты @fontsource) | SIL OFL 1.1 — ⚠️ см. open-questions #4 |
| Иконки | свои, из макетов (`design/index.html`) |

На этапе 1 компоненты (кнопки, поля, таблицы, бейджи) взяты прямо из согласованных макетов, а окна и выпадающие меню — из Reka UI (на ней построен shadcn-vue): так интерфейс совпадает с макетом один в один. TanStack Table подключим на этапе 2, когда понадобятся таблицы документов с сортировкой.
