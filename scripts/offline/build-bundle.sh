#!/usr/bin/env bash
# Сборка офлайн-пакета на машине С ИНТЕРНЕТОМ (Linux, macOS или Windows + Git Bash),
# где установлен Docker. Результат — папка offline-bundle/, которую переносят на завод
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

echo "==> Версия пакета: $VERSION"
rm -rf "$OUT"; mkdir -p "$OUT"

echo "==> Сборка образов app/worker/proxy"
docker compose build --pull

echo "==> Загрузка сторонних образов"
docker compose pull postgres ollama

IMAGES=$(docker compose config --images | sort -u)
echo "==> Сохранение образов:"; echo "$IMAGES"
# shellcheck disable=SC2086
docker save -o "$OUT/images.tar" $IMAGES
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
./scripts/offline/export-reranker.sh "$OUT/reranker"
tar -C "$OUT/reranker" -cf "$OUT/reranker-model.tar" .
rm -rf "$OUT/reranker"

echo "==> Копирование файлов установки"
cp docker-compose.yml docker-compose.cpu.yml .env.example "$OUT/"
cp -r scripts docs "$OUT/"
mkdir -p "$OUT/certs" && cp certs/README.md "$OUT/certs/"
cp README.md "$OUT/"
echo "$VERSION" > "$OUT/VERSION"
(cd "$OUT" && sha256sum images.tar ollama-models.tar reranker-model.tar > SHA256SUMS)

echo "==> Готово: $OUT ($(du -sh "$OUT" | cut -f1))"
echo "    Перенесите папку на сервер завода и запустите scripts\\offline\\install.ps1"
