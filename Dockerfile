FROM python:3.11-slim

WORKDIR /app

# Системные зависимости (ffmpeg для отладки, curl для healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

# Python зависимости
RUN pip install --no-cache-dir flask gunicorn

# Копируем приложение
COPY app/ /app/

# Данные (плейлист, конфиги)
RUN mkdir -p /data

EXPOSE 8080

# Gunicorn: 1 worker (in-memory state), много threads для streaming
CMD ["gunicorn", \
     "--bind", "0.0.0.0:8080", \
     "--workers", "1", \
     "--threads", "32", \
     "--timeout", "0", \
     "--keep-alive", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "server:app"]
