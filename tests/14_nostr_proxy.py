"""
14  Nostr Proxy — OpenAI-compatible endpoints
==========================================
測試 nostr-proxy 的三個主要 endpoint，驗證：
  /v1/embeddings       → 回傳 OpenAI format  {"data":[{"embedding":[...]}]}
  /v1/rerank           → 回傳 {"results":[{"index":…,"relevance_score":…}]}
  /v1/chat/completions → 回傳 OpenAI format  {"choices":[{"message":{...}}]}

前提：nostr-proxy + nostr-consumer + LiteLLM 全部在線，
      且 EMBED_VIA_NOSTR / RERANK_VIA_NOSTR / CHAT_VIA_NOSTR 均為 true（預設值）。

用法：
  NOSTR_PROXY=http://localhost:8800 python3 14_nostr_proxy.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import requests
from config import *

header("14  Nostr Proxy（OpenAI-format endpoints）")

EMBED_MODEL  = os.getenv("EMBED_MODEL",  "qwen3-embedding")
RERANK_MODEL = os.getenv("RERANK_MODEL", "qwen3-reranker")
CHAT_MODEL   = os.getenv("CHAT_MODEL",   "qwen3.5-9b")

# ── 1. /v1/embeddings ─────────────────────────────────────────
info(f"POST {NOSTR_PROXY}/v1/embeddings  model={EMBED_MODEL!r}")
r = requests.post(
    f"{NOSTR_PROXY}/v1/embeddings",
    json={"model": EMBED_MODEL, "input": "什麼是氮氣？"},
    timeout=60,
)
if r.status_code != 200:
    fail(f"/v1/embeddings → HTTP {r.status_code}  body={r.text[:300]}")
else:
    data = r.json()
    try:
        vec = data["data"][0]["embedding"]
        if isinstance(vec, list) and len(vec) > 0:
            ok(f"embedding dim={len(vec)}  first3={[round(v,4) for v in vec[:3]]}")
        else:
            fail(f"embedding 格式異常：{data}")
    except (KeyError, IndexError, TypeError) as e:
        fail(f"回傳格式不符 OpenAI 規格：{e}  body={str(data)[:200]}")

# ── 2. /v1/rerank ─────────────────────────────────────────────
info(f"POST {NOSTR_PROXY}/v1/rerank  model={RERANK_MODEL!r}")
QUERY     = "什麼是氮氣？"
DOCUMENTS = [
    "氮氣是大氣中含量最多的氣體，約佔 78%。",
    "氧氣用於呼吸和燃燒。",
    "氮氣在工業上用於防氧化保護。",
]
r = requests.post(
    f"{NOSTR_PROXY}/v1/rerank",
    json={"model": RERANK_MODEL, "query": QUERY, "documents": DOCUMENTS},
    timeout=120,
)
if r.status_code != 200:
    fail(f"/v1/rerank → HTTP {r.status_code}  body={r.text[:300]}")
else:
    data    = r.json()
    results = data.get("results", [])
    if not results:
        fail(f"rerank results 為空：{data}")
    else:
        indices = [res.get("index") for res in results]
        scores  = [round(res.get("relevance_score", 0), 4) for res in results]
        ok(f"rerank results={len(results)}  indices={indices}  scores={scores}")
        top_idx = results[0].get("index")
        if top_idx == 0:
            ok(f"最相關文件正確（index=0：氮氣定義）")
        else:
            info(f"最相關文件 index={top_idx}（可接受，視模型而定）")

# ── 3. /v1/chat/completions ───────────────────────────────────
info(f"POST {NOSTR_PROXY}/v1/chat/completions  model={CHAT_MODEL!r}")
r = requests.post(
    f"{NOSTR_PROXY}/v1/chat/completions",
    json={
        "model": CHAT_MODEL,
        "messages": [
            {"role": "system", "content": "You are a concise assistant. Reply in one sentence."},
            {"role": "user",   "content": "What is nitrogen?"},
        ],
        "stream": False,
    },
    timeout=180,
)
if r.status_code != 200:
    fail(f"/v1/chat/completions → HTTP {r.status_code}  body={r.text[:300]}")
else:
    data = r.json()
    try:
        choices = data.get("choices", [])
        if not choices:
            fail(f"choices 為空：{data}")
        else:
            msg = choices[0].get("message", {})
            role    = msg.get("role", "")
            content = msg.get("content", "")
            if role == "assistant" and len(content) > 5:
                ok(f"chat 回覆正常  role={role!r}  content 前 80 字：{content[:80]!r}")
            else:
                fail(f"chat 回覆格式異常：role={role!r}  content={content!r}")
    except (KeyError, IndexError, TypeError) as e:
        fail(f"回傳格式不符 OpenAI 規格：{e}  body={str(data)[:200]}")

# ── 4. /api/embeddings（legacy Ollama-compat，轉換後仍走 Nostr）─
info(f"POST {NOSTR_PROXY}/api/embeddings  (legacy Ollama format)")
r = requests.post(
    f"{NOSTR_PROXY}/api/embeddings",
    json={"model": EMBED_MODEL, "prompt": "legacy format test"},
    timeout=60,
)
if r.status_code != 200:
    fail(f"/api/embeddings (legacy) → HTTP {r.status_code}  body={r.text[:200]}")
else:
    data = r.json()
    try:
        vec = data["data"][0]["embedding"]
        if isinstance(vec, list) and len(vec) > 0:
            ok(f"legacy /api/embeddings → 成功  dim={len(vec)}")
        else:
            fail(f"legacy embedding 格式異常：{data}")
    except (KeyError, IndexError, TypeError) as e:
        fail(f"legacy 回傳格式異常：{e}  body={str(data)[:200]}")

summary()
