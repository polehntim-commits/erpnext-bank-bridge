FROM python:3.14-slim

WORKDIR /app

# psycopg2-binary ships wheels, so no build-essential needed; curl is handy for
# the Umbrel healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY config.py ./

ENV FLASK_APP=app:create_app \
    DATA_DIR=/data \
    PYTHONUNBUFFERED=1

EXPOSE 5202

# Two gunicorn workers so the background sync scheduler thread (flock-elected in
# one worker) and web requests don't fight. 300s timeout covers a large
# first-time transactions backfill.
CMD ["gunicorn", "--workers", "2", "--threads", "4", \
     "--worker-class", "gthread", \
     "--timeout", "300", \
     "--bind", "0.0.0.0:5202", \
     "app:create_app()"]
