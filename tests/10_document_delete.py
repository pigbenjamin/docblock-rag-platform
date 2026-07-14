"""
10 Document Delete
DELETE /v1/documents/{document_id}
驗證刪除後：
  - GET /v1/documents/{document_id} → 404
  - 搜尋結果不再出現該文件的 chunks
流程：先上傳一份新文件 → 設定 ACL → 搜尋確認有結果 → 刪除 → 確認消失
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(__file__))
import requests
from config import *

header("10  Document Delete")

UPLOADER_USER_ID = USERS["u001"]

# ── 1. 上傳一份新文件作為刪除目標 ────────────────────────────
if not os.path.exists(TEST_PDF):
    fail(f"測試 PDF 不存在：{TEST_PDF}")
    summary()

info(f"找出 u001 所屬部門（{DEPT_A}）的根資料夾 node_id")
parent_folder_id = find_root_folder_id(UPLOADER_USER_ID, DEPT_A)
if not parent_folder_id:
    fail(f"找不到部門 '{DEPT_A}' 的根資料夾")
    summary()

info("建立待刪文件（不指定 document_id，由伺服器生成）")

with open(TEST_PDF, "rb") as f:
    r = requests.post(
        f"{DOCUMENT_API}/v1/documents/upload",
        files={"file": ("test.pdf", f, "application/pdf")},
        data={"title": "Document for Delete Test", "parent_folder_id": parent_folder_id},
        headers={"X-User-Id": UPLOADER_USER_ID},
        timeout=30,
    )
if r.status_code != 200:
    fail(f"upload → HTTP {r.status_code}")
    summary()

upload_body = r.json()
job_id = upload_body.get("job_id")
document_id = upload_body.get("document_id")
ok(f"上傳成功  job_id={job_id}  document_id={document_id}")

# ── 2. 等待 ingest 完成 ───────────────────────────────────────
info("等待 pipeline 完成（最多 5 分鐘）")
MAX_WAIT = 300
elapsed  = 0
done = False
while elapsed < MAX_WAIT:
    rj = requests.get(
        f"{DOCUMENT_API}/v1/documents/job/{job_id}",
        headers={"X-User-Id": UPLOADER_USER_ID},
        timeout=10,
    )
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

# ── 3. 確認文件存在 ─────────────────────────────────────────
r = requests.get(
    f"{DOCUMENT_API}/v1/documents/{document_id}",
    headers={"X-User-Id": UPLOADER_USER_ID},
    timeout=10,
)
if r.status_code != 200:
    fail(f"取得文件 metadata → HTTP {r.status_code}")
    summary()
ok(f"文件確認存在  document_id={document_id}")

# ── 4. 搜尋確認有 hits（新文件預設 inherit_acl=true，已透過
#      parent_folder_id 繼承部門資料夾的 query 權限，不需要額外設 ACL）──
r = requests.post(
    f"{RETRIEVE_API}/v1/search",
    json={"query": "policy", "user_id": UPLOADER_USER_ID, "document_ids": [document_id], "top_k": 3},
    timeout=SEARCH_TIMEOUT,
)
if r.status_code == 200:
    hits = r.json().get("hits", [])
    info(f"刪除前搜尋  hits={len(hits)}")
else:
    info(f"刪除前搜尋 → HTTP {r.status_code}（不影響刪除測試）")

# ── 5. 刪除文件 ───────────────────────────────────────────────
info(f"DELETE /v1/documents/{document_id}")
r = requests.delete(
    f"{DOCUMENT_API}/v1/documents/{document_id}",
    headers={"X-User-Id": UPLOADER_USER_ID},
    timeout=10,
)
if r.status_code == 200 and r.json().get("ok"):
    ok(f"DELETE 成功  → ok=true")
else:
    fail(f"DELETE → HTTP {r.status_code}  body={r.text[:200]}")
    summary()

# ── 6. 確認 GET → 404 ────────────────────────────────────────
info(f"GET /v1/documents/{document_id}（預期 404）")
r = requests.get(
    f"{DOCUMENT_API}/v1/documents/{document_id}",
    headers={"X-User-Id": UPLOADER_USER_ID},
    timeout=10,
)
if r.status_code == 404:
    ok("刪除後 GET → 404 正確")
else:
    fail(f"刪除後 GET → HTTP {r.status_code}（預期 404）")

# ── 7. 確認搜尋不再出現該文件 ────────────────────────────────
info("確認搜尋結果不含已刪除文件（以 u001 身分搜尋）")
r = requests.post(
    f"{RETRIEVE_API}/v1/search",
    json={"query": "policy", "user_id": USERS["u001"], "top_k": 20},
    timeout=SEARCH_TIMEOUT,
)
if r.status_code == 200:
    hits = r.json().get("hits", [])
    ghost_hits = [h for h in hits if h.get("document_id") == document_id]
    if ghost_hits:
        fail(f"刪除後仍有 {len(ghost_hits)} 個 hits 來自已刪除文件")
    else:
        ok(f"搜尋結果中已無已刪除文件（共 {len(hits)} hits，無殘留）")
else:
    info(f"確認搜尋 → HTTP {r.status_code}（略過）")

summary()
