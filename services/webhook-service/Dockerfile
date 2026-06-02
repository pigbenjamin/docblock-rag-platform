FROM python:3.11-slim

WORKDIR /app

# Install docblock-core shared library
COPY libs/docblock-core /libs/docblock-core
RUN pip install --no-cache-dir /libs/docblock-core

# Install service dependencies
COPY services/webhook-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy service source
COPY services/webhook-service/app/ ./app/

EXPOSE 8763

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8763"]
