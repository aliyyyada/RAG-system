#!/usr/bin/env bash
# Самоподписанный сертификат для разработки (пока нет сертификата от внутреннего CA).
# Использование: ./scripts/dev-cert.sh [имя-хоста]
set -euo pipefail
HOST="${1:-localhost}"
DIR="$(cd "$(dirname "$0")/.." && pwd)/certs"
mkdir -p "$DIR"
if [[ -f "$DIR/server.crt" ]]; then
  echo "Сертификат уже есть: $DIR/server.crt (удалите его, чтобы создать заново)"; exit 0
fi
openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
  -keyout "$DIR/server.key" -out "$DIR/server.crt" \
  -subj "/CN=$HOST" \
  -addext "subjectAltName=DNS:$HOST,DNS:localhost,IP:127.0.0.1"
chmod 600 "$DIR/server.key"
echo "Готово: $DIR/server.crt и $DIR/server.key"
