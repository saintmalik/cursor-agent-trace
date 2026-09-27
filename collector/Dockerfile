# Org TRACE collector — AWS creds + COLLECTOR_TOKEN stay on the server.
FROM python:3.12-slim

WORKDIR /app

# Optional S3 client; collector runs fine without it (local disk fallback).
RUN pip install --no-cache-dir boto3

COPY server.py /app/server.py
RUN mkdir -p /app/data

ENV HOST=0.0.0.0 \
    PORT=8787 \
    LOCAL_DIR=/app/data \
    PREFIX=cursor-agent-trace

EXPOSE 8787

# COLLECTOR_TOKEN required at runtime. Optionally set S3_BUCKET + AWS_*.
CMD ["python3", "/app/server.py"]
