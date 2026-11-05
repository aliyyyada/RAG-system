# Сборка офлайн-пакета на Mac и перенос на сервер завода (Windows) на флешке

Пакет собирается на Mac с интернетом: в него попадают все Docker-образы (наш код со всеми
зависимостями Python и JavaScript, nginx, PostgreSQL + pgvector, Ollama), модели Ollama
(`qwen2.5:7b-instruct-q4_K_M`, `qwen3:4b`, `bge-m3`) и реранкер `bge-reranker-v2-m3`.
На заводе интернет не нужен совсем.

**Главная ловушка.** Mac с процессором Apple (M1–M4) — это архитектура arm64, а сервер завода
(Intel i3, Windows + WSL2) — amd64. Образы, собранные «как обычно», на заводе не запустятся
с ошибкой `exec format error`. Скрипт `build-bundle.sh` поэтому всегда собирает и скачивает
образы под `linux/amd64` и перед сохранением проверяет каждый образ. `install.ps1` на заводе
проверяет ещё раз.

Что получится: папка `offline-bundle/` размером около 15–18 ГБ.

---

## Часть 1. Подготовка Mac (один раз)

1. **Узнать процессор:**  → «Об этом Mac». «Chip Apple M…» — Apple Silicon, «Intel» — Intel.
   На Intel-Mac шаг 3 не нужен, остальное так же.
2. **Docker Desktop для Mac** (версия под ваш процессор) установлен и запущен.
3. **Эмуляция amd64 (только Apple Silicon).** Docker Desktop → Settings → General →
   включить **Use Rosetta for x86_64/amd64 emulation on Apple Silicon** → Apply & restart.
   Без Rosetta сборка тоже пройдёт (через эмулятор QEMU), но в несколько раз медленнее.
4. **Ресурсы Docker:** Settings → Resources:
   - Memory — не меньше 8 ГБ;
   - Disk usage limit — не меньше 100 ГБ (образы + кэш сборки + копия моделей).

   На самом диске Mac должно быть свободно ещё ≈ 20 ГБ под папку `offline-bundle`.
5. **Git:** в Терминале `git --version`. Если macOS предложит установить Command Line Tools,
   согласиться.

## Часть 2. Код: какую ветку собирать

Самая свежая версия — ветка **`dev`**. В ней источник ОРД из MS SQL и вход без сертификата.
Ещё два PR ждут слияния в `dev`, и их нужно влить до сборки:

- **#12**: разбор файлов в отдельном процессе, две полосы worker'а, памятка по GPU и памяти;
- PR с этой инструкцией: сборка под amd64 на Mac.

На GitHub откройте каждый PR → **Merge pull request**. Затем в Терминале:

```bash
cd ~
git clone https://github.com/aliyyyada/RAG-system.git   # если папки ещё нет
cd RAG-system
git checkout dev
git pull
git log --oneline -3        # сверху должны быть свежие коммиты из влитых PR
```

## Часть 3. Сертификат (если нужен HTTPS)

Варианты:

- **Сертификат от админов завода есть:** положите его файлы рядом, на шаге 6.3 скопируете
  их в пакет как `certs/server.crt` и `certs/server.key`.
- **Сертификата пока нет, но нужен HTTPS:** создайте самоподписанный на Mac (подставьте имя
  сервера завода, по которому его будут открывать в браузере):
  ```bash
  ./scripts/dev-cert.sh имя-сервера-завода
  ```
  Файлы появятся в `certs/`. Браузер покажет предупреждение — для теста это нормально.
- **Тестовый стенд без HTTPS:** ничего не делайте, на заводе впишете `PLAIN_HTTP=true` в `.env`.

## Часть 4. Сборка пакета

```bash
cd ~/RAG-system
./scripts/offline/build-bundle.sh
```

