"""
直接測試 nostr-consumer 的 LiteLLM 呼叫，不需要 Nostr relay。
用法：
  LITELLM_BASE_URL=http://... python3 test_litellm.py
"""

import json
import os
import sys

sys.path.insert(0, ".")
#os.environ.setdefault("LITELLM_BASE_URL", "http://litellm-service.enterprise-brain.svc.cluster.local:4000")
os.environ.setdefault("LITELLM_BASE_URL", "http://10.90.20.55:30400")
os.environ.setdefault("EMBED_MODEL", "qwen3-embedding")
os.environ.setdefault("RERANK_MODEL", "qwen3-reranker")

from app.main import _call_embedding, _call_rerank

TEST_PUBKEY = "6d129961839371998d59a9a817d1274c79a758dd06c628c35a30a1a0682e50d5"

# ── Embedding ──────────────────────────────────────────────────
print("=== Test Kind 2000: Embedding ===")
emb_payload = json.dumps({"model": "qwen3-embedding", "input": "什麼是氮氣？"})
result = _call_embedding(emb_payload, TEST_PUBKEY)
if result:
    vec = json.loads(result).get("embedding", [])
    print(f"✅ embedding dim={len(vec)}  first5={vec[:5]}")
else:
    print("❌ embedding 回傳 None")

print()

# ── Rerank ─────────────────────────────────────────────────────
print("=== Test Kind 2001: Rerank ===")
rerank_payload = json.dumps({
    "model": "qwen3-reranker",
    "query": "什麼是氮氣？",
    "documents": [
        "氮氣是大氣中含量最多的氣體，約佔 78%。",
        "氧氣用於呼吸和燃燒。",
        "氮氣在工業上用於防氧化保護。",
    ],
})
result = _call_rerank(rerank_payload, TEST_PUBKEY)
if result:
    data = json.loads(result)
    print(f"✅ rerank results={json.dumps(data.get('results', []), ensure_ascii=False, indent=2)}")
else:
    print("❌ rerank 回傳 None")
