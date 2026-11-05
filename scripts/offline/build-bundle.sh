#!/usr/bin/env bash
# Сборка офлайн-пакета на машине С ИНТЕРНЕТОМ (Linux, macOS или Windows + Git Bash),
# где установлен Docker. На Mac с процессором Apple (M1–M4) образы всё равно собираются
# под linux/amd64 — архитектуру сервера завода (Intel i3, Windows + WSL2); подробности —
# docs/build-on-mac.md. Результат — папка offline-bundle/, которую переносят на завод
# (флешка, общий диск) и устанавливают скриптом install.ps1.
#
# Что внутри пакета:
#   images.tar          — все Docker-образы (наш код со всеми pip/npm-зависимостями,
#                         nginx, PostgreSQL+pgvector, Ollama)
#   ollama-models.tar   — модели Ollama из scripts/offline/models.txt
#   docker-compose*.yml, .env.example, scripts/, docs/, certs/README.md
#   SHA256SUMS          — контрольные суммы для проверки после переноса
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT="$(pwd)"
OUT="$ROOT/offline-bundle"
VERSION="${APP_VERSION:-$(git describe --tags --always 2>/dev/null || date +%Y%m%d)}"
export APP_VERSION="$VERSION"

# Нужен .env только ради подстановки переменных в compose при сборке.
[[ -f .env ]] || cp .env.example .env
set -a; source .env; set +a
export APP_VERSION="$VERSION"

docker info >/dev/null 2>&1 || { echo "ОШИБКА: Docker не запущен. Запустите Docker Desktop и повторите." >&2; exit 1; }

# Платформа сервера завода. На Mac с Apple Silicon без этого Docker собрал бы и скачал
# образы arm64, и на заводе контейнеры не запустились бы («exec format error»).
PLATFORM="${BUNDLE_PLATFORM:-linux/amd64}"
export DOCKER_DEFAULT_PLATFORM="$PLATFORM"
# Платформа этой машины: на ней быстрее (без эмуляции) готовится реранкер — файл ONNX
# от платформы не зависит.
HOST_PLATFORM="linux/$(docker version --format '{{.Server.Arch}}')"

# sha256sum есть в Linux и Git Bash; в macOS — shasum -a 256 (формат вывода тот же).
if command -v sha256sum >/dev/null 2>&1; then SHA256=(sha256sum); else SHA256=(shasum -a 256); fi

echo "==> Версия пакета: $VERSION, платформа образов: $PLATFORM (эта машина: $HOST_PLATFORM)"
rm -rf "$OUT"; mkdir -p "$OUT"

echo "==> Сборка образов app/worker/proxy"
docker compose build --pull

echo "==> Загрузка сторонних образов"
docker compose pull postgres ollama

IMAGES=$(docker compose config --images | sort -u)
echo "==> Проверка архитектуры образов (должно быть x86_64)"
for img in $IMAGES; do
  arch=$(docker run --rm --platform "$PLATFORM" --entrypoint uname "$img" -m)
  echo "    $img: $arch"
  [[ "$PLATFORM" != "linux/amd64" || "$arch" == "x86_64" ]] || {
    echo "ОШИБКА: образ $img собран не под $PLATFORM. Удалите его (docker image rm $img) и запустите скрипт снова." >&2
    exit 1
  }
done
echo "==> Сохранение образов:"; echo "$IMAGES"
SAVE_ARGS=()
# Docker 27+: сохраняем только нужную платформу (с хранилищем containerd в образе их бывает несколько).
if docker save --help 2>/dev/null | grep -q -- '--platform'; then SAVE_ARGS=(--platform "$PLATFORM"); fi
# shellcheck disable=SC2086
docker save "${SAVE_ARGS[@]+"${SAVE_ARGS[@]}"}" -o "$OUT/images.tar" $IMAGES
echo "$IMAGES" > "$OUT/images.txt"

echo "==> Скачивание моделей Ollama"
MODELS_VOL="rag_bundle_models_$$"
docker volume create "$MODELS_VOL" >/dev/null
docker run -d --name "rag_bundle_ollama_$$" -v "$MODELS_VOL:/root/.ollama" "ollama/ollama:${OLLAMA_VERSION}" >/dev/null
trap 'docker rm -f "rag_bundle_ollama_$$" >/dev/null 2>&1 || true; docker volume rm "$MODELS_VOL" >/dev/null 2>&1 || true' EXIT
sleep 3
grep -vE '^\s*(#|$)' scripts/offline/models.txt | while read -r model; do
  echo "    - $model"
  docker exec "rag_bundle_ollama_$$" ollama pull "$model"
done
docker run --rm -v "$MODELS_VOL:/models:ro" -v "$OUT:/out" alpine:3.22 \
  tar -C /models -cf /out/ollama-models.tar .

echo "==> Реранкер bge-reranker-v2-m3 (ONNX, int8) — работает на CPU в контейнере app"
RERANKER_PLATFORM="$HOST_PLATFORM" ./scripts/offline/export-reranker.sh "$OUT/reranker"
tar -C "$OUT/reranker" -cf "$OUT/reranker-model.tar" .
rm -rf "$OUT/reranker"

echo "==> Копирование файлов установки"
cp docker-compose.yml docker-compose.cpu.yml .env.example "$OUT/"
cp -r scripts docs "$OUT/"
mkdir -p "$OUT/certs" && cp certs/README.md "$OUT/certs/"
cp README.md "$OUT/"
echo "$VERSION" > "$OUT/VERSION"
(cd "$OUT" && "${SHA256[@]}" images.tar ollama-models.tar reranker-model.tar > SHA256SUMS)

echo "==> Готово: $OUT ($(du -sh "$OUT" | cut -f1))"
echo "    Перенесите папку на сервер завода и запустите scripts\\offline\\install.ps1"
