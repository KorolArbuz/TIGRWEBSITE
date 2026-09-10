FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=production

WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
    && groupadd --gid 10001 shop \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin shop

COPY app.py ./
COPY shop ./shop
COPY static ./static
COPY templates ./templates
RUN mkdir -p /app/data/media /app/data/imports \
    && chown 10001:10001 /app/data /app/data/media /app/data/imports
USER 10001:10001

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--no-proxy-headers", "--no-access-log", "--limit-concurrency", "64", "--backlog", "64", "--timeout-keep-alive", "5"]
