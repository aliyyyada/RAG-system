# Поиск по документам предприятия (RAG)

Корпоративная система поиска и ответов по документам завода. Работает полностью офлайн на одном сервере (Windows + Docker, GPU GTX 1070). Полное ТЗ — в [CLAUDE.md](CLAUDE.md).

**Статус:** этап 0 — каркас (контейнеры, БД, миграции, очередь задач, потоковые ответы, CI, офлайн-сборка) и дизайн экранов на согласование.

## Состав

| Папка | Что внутри |
|-------|-----------|
| `backend/` | Python 3.12: FastAPI (`app/`), worker (`app/worker/`), миграции Alembic (`migrations/`), тесты (`tests/`) |
| `proxy/` | nginx: TLS, редирект 80 → 443, проксирование `/api`, статика фронтенда |
| `design/` | Дизайн-система и макеты экранов (открыть `design/index.html` в браузере) |
| `scripts/` | Сертификат для разработки, офлайн-сборка и установка, замер скорости моделей |
| `docs/` | Архитектура, установка на заводе, лицензии, открытые вопросы |
| `frontend/` | Появится на этапе 1, после согласования дизайна |

## Быстрый старт (разработка, машина с интернетом)

```bash
cp .env.example .env              # задайте POSTGRES_PASSWORD
./scripts/dev-cert.sh             # самоподписанный сертификат в certs/ (Windows: .\scripts\dev-cert.ps1)

# С видеокартой NVIDIA:
docker compose up -d --build
# Без видеокарты (Ollama на CPU):
docker compose -f docker-compose.yml -f docker-compose.cpu.yml up -d --build

# Модели (один раз):
docker compose exec ollama ollama pull qwen2.5:7b-instruct-q4_K_M
docker compose exec ollama ollama pull bge-m3
```

Проверка:
- https://localhost/ — заглушка со ссылками;
- https://localhost/api/health/ready — состояние БД и Ollama;
- https://localhost/api/docs — документация API;
- `curl -k -N "https://localhost/api/diag/sse?seconds=20"` — поток с heartbeat: каждые 2 с приходит `: ping`, соединение не рвётся.

## Тесты

```bash
cd backend
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
ruff check . && ruff format --check .

pytest                       # только юнит-тесты
# + тесты с БД (нужен PostgreSQL с pgvector, например из docker-compose.dev.yml):
TEST_DATABASE_URL=postgresql+asyncpg://rag:<пароль>@localhost:5432/postgres pytest
```

Тесты с БД создают временную базу, накатывают на неё миграции и удаляют её после прогона. CI (`.github/workflows/ci.yml`) гоняет линтер, миграции вверх-вниз, все тесты и smoke-тест всего стека через nginx.

## Установка на заводе

Пошагово, без опыта Docker: [docs/install-offline.md](docs/install-offline.md).

## Документы

- [Архитектура и ключевые решения](docs/architecture.md)
- [Лицензии компонентов](docs/licenses.md)
- [Открытые вопросы к заказчику](docs/open-questions.md)
- [Дизайн-система и макеты](design/README.md)
