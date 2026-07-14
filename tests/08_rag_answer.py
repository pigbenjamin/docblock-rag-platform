"""
08 RAG Answer
POST /v1/answer
對有 query 權限的文件提問，驗證：
  - answer 非空
  - hits 有內容
  - context 包含引用
對無 query 權限的用戶提問，驗證 → 403（節點存在但被拒絕）

u002 的「無權限」狀態由本測試自己設定（見 §2），不依賴 05/06 測試留下的
狀態——那兩支測試會在結束時清空自己寫入的 ACL entries，跑完後 u002 會
恢復成單純透過部門資料夾繼承取得權限，不再是 deny。
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import requests
from config import *

header("08  RAG Answer")

TARGET_DOC = "test-eurfood"
TARGET_UUID = EXISTING_DOCS[TARGET_DOC]
USER_WITH_ACCESS = USERS["u001"]    # A 部門，透過資料夾繼承取得 query 權限
USER_NO_ACCESS   = USERS["u002"]    # 本測試會明確 deny 這個使用者
OWNER_KM_USER    = USERS["u001"]    # 假設是這份文件所在部門（A）的 KM

QUESTION = "What are the main food safety regulations in this document?"

# ── 1. 有 query 權限：u001 問 test-eurfood ───────────────────
info(f"POST /v1/answer  document_id={TARGET_UUID}  user=u001（query 權限）")
r = requests.post(
    f"{RETRIEVE_API}/v1/answer",
    json={
        "document_id": TARGET_UUID,
        "question": QUESTION,
        "user_id":  USER_WITH_ACCESS,
        "top_k":    5,
    },
    timeout=RAG_TIMEOUT,   # RAG 需要 LLM 生成，等待較長
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

# ── 2. 無 query 權限：明確 deny u002 後再問同一份文件 ─────────
info(f"PUT /v1/nodes/{TARGET_UUID}/acl  明確 deny u002")
r = write_node_acl(
    TARGET_UUID, OWNER_KM_USER,
    [{"subject_type": "user", "subject_id": USER_NO_ACCESS, "actions": ["browse", "query", "read"], "effect": "deny"}],
)
if r.status_code != 200:
    fail(f"設定 u002 deny 失敗 → HTTP {r.status_code}  body={r.text[:200]}")
else:
    ok(f"u002 deny 已設定 → permission_revision={r.json().get('permission_revision')}")

    info(f"POST /v1/answer  document_id={TARGET_UUID}  user=u002（deny）")
    r = requests.post(
        f"{RETRIEVE_API}/v1/answer",
        json={
            "document_id": TARGET_UUID,
            "question": QUESTION,
            "user_id":  USER_NO_ACCESS,
            "top_k":    5,
        },
        timeout=SEARCH_TIMEOUT,
    )
    if r.status_code == 403:
        ok(f"deny 用戶 → 正確拒絕  HTTP 403")
    else:
        fail(f"deny 用戶 → 預期 403，got HTTP {r.status_code}  body={r.text[:200]}")

    # 還原：清空 entries，回到純繼承
    info("清空測試 ACL entries")
    r = write_node_acl(TARGET_UUID, OWNER_KM_USER, [])
    if r.status_code == 200:
        ok("已還原為純繼承")
    else:
        fail(f"還原失敗 → HTTP {r.status_code}  body={r.text[:200]}")

# ── 3. 有 query 權限：u001 問 deptA_IT-OT_Network_Policy ──────
TARGET_DOC2 = "deptA_IT-OT_Network_Policy"
TARGET_UUID2 = EXISTING_DOCS[TARGET_DOC2]
QUESTION2   = "What is the IT/OT network isolation policy?"

info(f"POST /v1/answer  document_id={TARGET_UUID2}  user=u001（A 部門）")
r = requests.post(
    f"{RETRIEVE_API}/v1/answer",
    json={
        "document_id": TARGET_UUID2,
        "question": QUESTION2,
        "user_id":  USER_WITH_ACCESS,
        "top_k":    5,
    },
    timeout=RAG_TIMEOUT,
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
    fail(f"u001 對 {TARGET_DOC2} 應有 query 權限，但被拒絕（403）")
else:
    fail(f"answer → HTTP {r.status_code}")

summary()
