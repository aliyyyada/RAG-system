# Установка/обновление системы на сервере завода БЕЗ ИНТЕРНЕТА.
# Запуск (PowerShell от администратора, из папки офлайн-пакета):
#   powershell -ExecutionPolicy Bypass -File .\scripts\offline\install.ps1
# Подробная инструкция: docs/install-offline.md
$ErrorActionPreference = "Stop"
$Bundle = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Bundle

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "ОШИБКА: $text" -ForegroundColor Red; exit 1 }

Write-Host "Папка пакета: $Bundle"
$version = (Get-Content VERSION).Trim()
$ollamaImage = Get-Content images.txt | Where-Object { $_ -like "ollama/ollama:*" } | Select-Object -First 1

Step "1/9 Проверка Docker"
docker version --format "{{.Server.Version}}" *> $null
if ($LASTEXITCODE -ne 0) { Fail "Docker не запущен. Запустите Docker и дождитесь, пока он будет готов." }

Step "2/9 Файл настроек .env"
if (-not (Test-Path .env)) {
  Copy-Item .env.example .env
  Write-Host "Создан .env из шаблона. Сейчас откроется Блокнот: задайте POSTGRES_PASSWORD," -ForegroundColor Yellow
  Write-Host "APP_ENV=prod, ENABLE_TEST_USERS=false, сохраните файл и запустите скрипт ещё раз." -ForegroundColor Yellow
  notepad .env
  exit 0
}
$envText = Get-Content .env -Raw
if ($envText -match "POSTGRES_PASSWORD=change-me") { Fail "В .env остался пароль по умолчанию. Задайте свой POSTGRES_PASSWORD." }
if ($envText -match "(?m)^APP_ENV=prod" -and $envText -match "(?m)^COMPOSE_PROFILES=dev") {
  Fail "В .env для завода (APP_ENV=prod) удалите строку COMPOSE_PROFILES=dev: фейковый сервис не нужен."
}
# Ключ подписи токенов: если пустой — создаём случайный (48 байт) и сохраняем в .env.
if ($envText -notmatch "(?m)^JWT_SECRET=\S{32,}") {
  $bytes = New-Object byte[] 48
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $secret = [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
  if ($envText -match "(?m)^JWT_SECRET=.*$") { $envText = $envText -replace "(?m)^JWT_SECRET=.*$", "JWT_SECRET=$secret" }
  else { $envText = $envText.TrimEnd() + "`r`nJWT_SECRET=$secret`r`n" }
  Write-Host "Создан JWT_SECRET (ключ подписи сессий)."
}
if ($envText -match "(?m)^APP_VERSION=.*$") { $envText = $envText -replace "(?m)^APP_VERSION=.*$", "APP_VERSION=$version" }
else { $envText = $envText.TrimEnd() + "`r`nAPP_VERSION=$version`r`n" }
Set-Content -Path .env -Value $envText -NoNewline -Encoding ascii

Step "3/9 Сертификат"
if (-not ((Test-Path certs\server.crt) -and (Test-Path certs\server.key))) {
  Fail "Нет certs\server.crt и certs\server.key. Положите сертификат от администраторов (см. certs\README.md)."
}

Step "4/9 Проверка целостности файлов пакета"
foreach ($line in Get-Content SHA256SUMS) {
  $hash, $file = $line -split "\s+", 2
  $file = $file.Trim().TrimStart("*")
  $actual = (Get-FileHash -Algorithm SHA256 $file).Hash.ToLower()
  if ($actual -ne $hash) { Fail "Файл $file повреждён при переносе (контрольная сумма не совпадает)." }
}
Write-Host "Файлы целы."

Step "5/9 Загрузка Docker-образов (несколько минут)"
docker load -i images.tar
if ($LASTEXITCODE -ne 0) { Fail "Не удалось загрузить образы." }

Step "6/9 Проверка видеокарты в Docker"
docker run --rm --gpus all --entrypoint nvidia-smi $ollamaImage
if ($LASTEXITCODE -ne 0) {
  Write-Host "Внимание: Docker не видит видеокарту. Модели будут работать на CPU очень медленно." -ForegroundColor Yellow
}

Step "7/9 Загрузка моделей Ollama"
docker volume create rag_ollama_models | Out-Null
docker run --rm -v rag_ollama_models:/root/.ollama -v "${Bundle}:/bundle:ro" --entrypoint tar $ollamaImage `
  -C /root/.ollama -xf /bundle/ollama-models.tar
if ($LASTEXITCODE -ne 0) { Fail "Не удалось распаковать модели." }

Step "8/9 Модель реранкера (уточняет порядок найденных фрагментов)"
docker volume create rag_reranker_model | Out-Null
docker run --rm -v rag_reranker_model:/models -v "${Bundle}:/bundle:ro" --entrypoint tar $ollamaImage `
  -C /models -xf /bundle/reranker-model.tar
if ($LASTEXITCODE -ne 0) { Fail "Не удалось распаковать модель реранкера." }

Step "9/9 Запуск системы"
docker compose up -d --no-build
if ($LASTEXITCODE -ne 0) { Fail "docker compose up завершился с ошибкой. Логи: docker compose logs" }

Write-Host "Ожидание готовности (до 5 минут)..."
for ($i = 0; $i -lt 60; $i++) {
  # curl.exe встроен в Windows 10/11 и Server 2019+; -k: сертификат может быть от внутреннего CA
  $body = & curl.exe -sk --max-time 3 https://localhost/api/health/ready
  if ($LASTEXITCODE -eq 0 -and $body -match '"status":"ready"') {
    Write-Host $body
    Write-Host "`nСистема запущена. Откройте в браузере https://<имя-сервера>/" -ForegroundColor Green
    Write-Host "Первый запуск? Назначьте администратора (см. docs/install-offline.md, часть 4):" -ForegroundColor Yellow
    Write-Host "  docker compose exec app python -m app.cli create-admin <табельный номер>" -ForegroundColor Yellow
    exit 0
  }
  Start-Sleep -Seconds 5
}
Fail "Система не ответила за 5 минут. Посмотрите логи: docker compose logs app migrate"
