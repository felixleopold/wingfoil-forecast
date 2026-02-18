FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- dev ---
FROM base AS development
RUN pip install --no-cache-dir flask[dev]
COPY app/ ./app/
RUN mkdir -p /app/data /app/static /app/static/overlays \
 && if [ -f "/app/app/static/overlays/compassrose.svg" ]; then cp -f "/app/app/static/overlays/compassrose.svg" "/app/static/overlays/compassrose.svg"; fi
ENV FLASK_APP=app/main.py FLASK_ENV=development PYTHONPATH=/app
EXPOSE 5001
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:5001/health || exit 1
CMD ["python", "-m", "flask", "run", "--host=0.0.0.0", "--port=5001", "--reload"]

# --- prod ---
FROM base AS production
RUN useradd -r -u 10001 appuser
COPY app/ ./app/
RUN mkdir -p /app/data /app/static /app/static/overlays \
 && if [ -f "/app/app/static/overlays/compassrose.svg" ]; then cp -f "/app/app/static/overlays/compassrose.svg" "/app/static/overlays/compassrose.svg"; fi \
 && chown -R appuser:appuser /app
ENV FLASK_APP=app/main.py FLASK_ENV=production PYTHONPATH=/app
EXPOSE 5001
USER appuser
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:5001/health || exit 1
CMD ["gunicorn", "-b", "0.0.0.0:5001", "app.main:app", "--workers=2", "--threads=4", "--timeout=30"]
