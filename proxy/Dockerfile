# Reverse proxy: TLS, статика фронтенда, /api → app.
# На этапе 0 вместо фронтенда — заглушка; Vue-сборка добавится сюда отдельной стадией
# (FROM node AS build ... COPY --from=build /app/dist /usr/share/nginx/html) после согласования дизайна.
FROM nginx:1.28-alpine

RUN rm /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/conf.d/rag.conf
COPY placeholder/ /usr/share/nginx/html/

EXPOSE 80 443
