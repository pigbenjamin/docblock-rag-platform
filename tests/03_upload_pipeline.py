"""
03 Upload & Full Pipeline
POST /v1/documents/upload — 上傳 PDF（不帶 document_id，由伺服器生成新 UUID），
  帶必填 parent_folder_id（取代舊版 departments 欄位）+ X-User-Id，
  觸發完整 ingest pipeline。node 在呼叫 ingest-worker 之前就已建立，
  繼承 parent_folder_id 資料夾的 ACL（見 FB-3）。
GET  /v1/documents/job/{job_id} — 每 5 秒輪詢，直到 done 或 failed
最後確認文件出現於 GET /v1/documents/，並驗證資料夾繼承的 ACL 生效
（上傳者所屬部門的成員可 query 存取新文件）。
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(__file__))
import requests
from config import *

header("03  Upload & Full Pipeline")

# ── 前置檢查 ────────────────────────────────────────────────
if not os.path.exists(TEST_PDF):
    fail(f"測試 PDF 不存在：{TEST_PDF}")
    fail("請先執行：docker cp compose-ingest-worker-1:/data/uploads/104fa00d-.../deptA_IT-OT_Network_Policy.pdf tests/fixtures/test.pdf")
    summary()

UPLOADER_USER_ID = USERS["u001"]

info(f"找出 u001 所屬部門（{DEPT_A}）的根資料夾 node_id")
parent_folder_id = find_root_folder_id(UPLOADER_USER_ID, DEPT_A)
if not parent_folder_id:
    fail(f"找不到部門 '{DEPT_A}' 的根資料夾——確認 DEPT_A 是否對應真實 Keycloak 群組名稱、"
         f"scripts/migrate_fb1_nodes_acl.sql 是否已執行")
    summary()
ok(f"parent_folder_id={parent_folder_id}")

info(f"上傳新文件（不指定 document_id，由伺服器生成），PDF={TEST_PDF}")

# ── 1. 上傳 PDF ──────────────────────────────────────────────
info("POST /v1/documents/upload")
with open(TEST_PDF, "rb") as f:
    r = requests.post(
        f"{DOCUMENT_API}/v1/documents/upload",
        files={"file": ("test.pdf", f, "application/pdf")},
        data={"title": "Test Upload Document", "parent_folder_id": parent_folder_id},
        headers={"X-User-Id": UPLOADER_USER_ID},
        timeout=30,
    )

if r.status_code != 200:
    fail(f"upload → HTTP {r.status_code}  body={r.text[:300]}")
    summary()

body = r.json()
job_id = body.get("job_id")
document_id = body.get("document_id")
info(f"job_id={job_id}  document_id={document_id}")

if job_id and document_id and body.get("status") == "submitted":
    ok(f"upload 成功 → job_id={job_id}  document_id={document_id}")
else:
    fail(f"upload 回應格式錯誤：{body}")
    summary()

# ── 1b. 缺少必填欄位 → 預期 4xx ─────────────────────────────
info("POST /v1/documents/upload（新文件缺少 parent_folder_id）→ 預期 400")
with open(TEST_PDF, "rb") as f:
    r_missing_parent = requests.post(
        f"{DOCUMENT_API}/v1/documents/upload",
        files={"file": ("test.pdf", f, "application/pdf")},
        headers={"X-User-Id": UPLOADER_USER_ID},
        timeout=30,
    )
if r_missing_parent.status_code == 400:
    ok(f"缺少 parent_folder_id → HTTP 400（正確拒絕）")
else:
    fail(f"缺少 parent_folder_id → 預期 400，got {r_missing_parent.status_code}")

info("POST /v1/documents/upload（缺少身分）→ 預期 401")
with open(TEST_PDF, "rb") as f:
    r_missing_user = requests.post(
        f"{DOCUMENT_API}/v1/documents/upload",
        files={"file": ("test.pdf", f, "application/pdf")},
        data={"parent_folder_id": parent_folder_id},
        timeout=30,
    )
if r_missing_user.status_code == 401:
    ok(f"缺少身分 → HTTP 401（正確拒絕）")
else:
    fail(f"缺少身分 → 預期 401，got {r_missing_user.status_code}")

# ── 2. 輪詢進度 ──────────────────────────────────────────────
info("輪詢 GET /v1/documents/job/{job_id}（最多等 5 分鐘）")
MAX_WAIT = 300
interval = 5
elapsed  = 0
final_status = None

while elapsed < MAX_WAIT:
    r = requests.get(
        f"{DOCUMENT_API}/v1/documents/job/{job_id}",
        headers={"X-User-Id": UPLOADER_USER_ID},
        timeout=10,
    )
    if r.status_code != 200:
        fail(f"poll → HTTP {r.status_code}")
        break
    j = r.json()
    status = j.get("status")
    detail = j.get("detail", "")
    info(f"  [{elapsed:>3}s] status={status}  detail={detail!r}")

    if status == "done":
        final_status = "done"
        break
    elif status == "failed":
        final_status = "failed"
        break

    time.sleep(interval)
    elapsed += interval

if final_status == "done":
    ok(f"Pipeline 完成（耗時 ~{elapsed}s）")
elif final_status == "failed":
    fail(f"Pipeline 失敗：{j.get('detail','')[:400]}")
else:
    fail(f"Pipeline 超時（>{MAX_WAIT}s），最後狀態：{status}")

# ── 3. 確認文件出現於清單 ────────────────────────────────────
if final_status == "done":
    info(f"GET /v1/documents/{document_id}")
    r = requests.get(
        f"{DOCUMENT_API}/v1/documents/{document_id}",
        headers={"X-User-Id": UPLOADER_USER_ID},
        timeout=10,
    )
    if r.status_code == 200:
        d = r.json()
        ok(f"文件已建立 → document_id={d['document_id']}  version={d['active_version']}")
        print(f"\n  document_id : {d['document_id']}")
        print(f"  title       : {d.get('title')}")
        print(f"  version     : {d['active_version']}")
    else:
        fail(f"文件建立後查不到 → HTTP {r.status_code}")

    # ── 4. 驗證資料夾繼承的 ACL 生效 ─────────────────────────
    # 上傳時沒有另外指定 acl，新文件的 node 預設 inherit_acl=true，
    # 應該直接繼承 parent_folder_id（部門根資料夾）的 query 權限。
    info(f"驗證資料夾繼承 ACL：u001（{DEPT_A} 部門）應可 query 新文件")
    r = requests.post(
        f"{RETRIEVE_API}/v1/search",
        json={"query": "test", "user_id": USERS["u001"], "document_ids": [document_id], "top_k": 5},
        timeout=SEARCH_TIMEOUT,
    )
    if r.status_code == 200:
        used = r.json().get("document_ids_used", [])
        if document_id in used:
            ok(f"u001 可 query 新文件（資料夾繼承生效，document_ids_used={used}）")
        else:
            fail(f"u001 無法 query 新文件（預期資料夾繼承生效）：document_ids_used={used}")
    else:
        fail(f"部門 ACL 驗證 search → HTTP {r.status_code}")

summary()
