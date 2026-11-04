# Reverse proxy: TLS, статика фронтенда, /api → app.
# Сборка из корня репозитория (см. docker-compose.yml): npm-зависимости ставятся на машине
# с интернетом при сборке образа, на заводе образ загружается из офлайн-пакета готовым.

# --- 1. Сборка фронтенда (Vue + Vite) в статические файлы
FROM node:22-alpine AS build
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY design/tokens.css /src/design/tokens.css
COPY frontend/ ./
RUN npm run build

# --- 2. nginx со статикой
FROM nginx:1.28-alpine

RUN rm /etc/nginx/conf.d/default.conf
# Обе настройки в образе, нужную при старте выбирает choose-config.sh по PLAIN_HTTP из .env.
COPY proxy/nginx.conf /etc/nginx/rag-https.conf
COPY proxy/nginx-http.conf /etc/nginx/rag-http.conf
COPY --chmod=755 proxy/choose-config.sh /docker-entrypoint.d/05-rag-choose-config.sh
COPY --from=build /src/frontend/dist/ /usr/share/nginx/html/

EXPOSE 80 443