Займёт от 30 минут до пары часов: на Apple Silicon образы собираются через эмуляцию, и модели
скачиваются (≈ 9 ГБ). Mac не должен засыпать: держите крышку открытой и подключите зарядку,
или запускайте так: `caffeinate -i ./scripts/offline/build-bundle.sh`.

Что смотреть в выводе:

| Строка | Что значит |
|---|---|
| `платформа образов: linux/amd64 (эта машина: linux/arm64)` | собираем под завод, Mac — Apple Silicon |
| `==> Проверка архитектуры образов` и у каждого образа `x86_64` | образы подходят для завода |
| `- qwen2.5:7b-instruct-q4_K_M` … `success` | модель скачана |
| `Проверка реранкера (логиты): [число, число]` | реранкер собран и работает |
| `==> Готово: …/offline-bundle (16G)` | пакет готов |

Если скрипт остановился с ошибкой, его можно просто запустить ещё раз: уже скачанные
образы и слои берутся из кэша.

## Часть 5. Проверить пакет

```bash
ls -lh offline-bundle
cat offline-bundle/VERSION
cd offline-bundle && shasum -a 256 -c SHA256SUMS && cd ..
```

Должно быть: `images.tar`, `ollama-models.tar`, `reranker-model.tar`, `images.txt`,
`SHA256SUMS`, `VERSION`, `docker-compose.yml`, `.env.example`, папки `scripts`, `docs`, `certs`.
Проверка контрольных сумм печатает `OK` у трёх файлов.

## Часть 6. Флешка

### 6.1. Файловая система — только exFAT

`images.tar` и `ollama-models.tar` больше 4 ГБ:

- **FAT32** — не подходит, файлы больше 4 ГБ на неё не записываются;
- **NTFS** — Mac на неё писать не умеет;
- **exFAT** — понимают и Mac, и Windows. Её и берём.

Нужна флешка от **32 ГБ** (лучше USB 3.0: 16 ГБ на медленную флешку пишутся больше часа).

Форматирование (всё с флешки сотрётся):
«Дисковая утилита» → меню «Вид» → «Показать все устройства» → слева выбрать **саму флешку**
(верхнюю строку, не раздел) → «Стереть» → Имя: `RAG`, Формат: **ExFAT**,
Схема: **Основная загрузочная запись (MBR)** → «Стереть».

### 6.2. Копирование

```bash
rsync -a --progress ~/RAG-system/offline-bundle/ /Volumes/RAG/offline-bundle/
```

### 6.3. Сертификат в пакет (если делали часть 3)

```bash
cp ~/RAG-system/certs/server.crt ~/RAG-system/certs/server.key /Volumes/RAG/offline-bundle/certs/
```

### 6.4. Проверить копию и извлечь флешку

```bash
cd /Volumes/RAG/offline-bundle && shasum -a 256 -c SHA256SUMS && cd ~
dot_clean -m /Volumes/RAG      # убирает служебные файлы macOS «._*»
diskutil eject /Volumes/RAG
```

Флешку вынимать только после сообщения `Disk /Volumes/RAG ejected`.

## Часть 7. На сервере завода (Windows)

### 7.1. Один раз: подготовка сервера

Это описано в [install-offline.md](install-offline.md), часть 2: `nvidia-smi` показывает
GTX 1070, Docker Desktop запущен на WSL2. Память для WSL2 настраивается файлом
`C:\Users\<пользователь>\.wslconfig` (подробно — [performance.md](performance.md)):

```ini
[wsl2]
memory=24GB

[experimental]
autoMemoryReclaim=gradual
```

После правки: PowerShell → `wsl --shutdown`, затем снова запустить Docker Desktop.

### 7.2. Скопировать пакет с флешки на диск

Не запускайте установку с флешки, это медленно. Скопируйте папку `offline-bundle` с флешки
в новую папку, например `D:\rag-<версия из файла VERSION>`.

### 7.3. Если на сервере уже стояла тестовая версия

