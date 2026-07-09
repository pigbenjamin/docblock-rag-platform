"""
04 ACL Write & Delete
POST /v1/acl/write-map  — 設定文件存取規則
POST /v1/acl/delete-map — 刪除存取規則
驗證：寫入後規則生效、刪除後規則消失（透過搜尋行為驗證）
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import requests
from config import *

header("04  ACL Write & Delete")

ACL_HEADERS = {"X-Acl-Secret": ACL_ADMIN_SECRET, "Content-Type": "application/json"}
DOC_ID  = "deptA_IT-OT_Network_Policy"
DOC_UUID = EXISTING_DOCS[DOC_ID]

# ── 1. 寫入 3 條 ACL 規則 ────────────────────────────────────
info(f"POST /v1/acl/write-map  document_id={DOC_UUID}")
rules = [
    {"principal_type": "department", "principal_id": "dept-A", "effect": "detail"},
    {"principal_type": "department", "principal_id": "dept-B", "effect": "summary"},
    {"principal_type": "user",       "principal_id": USERS["u004"], "effect": "deny"},
]
r = requests.post(
    f"{DOCUMENT_API}/v1/acl/write-map",
    headers=ACL_HEADERS,
    json={"document_id": DOC_UUID, "access_rules": rules},
    timeout=ACL_TIMEOUT,
)
if r.status_code != 200:
    fail(f"write-map → HTTP {r.status_code}  body={r.text[:200]}")
else:
    body = r.json()
    if body.get("ok") and body.get("count") == 3:
        ok(f"write-map → ok=true  count={body['count']}")
    else:
        fail(f"write-map → 預期 count=3，got: {body}")

# ── 2. 搜尋驗證：各用戶存取層級應符合規則 ───────────────────
info("搜尋驗證 write-map 效果")

cases = [
    (USERS["u001"], "u001(dept-A)", "detail"),   # dept-A → detail
    (USERS["u003"], "u003(dept-B)", "summary"),  # dept-B → summary
    (USERS["u004"], "u004(deny)",   "deny"),      # user rule → deny
]

for user_id, label, expected_access in cases:
    r = requests.post(
        f"{RETRIEVE_API}/v1/search",
        json={"query": "IT OT 網路", "user_id": user_id, "document_ids": [DOC_UUID], "top_k": 5},
        timeout=ACL_TIMEOUT,
    )
    if r.status_code != 200:
        fail(f"search({label}) → HTTP {r.status_code}")
        continue
    data   = r.json()
    access = data.get("access", {}).get(DOC_UUID, "deny")  # 無 access 欄位 = 被拒
    hits   = len(data.get("hits", []))

    if access == expected_access:
        ok(f"{label}  access={access}  hits={hits}")
    else:
        fail(f"{label}  預期 access={expected_access}，got={access}  principals={data.get('user',{}).get('principals','?')}")

# ── 3. 刪除 ACL 規則 ─────────────────────────────────────────
info(f"POST /v1/acl/delete-map  document_id={DOC_UUID}")
principals_to_delete = [
    "department:dept-B",
    f"user:{USERS['u004']}",
]
r = requests.post(
    f"{DOCUMENT_API}/v1/acl/delete-map",
    headers=ACL_HEADERS,
    json={"document_id": DOC_UUID, "principals": principals_to_delete},
    timeout=ACL_TIMEOUT,
)
if r.status_code != 200:
    fail(f"delete-map → HTTP {r.status_code}  body={r.text[:200]}")
else:
    body = r.json()
    if body.get("ok") and body.get("count") == 2:
        ok(f"delete-map → ok=true  count={body['count']}（刪除 dept-B 與 u004）")
    else:
        fail(f"delete-map → 預期 count=2，got: {body}")

# ── 4. 驗證刪除後 dept-B 用戶變為 deny ───────────────────────
info("搜尋驗證 delete-map 效果（dept-B 用戶應變 deny）")
r = requests.post(
    f"{RETRIEVE_API}/v1/search",
    json={"query": "IT OT 網路", "user_id": USERS["u003"], "document_ids": [DOC_UUID], "top_k": 5},
    timeout=ACL_TIMEOUT,
)
if r.status_code == 200:
    access = r.json().get("access", {}).get(DOC_UUID, "deny")
    if access == "deny":
        ok(f"刪除後 dept-B 用戶 access={access}（正確）")
    else:
        fail(f"刪除後 dept-B 用戶 access={access}（預期 deny）")
else:
    fail(f"驗證搜尋 → HTTP {r.status_code}")

# ── 5. 還原：重設為 dept-A=detail 只保留一條 ─────────────────
info("還原 ACL（只保留 dept-A=detail）")
r = requests.post(
    f"{DOCUMENT_API}/v1/acl/write-map",
    headers=ACL_HEADERS,
    json={
        "document_id": DOC_UUID,
        "access_rules": [
            {"principal_type": "department", "principal_id": "dept-A", "effect": "detail"},
        ],
    },
    timeout=ACL_TIMEOUT,
)
if r.status_code == 200 and r.json().get("ok"):
    ok("ACL 已還原為 dept-A=detail")
else:
    fail(f"還原失敗 → {r.text[:100]}")

summary()
