"""
Nostr Consumer — Kind 2000 (embedding) & Kind 2001 (rerank)
============================================================
Subscribes to the Nostr relay for Kind 2000 and 2001 events published by
nostr-proxy, forwards them to LiteLLM, and publishes the result back to the
relay so nostr-proxy can return it to the caller.

Flow
  nostr-proxy  →  relay  →  [this service]  →  LiteLLM  →  reply event  →  relay  →  nostr-proxy

Environment variables
  RELAY_URL          wss://…
  LITELLM_BASE_URL   http://…:4000
  EMBED_MODEL        LiteLLM model name for embeddings   (default: Qwen3-Embedding-8B)
  RERANK_MODEL       LiteLLM model name for rerank       (default: Qwen3-Reranker-8B)
  ALLOWLIST_PATH     path to allowlist.json              (default: /app/allowlist.json)
  BOT_PRIVATE_KEY    64-char hex  consumer bot signing key
  BOT_PUBKEY         64-char hex  consumer bot public key
"""

import hashlib
import json
import os
import signal
import ssl
import sqlite3
import sys
import time

import requests
import websocket

from app.schnorr import verify_schnorr, sign_schnorr

# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------
RELAY_URL = os.getenv("RELAY_URL", "wss://10.90.20.55:9443/")
#LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "http://litellm-service.enterprise-brain.svc.cluster.local:4000")
LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "http://10.90.20.55:30400")
LITELLM_API_KEY  = os.getenv("LITELLM_API_KEY", "")
EMBED_MODEL = os.getenv("EMBED_MODEL", "qwen3-embedding")
RERANK_MODEL = os.getenv("RERANK_MODEL", "qwen3-reranker")
CHAT_MODEL = os.getenv("CHAT_MODEL", "qwen3:8b")


def _litellm_headers() -> dict:
    if LITELLM_API_KEY:
        return {"Authorization": f"Bearer {LITELLM_API_KEY}"}
    return {}

BOT_PRIVATE_KEY = os.getenv("BOT_PRIVATE_KEY", "")
BOT_PUBKEY = os.getenv("BOT_PUBKEY", "")

DB_PATH = os.getenv("DATABASE_URL", "./data/audit.db")
ALLOWLIST_PATH = os.getenv("ALLOWLIST_PATH", "./allowlist.json")

# Populated by _setup() at startup; empty list is safe for import-time use
ALLOWED_LIST: list = []
start_time: int = 0

# WebSocketApp instance — held so the signal handler can call .close()
_ws_app: websocket.WebSocketApp | None = None


def _handle_shutdown(sig, _):
    print(f"\n收到 signal {sig}，正在關閉 consumer...")
    if _ws_app is not None:
        _ws_app.close()
    sys.exit(0)


signal.signal(signal.SIGTERM, _handle_shutdown)
signal.signal(signal.SIGINT,  _handle_shutdown)

# ------------------------------------------------------------------
# Audit DB
# ------------------------------------------------------------------

