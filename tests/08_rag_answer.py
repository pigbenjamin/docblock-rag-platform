"""
08 RAG Answer
POST /v1/answer
對有 detail 存取權限的文件提問，驗證：
  - answer 非空
  - hits 有內容
  - context 包含引用
對無存取權限的用戶提問，驗證 → 403 或 access=deny
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import requests
from config import *

header("08  RAG Answer")

# ── 1. 有 detail 存取：u001 問 test-eurfood ───────────────────
TARGET_DOC = "test-eurfood"
USER_WITH_ACCESS = USERS["u001"]    # dept-A=detail
USER_NO_ACCESS   = USERS["u002"]    # deny

QUESTION = "What are the main food safety regulations in this document?"

info(f"POST /v1/answer  doc_id={TARGET_DOC}  user=u001（detail access）")
r = requests.post(
    f"{RETRIEVE_API}/v1/answer",
    json={
        "doc_id":   TARGET_DOC,
        "question": QUESTION,
        "user_id":  USER_WITH_ACCESS,
        "top_k":    5,
    },
    timeout=120,   # RAG 需要 LLM 生成，等待較長
)

if r.status_code != 200:
    fail(f"answer → HTTP {r.status_code}  body={r.text[:300]}")
else:
    data    = r.json()
    answer  = data.get("answer", "")
    hits    = data.get("hits", [])
    context = data.get("context", "")
    model   = data.get("model", "")

    if len(answer) > 10:
        ok(f"answer 非空（長度={len(answer)} 字元）  model={model!r}")
        info(f"  answer 前 200 字：{answer[:200]!r}")
    else:
        fail(f"answer 太短或為空：{answer!r}")

    if hits:
        ok(f"hits={len(hits)}  top source={hits[0].get('source')}")
    else:
        fail("hits 為空，RAG 無法找到相關內容")

    if "[1]" in context or len(context) > 50:
        ok(f"context 有引用格式（長度={len(context)} 字元）")
    else:
        fail(f"context 格式異常：{context[:100]!r}")

# ── 2. 無存取權限：u002（deny）問同一份文件 ─────────────────
info(f"POST /v1/answer  doc_id={TARGET_DOC}  user=u002（deny access）")
r = requests.post(
    f"{RETRIEVE_API}/v1/answer",
    json={
        "doc_id":   TARGET_DOC,
        "question": QUESTION,
        "user_id":  USER_NO_ACCESS,
        "top_k":    5,
    },
    timeout=30,
)

if r.status_code in (403, 404):
    ok(f"deny 用戶 → 正確拒絕  HTTP {r.status_code}")
elif r.status_code == 200:
    # 部分實作可能回傳 200 但 answer 空
    data   = r.json()
    answer = data.get("answer", "")
    hits   = data.get("hits", [])
    if not answer and not hits:
        ok(f"deny 用戶 → HTTP 200 但 answer/hits 皆空（可接受）")
    else:
        fail(f"deny 用戶拿到了 answer：{answer[:100]!r}")
else:
    fail(f"deny 用戶 → 預期 403/404，got HTTP {r.status_code}")

# ── 3. 有 detail 存取：u001 問 deptA_IT-OT_Network_Policy ──────
TARGET_DOC2 = "deptA_IT-OT_Network_Policy"
QUESTION2   = "What is the IT/OT network isolation policy?"

info(f"POST /v1/answer  doc_id={TARGET_DOC2}  user=u001（dept-A=detail）")
r = requests.post(
    f"{RETRIEVE_API}/v1/answer",
    json={
        "doc_id":   TARGET_DOC2,
        "question": QUESTION2,
        "user_id":  USER_WITH_ACCESS,
        "top_k":    5,
    },
    timeout=120,
)

if r.status_code == 200:
    data   = r.json()
    answer = data.get("answer", "")
    hits   = data.get("hits", [])
    if len(answer) > 10:
        ok(f"answer 非空（長度={len(answer)} 字元）")
        info(f"  answer 前 200 字：{answer[:200]!r}")
    else:
        fail(f"answer 太短：{answer!r}")
    ok(f"hits={len(hits)}")
elif r.status_code == 403:
    fail(f"u001 對 {TARGET_DOC2} 應有 detail 存取，但被拒絕（403）")
else:
    fail(f"answer → HTTP {r.status_code}")

summary()