1. В PowerShell в **старой** папке остановить систему (данные останутся в томах Docker):
   ```powershell
   cd D:\старая-папка
   docker compose down
   ```
   Без `-v`! С `-v` удалятся база и загруженные документы.
2. Перенести из старой папки в новую файл `.env` и папку `certs\` (если там был сертификат).
3. **Не переносить** `docker-compose.override.yml` (он выключал модели в тесте «без моделей»)
   и `docker-compose.http.yml`. Режим без сертификата теперь встроен: `PLAIN_HTTP=true`.
4. В перенесённом `.env` удалить строку `COMPOSE_FILE=…`, если она есть.
5. `POSTGRES_PASSWORD` оставить **тем же**: база уже создана с этим паролем.

### 7.4. Установка

PowerShell **от имени администратора**:

```powershell
cd D:\rag-<версия>
powershell -ExecutionPolicy Bypass -File .\scripts\offline\install.ps1
```

Если `.env` ещё нет, откроется Блокнот. Заполнить и сохранить:

| Строка | Значение |
|---|---|
| `POSTGRES_PASSWORD` | длинный пароль, латиница и цифры |
| `APP_ENV` | `prod` для работы; `dev` для теста |
| `ENABLE_TEST_USERS` | `false` (при `APP_ENV=prod` обязательно) |
| `COMPOSE_PROFILES=dev` | удалить строку (фейковый сервис на заводе не нужен) |
| `PLAIN_HTTP` | `true`, если сертификата нет |
| `EXTERNAL_AUTH_URL`, `EXTERNAL_AUTH_CA_FILE` | адрес функции проверки пароля и её CA; для теста без неё — `EXTERNAL_AUTH_CA_FILE=` пусто и вход аварийной учёткой |
| `SOURCE_MSSQL_USER`, `SOURCE_MSSQL_PASSWORD` | логин и пароль MS SQL на чтение |

Затем запустить скрипт ещё раз. Он проверит контрольные суммы, загрузит образы, проверит,
что они под amd64, проверит видеокарту, зальёт модели и реранкер в тома и запустит систему.
В конце будет зелёная надпись «Система запущена».

### 7.5. После первого запуска

1. Администратор и аварийная учётка: [install-offline.md](install-offline.md), часть 4.
2. Источник ОРД: [install-offline.md](install-offline.md), часть 5.
3. Модели на видеокарте:
   ```powershell
   docker compose exec ollama ollama ps
   ```
   В колонке PROCESSOR должно быть `100% GPU`. Если там `CPU/GPU`, впишите в `.env`
   `LLM_NUM_CTX=4096` и выполните `docker compose up -d`.

## Если что-то пошло не так

| Симптом | Где | Что сделать |
|---|---|---|
| `ОШИБКА: образ … собран не под linux/amd64` | Mac | `docker image rm <образ>` и снова `build-bundle.sh`; проверить, что не задана переменная `BUNDLE_PLATFORM` |
| Сборка очень медленная, `npm ci` или `pip install` идут десятки минут | Mac | включить Rosetta (часть 1, шаг 3) |
| `no space left on device` | Mac | Docker Desktop → Settings → Resources → увеличить диск; `docker builder prune` |
| Ошибка на реранкере (`pip install torch`) | Mac | запустить скрипт ещё раз: часто это обрыв скачивания |
| `File too large` при копировании на флешку | Mac | флешка в FAT32, переформатировать в exFAT (6.1) |
| «Файл … повреждён при переносе» | Windows | скопировать этот файл с флешки ещё раз; если не помогло, заново записать на флешку |
| «Образ … собран под 'arm64'» | Windows | пакет собран старым скриптом без amd64, пересобрать на Mac с этой версией скрипта |
| `exec format error` в `docker compose logs` | Windows | то же самое |
| «Docker не видит видеокарту» | Windows | [install-offline.md](install-offline.md), таблица «Если что-то пошло не так» |
