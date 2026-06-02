"""
Nostr LLM Proxy
===============
Exposes OpenAI-compatible HTTP endpoints and translates each request into a
Nostr event.  The nostr-consumer picks it up, calls LiteLLM, and replies back
via the relay.  The proxy waits for that reply and returns it as an HTTP
response.

Kind mapping
  2000  POST /v1/embeddings         →  LiteLLM /v1/embeddings
  2001  POST /v1/rerank             →  LiteLLM /v1/rerank
  2002  POST /v1/chat/completions   →  LiteLLM /v1/chat/completions

Pass-through (no Nostr, Ollama-compat legacy)
  POST /api/chat      →  upstream Ollama/LiteLLM
  POST /api/generate  →  upstream Ollama/LiteLLM

Routing flags (all default true — set to "false" to bypass Nostr)
  EMBED_VIA_NOSTR    bool  /v1/embeddings
  RERANK_VIA_NOSTR   bool  /v1/rerank
  CHAT_VIA_NOSTR     bool  /v1/chat/completions

Environment variables
  RELAY_URL           wss://…      Nostr relay
  NOSTR_PRIV_KEY      64-char hex  signing key  (pubkey must be in consumer allowlist)
  NOSTR_PUB_KEY       64-char hex  x-only public key
  OLLAMA_DIRECT_URL   http://…     upstream Ollama/LiteLLM for direct (non-Nostr) calls
  PROXY_PORT          int          HTTP listen port (default 8800)
"""

import asyncio
import hashlib
import json
import os
import ssl
import time

import requests as req_lib
import websocket
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.schnorr import sign_schnorr

RELAY_URL = os.getenv("RELAY_URL", "wss://10.90.20.55:9443/")
PRIV_KEY_HEX = os.getenv("NOSTR_PRIV_KEY", "0000000000000000000000000000000000000000000000000000000000000001")
PUB_KEY_HEX = os.getenv("NOSTR_PUB_KEY", "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798")
OLLAMA_DIRECT_URL = os.getenv("OLLAMA_DIRECT_URL", "http://host.docker.internal:11434").rstrip("/")

def _env_bool(key: str, default: bool = True) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

EMBED_VIA_NOSTR  = _env_bool("EMBED_VIA_NOSTR",  True)
RERANK_VIA_NOSTR = _env_bool("RERANK_VIA_NOSTR", True)
CHAT_VIA_NOSTR   = _env_bool("CHAT_VIA_NOSTR",   True)

app = FastAPI(title="nostr-proxy")


# ------------------------------------------------------------------
# Nostr round-trip (blocking — run via asyncio.to_thread)
# ------------------------------------------------------------------

def _nostr_request_sync(content: str, kind: int, timeout: int = 120) -> dict:
    created_at = int(time.time())
    event_data = [0, PUB_KEY_HEX, created_at, kind, [], content]
    serialized = json.dumps(event_data, separators=(",", ":"), ensure_ascii=False)
    event_id = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    sig = sign_schnorr(PRIV_KEY_HEX, event_id)

    event = {
        "id": event_id,
        "pubkey": PUB_KEY_HEX,
        "created_at": created_at,
        "kind": kind,
        "tags": [],
        "content": content,
        "sig": sig,
    }

    ssl_opt = {"cert_reqs": ssl.CERT_NONE}
    ws = websocket.create_connection(RELAY_URL, sslopt=ssl_opt, timeout=timeout)
    try:
        ws.send(json.dumps(["EVENT", event]))

        sub_id = f"sub_{event_id[:8]}"
        ws.send(json.dumps(["REQ", sub_id, {"#e": [event_id]}]))

        deadline = time.time() + timeout
        while time.time() < deadline:
            ws.settimeout(min(5.0, deadline - time.time()))
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            data = json.loads(raw)
            if data[0] == "EVENT" and data[1] == sub_id:
                ai_event = data[2]
                if ai_event["pubkey"] == PUB_KEY_HEX:
                    continue  # skip our own echo
                ws.send(json.dumps(["CLOSE", sub_id]))
                return json.loads(ai_event["content"])
            # EOSE → keep waiting
    finally:
        ws.close()

    raise TimeoutError(
        f"No Nostr reply for kind={kind} event_id={event_id} within {timeout}s"
    )