def _init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS audit_logs
           (id TEXT PRIMARY KEY, pubkey TEXT, kind INTEGER,
            content TEXT, sig TEXT, timestamp INTEGER, status TEXT)"""
    )
    conn.commit()
    conn.close()
    print(f"✅ 稽核資料庫: {DB_PATH}")


def _save_log(event: dict, status: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO audit_logs VALUES (?,?,?,?,?,?,?)",
        (
            event["id"],
            event["pubkey"],
            event.get("kind", 0),
            event["content"],
            event["sig"],
            event["created_at"],
            status,
        ),
    )
    conn.commit()
    conn.close()
    print(f"LOG  id={event['id'][:8]}  status={status}")


def _setup():
    """Initialise DB, load allowlist, set start_time. Called only at runtime."""
    global ALLOWED_LIST, start_time
    start_time = int(time.time())
    print("--- 正在啟動 Nostr Consumer (Kind 2000/2001/2002) ---")
    print(f"啟動時間: {start_time}")
    print(f"RELAY_URL: {RELAY_URL}")
    print(f"LITELLM_BASE_URL: {LITELLM_BASE_URL}")
    _init_db()
    with open(ALLOWLIST_PATH, "r") as f:
        ALLOWED_LIST = json.load(f)["allowed_pubkeys"]
    print(f"白名單數量: {len(ALLOWED_LIST)}")

# ------------------------------------------------------------------
# LiteLLM calls
# ------------------------------------------------------------------

def _call_embedding(content: str, pubkey: str) -> str | None:
    try:
        payload = json.loads(content)
    except Exception:
        payload = {}
    model = payload.get("model", EMBED_MODEL)
    input_text = payload.get("input", "")
    print(f"🔢 Embedding [{pubkey[:8]}] model={model} input_len={len(input_text)}")
    try:
        r = requests.post(
            f"{LITELLM_BASE_URL}/v1/embeddings",
            headers=_litellm_headers(),
            json={"model": model, "input": input_text, "encoding_format": "float"},
            timeout=60,
        )
        if r.status_code == 200:
            embedding = r.json()["data"][0]["embedding"]
            return json.dumps({"embedding": embedding})
        print(f"❌ Embedding HTTP {r.status_code}: {r.text[:200]}")
        return json.dumps({"error": f"LiteLLM HTTP {r.status_code}", "detail": r.text[:200]})
    except Exception as e:
        print(f"❌ Embedding 異常: {e}")
        return json.dumps({"error": str(e)})


def _call_chat(content: str, pubkey: str) -> str | None:
    try:
        payload = json.loads(content)
    except Exception:
        payload = {}
    model = payload.get("model", CHAT_MODEL)
    messages = payload.get("messages", [])
    print(f"💬 Chat [{pubkey[:8]}] model={model} messages={len(messages)}")
    try:
        r = requests.post(
            f"{LITELLM_BASE_URL}/v1/chat/completions",
            headers=_litellm_headers(),
            json={"model": model, "messages": messages, "stream": False},
            timeout=300,
        )
        if r.status_code == 200:
            return r.text
        print(f"❌ Chat HTTP {r.status_code}: {r.text[:200]}")
        return json.dumps({"error": {"message": f"LiteLLM HTTP {r.status_code}", "detail": r.text[:200]}})
    except Exception as e:
        print(f"❌ Chat 異常: {e}")
        return json.dumps({"error": {"message": str(e)}})


def _call_rerank(content: str, pubkey: str) -> str | None:
    try:
        payload = json.loads(content)
    except Exception:
        payload = {}
    model = payload.get("model", RERANK_MODEL)
    query = payload.get("query", "")
    documents = payload.get("documents", [])
    print(f"📊 Rerank [{pubkey[:8]}] model={model} docs={len(documents)}")
    try:
        r = requests.post(
            f"{LITELLM_BASE_URL}/v1/rerank",
            headers=_litellm_headers(),
            json={"model": model, "query": query, "documents": documents},
            timeout=120,
        )
        if r.status_code == 200:
            return r.text
        print(f"❌ Rerank HTTP {r.status_code}: {r.text[:200]}")
        return json.dumps({"error": {"message": f"LiteLLM HTTP {r.status_code}", "detail": r.text[:200]}})
    except Exception as e:
        print(f"❌ Rerank 異常: {e}")
        return json.dumps({"error": {"message": str(e)}})

# ------------------------------------------------------------------
# Reply via Nostr
# ------------------------------------------------------------------

def _send_reply(ws, original_event: dict, reply_content: str) -> bool:
    if not BOT_PRIVATE_KEY or not BOT_PUBKEY:
        print("❌ BOT_PRIVATE_KEY / BOT_PUBKEY 未設定，無法回覆")
        return False

    created_at = int(time.time())
    kind = 1000
    tags = [
        ["e", original_event["id"], RELAY_URL, "reply"],
        ["p", original_event["pubkey"]],
    ]
    serialized = json.dumps(
        [0, BOT_PUBKEY, created_at, kind, tags, reply_content],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    event_id = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    try:
        sig = sign_schnorr(BOT_PRIVATE_KEY, event_id)
    except Exception as e:
        print(f"❌ 簽名失敗: {e}")
        return False

    reply_event = {
        "id": event_id,
        "pubkey": BOT_PUBKEY,
        "created_at": created_at,
        "kind": kind,
        "tags": tags,
        "content": reply_content,
        "sig": sig,
    }
    try:
        ws.send(json.dumps(["EVENT", reply_event]))
        print(f"✅ 回覆已送出 reply_id={event_id[:8]}")
        return True
    except Exception as e:
        print(f"❌ 送出回覆失敗: {e}")
        return False

# ------------------------------------------------------------------
# Message processing
# ------------------------------------------------------------------

def _process_message(event: dict, ws) -> None:
    if event.get("created_at", 0) < start_time:
        return
    if event["pubkey"] == BOT_PUBKEY:
        return
    if event["pubkey"] not in ALLOWED_LIST:
        _save_log(event, "REJECTED_UNAUTHORIZED")
        return

    serialized = json.dumps(
        [0, event["pubkey"], event["created_at"], event["kind"], event["tags"], event["content"]],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    recalculated_id = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    if recalculated_id != event["id"]:
        print(f"❌ ID 不符 received={event['id'][:8]} calc={recalculated_id[:8]}")
        _save_log(event, "REJECTED_BAD_ID")
        return

    try:
        if not verify_schnorr(event["pubkey"], event["sig"], recalculated_id):
            _save_log(event, "REJECTED_BAD_SIG")
            return
    except Exception as e:
        print(f"❌ 簽名驗證崩潰: {e}")
        return

    kind = event.get("kind", 0)
    if kind == 2000:
        reply = _call_embedding(event["content"], event["pubkey"])
    elif kind == 2001:
        reply = _call_rerank(event["content"], event["pubkey"])
    elif kind == 2002:
        reply = _call_chat(event["content"], event["pubkey"])
    else:
        print(f"⚠️  未知 kind={kind}，略過")
        return

    if reply:
        ok = _send_reply(ws, event, reply)
        _save_log(event, "SUCCESS" if ok else "REPLY_SEND_FAILED")
    else:
        _save_log(event, "LITELLM_EMPTY_REPLY")

# ------------------------------------------------------------------
# WebSocket handlers
# ------------------------------------------------------------------

def on_open(ws):
    print(">>> 連線成功，訂閱 Kind 2000 / 2001 ...")
    ws.send(json.dumps(["REQ", "rag_consumer", {"kinds": [2000, 2001, 2002]}]))


def on_message(ws, message):
    data = json.loads(message)
    if data[0] == "OK":
        print(f"📡 ACK id={data[1][:8]} ok={data[2]}")
    elif data[0] == "EVENT":
        _process_message(data[2], ws)


def on_error(ws, error):
    print(f"❌ WebSocket 錯誤: {error}")


def on_close(ws, code, msg):
    print(f"🔌 連線關閉 code={code}")


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

if __name__ == "__main__":
    _setup()
    while True:
        try:
            ws = websocket.WebSocketApp(
                RELAY_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            _ws_app = ws
            ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
        except SystemExit:
            print("👋 Consumer 已停止")
            break
        except Exception as e:
            print(f"❌ Consumer 崩潰，5 秒後重啟: {e}")
            time.sleep(5)
