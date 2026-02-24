FROM python:3.11-slim

WORKDIR /app

# Зависимости
RUN pip install --no-cache-dir flask gunicorn

# Копируем приложение
COPY app/ /app/

# Данные (плейлист, конфиги)
RUN mkdir -p /data

EXPOSE 8080

# Gunicorn с threads для streaming endpoints
CMD ["gunicorn", \
     "--bind", "0.0.0.0:8080", \
     "--workers", "2", \
     "--threads", "16", \
     "--timeout", "0", \
     "--keep-alive", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "server:app"]
