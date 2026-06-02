FROM python:3.11-slim

WORKDIR /app

COPY services/nostr-consumer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/nostr-consumer/app/ ./app/

COPY services/nostr-consumer/allowlist.json /app/allowlist.json

CMD ["python", "-m", "app.main"]
