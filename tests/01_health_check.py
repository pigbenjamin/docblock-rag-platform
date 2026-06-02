"""
01 Health Check
所有服務的 /healthz 與 /readyz 探針。
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import requests
from config import *

header("01  Health Check")

checks = [
    (RETRIEVE_API,    "/healthz",  "retrieve-api    /healthz"),
    (RETRIEVE_API,    "/readyz",   "retrieve-api    /readyz"),
    (ADMIN_API,       "/healthz",  "admin-api       /healthz"),
    (INGEST_WORKER,   "/healthz",  "ingest-worker   /healthz"),
    (WEBHOOK_SERVICE, "/healthz",  "webhook-service /healthz"),
    (MARKER_SERVICE,  "/healthz",  "marker-service  /healthz"),
    (NOSTR_PROXY,     "/health",   "nostr-proxy     /health"),
]

for base, path, label in checks:
    try:
        r = requests.get(base + path, timeout=5)
        body = r.json()
        status = body.get("status", "")
        if r.status_code == 200 and status in ("ok", "ready"):
            ok(f"{label}  →  status={status!r}")
        else:
            fail(f"{label}  →  HTTP {r.status_code}  body={body}")
    except Exception as e:
        fail(f"{label}  →  {e}")

summary()
