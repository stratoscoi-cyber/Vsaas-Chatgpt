# Build context is the repository root (see docker-compose.yml).
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agri_platform ./agri_platform

EXPOSE 5002
ENV PORT=5002
CMD ["gunicorn", "--bind", "0.0.0.0:5002", "--workers", "2", \
     "agri_platform.traas.wsgi:app"]
