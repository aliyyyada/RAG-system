# Поиск по документам предприятия (RAG)

Корпоративная система поиска и ответов по документам завода. Работает полностью офлайн на одном сервере (Windows + Docker, GPU GTX 1070). Полное ТЗ — в [CLAUDE.md](CLAUDE.md).

**Статус:** этап 1 — вход (через внешний сервис проверки пароля, локальные и тестовые учётки), сессии, роли и права (RBAC), админка пользователей, групп и ролей. Готов каркас этапа 0: контейнеры, БД, миграции, очередь задач, потоковые ответы, CI, офлайн-сборка, согласованный дизайн.

## Состав

| Папка | Что внутри |
|-------|-----------|
| `backend/` | Python 3.12: FastAPI (`app/`), worker (`app/worker/`), миграции Alembic (`migrations/`), тесты (`tests/`), фейковый внешний сервис (`fakes/`) |
| `frontend/` | Vue 3 + TypeScript + Vite: интерфейс, e2e-тесты Playwright (`e2e/`) |
| `proxy/` | nginx: TLS, редирект 80 → 443, проксирование `/api`, статика фронтенда |
| `design/` | Дизайн-система и макеты экранов (открыть `design/index.html` в браузере) |
| `scripts/` | Сертификат для разработки, офлайн-сборка и установка, замер скорости моделей |
| `docs/` | Архитектура, установка на заводе, лицензии, открытые вопросы |

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

# Тестовые учётки (один раз после первого запуска):
docker compose exec app python -m app.cli seed-test-users
```

Проверка:
- https://localhost/ — интерфейс; «Войти» → тестовые учётки ниже;
- https://localhost/api/health/ready — состояние БД и Ollama;
- https://localhost/api/docs — документация API;
- `curl -k -N "https://localhost/api/diag/sse?seconds=20"` — поток с heartbeat: каждые 2 с приходит `: ping`, соединение не рвётся.

> Если `certs/server.crt` создан до этапа 1, удалите оба файла в `certs/` и запустите `dev-cert` ещё раз: в сертификат добавлено имя `fake-service`.

## Учётки для разработки

| Логин | Пароль | Кто | Как проверяется |
|-------|--------|-----|-----------------|
| `T-0001` | `test-password-1` | Обычный пользователь | Тестовая учётка (у нас, argon2) |
| `T-0002` | `test-password-1` | Аналитик (роль через группу) | Тестовая учётка |
| `T-0003` | `test-password-1` | Без доступа к источнику X (этап 3) | Тестовая учётка |
| `T-0004` | `test-password-1` | Администратор | Тестовая учётка |
| `10234`, `00017`, `20871`, `31455` | `demo-password-1` | Сотрудники «внешнего сервиса» | Фейковый сервис `fake-service` |

Сделать сотрудника `00017` администратором: `docker compose exec app python -m app.cli create-admin 00017`. В prod (`APP_ENV=prod`, `ENABLE_TEST_USERS=false`) тестовые учётки не входят, а фейковый сервис не запускается.

## Фронтенд отдельно (быстрая разработка)

```bash
cd frontend
npm install
npm run dev            # https://localhost:5173, /api проксируется на uvicorn :8000
```

Backend для этого запускается локально: `uvicorn app.main:app --port 8000` (из `backend/`, с `DATABASE_URL` к PostgreSQL из `docker-compose.dev.yml`).

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

Тесты с БД создают временную базу, накатывают на неё миграции и удаляют её после прогона.

E2E-тесты интерфейса (Playwright) — на запущенном стеке: `cd frontend && npx playwright install chromium && npx playwright test` (адрес задаётся `E2E_BASE_URL`, по умолчанию `https://localhost:4173` после `npm run build && npm run preview`). CI (`.github/workflows/ci.yml`) гоняет линтер, миграции вверх-вниз, все тесты и smoke-тест всего стека через nginx.

## Установка на заводе

Пошагово, без опыта Docker: [docs/install-offline.md](docs/install-offline.md).

## Документы

- [Архитектура и ключевые решения](docs/architecture.md)
- [Лицензии компонентов](docs/licenses.md)
- [Открытые вопросы к заказчику](docs/open-questions.md)
- [Дизайн-система и макеты](design/README.md)
