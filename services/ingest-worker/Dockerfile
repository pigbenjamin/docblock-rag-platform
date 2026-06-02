FROM python:3.11-slim

WORKDIR /app

# Install docblock-core shared library
COPY libs/docblock-core /libs/docblock-core
RUN pip install --no-cache-dir /libs/docblock-core

# Install service dependencies
COPY services/ingest-worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy service source
COPY services/ingest-worker/worker/ ./worker/

# Shared volume for PDF/MD/JSON work files (mount at runtime)
VOLUME ["/data"]

EXPOSE 8762

CMD ["uvicorn", "worker.main:app", "--host", "0.0.0.0", "--port", "8762"]
