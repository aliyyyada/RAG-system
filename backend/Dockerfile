# Образ для app, worker и migrate (одинаковый код, разные команды запуска).
# Все pip-зависимости ставятся при сборке на машине с интернетом,
# на заводе образ загружается из офлайн-пакета (docker load) и в сеть не ходит.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/backend

# Tesseract OCR (Apache-2.0) с русским и английским языками — распознавание сканов.
# Ставится при сборке образа; на заводе образ приходит готовым в офлайн-пакете.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini pyproject.toml ./
COPY migrations ./migrations
COPY app ./app
# Фейковый внешний сервис (только разработка; в prod контейнер не запускается)
COPY fakes ./fakes

RUN useradd --system --uid 10001 --home-dir /srv appuser \
    && mkdir -p /data/documents && chown appuser /data/documents
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