# ------------------------------------------------------------------
# Format helpers
# ------------------------------------------------------------------

def _to_oai_embedding(result: dict) -> dict:
    """Convert consumer reply {"embedding":[...]} → OpenAI {"data":[...]} format."""
    if "data" in result:
        return result  # already OpenAI format
    return {
        "object": "list",
        "data": [{"object": "embedding", "index": 0, "embedding": result["embedding"]}],
    }


# ------------------------------------------------------------------
# Endpoints — OpenAI-compatible
# ------------------------------------------------------------------

@app.post("/v1/embeddings")
async def embeddings(request: Request):
    body = await request.json()
    if EMBED_VIA_NOSTR:
        content = json.dumps(body, ensure_ascii=False)
        result = await asyncio.to_thread(_nostr_request_sync, content, 2000)
        return JSONResponse(_to_oai_embedding(result))
    else:
        def _call():
            return req_lib.post(
                f"{OLLAMA_DIRECT_URL}/v1/embeddings", json=body, timeout=120
            )
        r = await asyncio.to_thread(_call)
        return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/v1/rerank")
async def rerank(request: Request):
    body = await request.json()
    if RERANK_VIA_NOSTR:
        content = json.dumps(body, ensure_ascii=False)
        result = await asyncio.to_thread(_nostr_request_sync, content, 2001)
        return JSONResponse(result)
    else:
        def _call():
            return req_lib.post(
                f"{OLLAMA_DIRECT_URL}/v1/rerank", json=body, timeout=120
            )
        r = await asyncio.to_thread(_call)
        return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    if CHAT_VIA_NOSTR:
        content = json.dumps(body, ensure_ascii=False)
        result = await asyncio.to_thread(_nostr_request_sync, content, 2002, timeout=180)
        return JSONResponse(result)
    else:
        def _call():
            return req_lib.post(
                f"{OLLAMA_DIRECT_URL}/v1/chat/completions", json=body, timeout=180
            )
        r = await asyncio.to_thread(_call)
        return JSONResponse(r.json(), status_code=r.status_code)


# ------------------------------------------------------------------
# Legacy Ollama-compat pass-through
# ------------------------------------------------------------------

@app.post("/api/embeddings")
async def embeddings_legacy(request: Request):
    body = await request.json()
    # Convert Ollama format → OpenAI format and forward
    oai_body = {
        "model": body.get("model", ""),
        "input": body.get("prompt", body.get("input", "")),
    }
    if EMBED_VIA_NOSTR:
        content = json.dumps(oai_body, ensure_ascii=False)
        result = await asyncio.to_thread(_nostr_request_sync, content, 2000)
        return JSONResponse(_to_oai_embedding(result))
    else:
        def _call():
            return req_lib.post(
                f"{OLLAMA_DIRECT_URL}/v1/embeddings", json=oai_body, timeout=120
            )
        r = await asyncio.to_thread(_call)
        return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/api/chat")
async def chat_passthrough(request: Request):
    body = await request.json()

    def _call():
        return req_lib.post(
            f"{OLLAMA_DIRECT_URL}/api/chat", json=body, timeout=180
        )

    r = await asyncio.to_thread(_call)
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/api/generate")
async def generate_passthrough(request: Request):
    body = await request.json()

    def _call():
        return req_lib.post(
            f"{OLLAMA_DIRECT_URL}/api/generate", json=body, timeout=180
        )

    r = await asyncio.to_thread(_call)
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/health")
async def health():
    return {"status": "ok"}
