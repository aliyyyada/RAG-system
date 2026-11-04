#!/bin/sh
# Запускается образом nginx перед стартом (папка /docker-entrypoint.d/).
# PLAIN_HTTP=true в .env → сайт по обычному http без сертификата (proxy/nginx-http.conf);
# иначе — HTTPS с сертификатом из certs/ (proxy/nginx.conf).
set -e
case "$(echo "${PLAIN_HTTP:-false}" | tr '[:upper:]' '[:lower:]')" in
  true|1|yes)
    cp /etc/nginx/rag-http.conf /etc/nginx/conf.d/rag.conf
    echo "rag: PLAIN_HTTP=true — сайт по http без сертификата" ;;
  *)
    cp /etc/nginx/rag-https.conf /etc/nginx/conf.d/rag.conf ;;
esac
