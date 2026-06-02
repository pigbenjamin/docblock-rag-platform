FROM python:3.11-slim

WORKDIR /app

# marker (PDF→Markdown) requires system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Install docblock-core shared library
COPY libs/docblock-core /libs/docblock-core
RUN pip install --no-cache-dir /libs/docblock-core

# Install service dependencies
COPY services/marker-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy service source
COPY services/marker-service/app/ ./app/

# Shared volume for PDF/MD work files (same as ingest-worker)
VOLUME ["/data"]

EXPOSE 8766

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8766"]
