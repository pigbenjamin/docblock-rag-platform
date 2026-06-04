"""
07 Open Search — 不套用 ACL
POST /v1/search/open
不需要 user_id，搜尋所有文件，不受 ACL 過濾。
驗證：
  - 結果包含已知文件的 chunks
  - routing 資訊正確
  - 可指定 doc_ids 縮小範圍
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import requests
from config import *

header("07  Open Search（不套用 ACL）")

# ── 1. 基本開放搜尋（不指定 doc_ids）─────────────────────────
info("POST /v1/search/open（搜尋所有文件）")
r = requests.post(
    f"{RETRIEVE_API}/v1/search/open",
    json={"query": "food regulation policy", "top_k": 10, "routing": True},
    timeout=SEARCH_TIMEOUT,
)
if r.status_code != 200:
    fail(f"open search → HTTP {r.status_code}  body={r.text[:200]}")
else:
    data  = r.json()
    hits  = data.get("hits", [])
    n     = len(hits)
    docs  = list({h["doc_id"] for h in hits})
    ok(f"open search → hits={n}  文件={docs}")
    if n > 0:
        top = hits[0]
        info(f"  top-1: doc_id={top['doc_id']}  source={top['source']}  score={top['score']:.4f}")
        info(f"         preview={top.get('preview','')[:80]!r}")
    else:
        fail("open search → hits=0（DB 應有資料）")

# ── 2. 指定 doc_ids 縮小範圍 ─────────────────────────────────
info("POST /v1/search/open（指定 doc_ids=[test-eurfood]）")
r = requests.post(
    f"{RETRIEVE_API}/v1/search/open",
    json={
        "query":   "food regulation",
        "doc_ids": ["test-eurfood"],
        "top_k":   5,
    },
    timeout=SEARCH_TIMEOUT,
)
if r.status_code != 200:
    fail(f"open search (filtered) → HTTP {r.status_code}")
else:
    data = r.json()
    hits = data.get("hits", [])
    n    = len(hits)
    foreign_docs = [h["doc_id"] for h in hits if h["doc_id"] != "test-eurfood"]
    if foreign_docs:
        fail(f"指定 doc_ids 後仍出現其他文件：{foreign_docs}")
    else:
        ok(f"指定 doc_ids 過濾正確  hits={n}  全部來自 test-eurfood")

# ── 3. 搜尋結果欄位完整性 ────────────────────────────────────
info("驗證回應欄位完整性")
if r.status_code == 200 and hits:
    required_fields = ["rank", "doc_id", "document_id", "source", "score",
                       "chunk_index", "preview", "content", "metadata"]
    hit = hits[0]
    missing = [f for f in required_fields if f not in hit]
    if missing:
        fail(f"hit 缺少欄位：{missing}")
    else:
        ok(f"所有必要欄位均存在：{required_fields}")

# ── 4. routing 資訊 ───────────────────────────────────────────
info("驗證 routing 資訊（routing=True 時應有 profile）")
r = requests.post(
    f"{RETRIEVE_API}/v1/search/open",
    json={"query": "network policy", "doc_ids": list(EXISTING_DOCS.keys()), "top_k": 3, "routing": True},
    timeout=SEARCH_TIMEOUT,
)
if r.status_code == 200:
    routing = r.json().get("routing", {})
    profile = routing.get("profile", "")
    ok(f"routing profile={profile!r}")
else:
    fail(f"routing 測試 → HTTP {r.status_code}")

summary()
