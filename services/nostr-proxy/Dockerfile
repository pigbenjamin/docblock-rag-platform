FROM python:3.11-slim

WORKDIR /app

COPY services/nostr-proxy/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/nostr-proxy/app/ ./app/

EXPOSE 8800

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8800"]
