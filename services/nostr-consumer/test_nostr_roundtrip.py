"""
測試完整 Nostr 往返：
  此腳本 → relay → nostr-consumer → LiteLLM → relay → 此腳本

前提：
  1. Nostr relay 在線
  2. nostr-consumer 已啟動並訂閱
  3. LiteLLM 可達

用法：
  RELAY_URL=wss://... python3 test_nostr_roundtrip.py
"""

import hashlib
import json
import os
import ssl
import sys
import time

import websocket
import secp256k1

RELAY_URL = os.getenv("RELAY_URL", "wss://10.90.20.55:9443/")
# 使用 nostr-proxy 的金鑰（已在 consumer allowlist 內）
PRIV_KEY_HEX = os.getenv("NOSTR_PRIV_KEY", "d303435269265a0bf6fd14e9be3612a1ade969cb99c1975d9a567d5a39785cbf")
PUB_KEY_HEX  = os.getenv("NOSTR_PUB_KEY",  "6d129961839371998d59a9a817d1274c79a758dd06c628c35a30a1a0682e50d5")
TIMEOUT = int(os.getenv("TEST_TIMEOUT", "60"))


def _sign(priv_hex: str, msg_hex: str) -> str:
    pk = secp256k1.PrivateKey(bytes.fromhex(priv_hex), raw=True)
    return pk.schnorr_sign(bytes.fromhex(msg_hex), None, raw=True).hex()


def _build_event(content: str, kind: int) -> dict:
    created_at = int(time.time())
    data = [0, PUB_KEY_HEX, created_at, kind, [], content]
    serialized = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    event_id = hashlib.sha256(serialized.encode()).hexdigest()
    return {
        "id": event_id,
        "pubkey": PUB_KEY_HEX,
        "created_at": created_at,
        "kind": kind,
        "tags": [],
        "content": content,
        "sig": _sign(PRIV_KEY_HEX, event_id),
    }


def send_and_wait(payload: dict, kind: int) -> dict | None:
    content = json.dumps(payload, ensure_ascii=False)
    event = _build_event(content, kind)
    sub_id = f"test_{event['id'][:8]}"

    ssl_opt = {"cert_reqs": ssl.CERT_NONE}
    ws = websocket.create_connection(RELAY_URL, sslopt=ssl_opt, timeout=TIMEOUT)
    try:
        ws.send(json.dumps(["EVENT", event]))
        ws.send(json.dumps(["REQ", sub_id, {"#e": [event["id"]]}]))
        print(f"  → sent kind={kind} event_id={event['id'][:8]}")

        deadline = time.time() + TIMEOUT
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
                    continue
                ws.send(json.dumps(["CLOSE", sub_id]))
                return json.loads(ai_event["content"])
    finally:
        ws.close()
    return None


# ── Kind 2000: Embedding ───────────────────────────────────────
print("=== Kind 2000: Embedding ===")
result = send_and_wait({"model": "qwen3-embedding", "input": "什麼是氮氣？"}, kind=2000)
if result:
    vec = result.get("embedding", [])
    print(f"✅ dim={len(vec)}  first5={vec[:5]}\n")
else:
    print("❌ 超時或無回覆\n")
    sys.exit(1)

# ── Kind 2001: Rerank ──────────────────────────────────────────
print("=== Kind 2001: Rerank ===")
result = send_and_wait({
    "model": "qwen3-reranker",
    "query": "什麼是氮氣？",
    "documents": [
        "氮氣是大氣中含量最多的氣體，約佔 78%。",
        "氧氣用於呼吸和燃燒。",
        "氮氣在工業上用於防氧化保護。",
    ],
}, kind=2001)
if result:
    print(f"✅ results={json.dumps(result.get('results', []), ensure_ascii=False, indent=2)}\n")
else:
    print("❌ 超時或無回覆\n")
    sys.exit(1)

print("All tests passed.")


# 測試方法
# cd services/nostr-consumer
# 1. 先啟動 consumer
"""
BOT_PRIVATE_KEY=91e49b43bd358f737bff5564c11ca9b278fdcf52b7622837ec3207ee4aadb408 \
BOT_PUBKEY=c54b07d81065e7953861a5e979f71b8c0364138cf8d604f33e3e23fc05e30b7b \
LITELLM_BASE_URL=http://10.90.20.55:30400 \
python3 -m app.main
"""
# 2. 再跑測試
"""
RELAY_URL=wss://10.90.20.55:9443/ python3 test_nostr_roundtrip.py
"""
