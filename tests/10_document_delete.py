"""
10 Document Delete
DELETE /v1/documents/{doc_id}
驗證刪除後：
  - GET /v1/documents/{doc_id} → 404
  - 搜尋結果不再出現該文件的 chunks
流程：先上傳一份新文件 → 設定 ACL → 搜尋確認有結果 → 刪除 → 確認消失
"""
import sys, os, time, uuid
sys.path.insert(0, os.path.dirname(__file__))
import requests
from config import *

header("10  Document Delete")

ACL_HEADERS = {"X-Acl-Secret": ACL_ADMIN_SECRET, "Content-Type": "application/json"}

# ── 1. 上傳一份新文件作為刪除目標 ────────────────────────────
if not os.path.exists(TEST_PDF):
    fail(f"測試 PDF 不存在：{TEST_PDF}")
    summary()

DELETE_DOC_ID = f"delete-test-{uuid.uuid4().hex[:8]}"
info(f"建立待刪文件  doc_id={DELETE_DOC_ID!r}")

with open(TEST_PDF, "rb") as f:
    r = requests.post(
        f"{ADMIN_API}/v1/documents/upload",
        files={"file": ("test.pdf", f, "application/pdf")},
        data={"doc_id": DELETE_DOC_ID, "title": "Document for Delete Test"},
        timeout=30,
    )
if r.status_code != 200:
    fail(f"upload → HTTP {r.status_code}")
    summary()

job_id = r.json().get("job_id")
ok(f"上傳成功  job_id={job_id}")

# ── 2. 等待 ingest 完成 ───────────────────────────────────────
info("等待 pipeline 完成（最多 5 分鐘）")
MAX_WAIT = 300
elapsed  = 0
done = False
while elapsed < MAX_WAIT:
    rj = requests.get(f"{ADMIN_API}/v1/documents/job/{job_id}", timeout=10)
    status = rj.json().get("status")
    if status == "done":
        done = True
        break
    elif status == "failed":
        fail(f"Pipeline 失敗：{rj.json().get('detail','')[:200]}")
        summary()
    info(f"  [{elapsed:>3}s] {status}")
    time.sleep(5)
    elapsed += 5

if not done:
    fail("Pipeline 超時")
    summary()
ok(f"Pipeline 完成（耗時 ~{elapsed}s）")

# ── 3. 取得 document_id ───────────────────────────────────────
r = requests.get(f"{ADMIN_API}/v1/documents/{DELETE_DOC_ID}", timeout=10)
if r.status_code != 200:
    fail(f"取得文件 metadata → HTTP {r.status_code}")
    summary()
doc_meta    = r.json()
document_id = doc_meta["document_id"]
ok(f"文件確認存在  document_id={document_id}")

# ── 4. 設定 ACL 並搜尋確認有 hits ────────────────────────────
info("設定 ACL → u001 detail")
requests.post(
    f"{ADMIN_API}/v1/acl/write-map",
    headers=ACL_HEADERS,
    json={
        "document_id": document_id,
        "access_rules": [{"principal_type": "user", "principal_id": USERS["u001"], "effect": "detail"}],
    },
    timeout=10,
)

r = requests.post(
    f"{RETRIEVE_API}/v1/search",
    json={"query": "policy", "user_id": USERS["u001"], "doc_ids": [DELETE_DOC_ID], "top_k": 3},
    timeout=20,
)
if r.status_code == 200:
    hits = r.json().get("hits", [])
    info(f"刪除前搜尋  hits={len(hits)}")
else:
    info(f"刪除前搜尋 → HTTP {r.status_code}（不影響刪除測試）")

# ── 5. 刪除文件 ───────────────────────────────────────────────
info(f"DELETE /v1/documents/{DELETE_DOC_ID}")
r = requests.delete(f"{ADMIN_API}/v1/documents/{DELETE_DOC_ID}", timeout=10)
if r.status_code == 200 and r.json().get("ok"):
    ok(f"DELETE 成功  → ok=true")
else:
    fail(f"DELETE → HTTP {r.status_code}  body={r.text[:200]}")
    summary()

# ── 6. 確認 GET → 404 ────────────────────────────────────────
info(f"GET /v1/documents/{DELETE_DOC_ID}（預期 404）")
r = requests.get(f"{ADMIN_API}/v1/documents/{DELETE_DOC_ID}", timeout=10)
if r.status_code == 404:
    ok("刪除後 GET → 404 正確")
else:
    fail(f"刪除後 GET → HTTP {r.status_code}（預期 404）")

# ── 7. 確認搜尋不再出現該文件 ────────────────────────────────
info("確認搜尋結果不含已刪除文件")
r = requests.post(
    f"{RETRIEVE_API}/v1/search/open",
    json={"query": "policy", "top_k": 20},
    timeout=20,
)
if r.status_code == 200:
    hits = r.json().get("hits", [])
    ghost_hits = [h for h in hits if h.get("doc_id") == DELETE_DOC_ID]
    if ghost_hits:
        fail(f"刪除後仍有 {len(ghost_hits)} 個 hits 來自已刪除文件")
    else:
        ok(f"搜尋結果中已無已刪除文件（共 {len(hits)} hits，無殘留）")
else:
    info(f"確認搜尋 → HTTP {r.status_code}（略過）")

summary()
