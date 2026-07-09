"""
06 ACL User Override — 優先順序驗證
聚焦在「user 規則覆蓋 department 規則」的核心場景：

  test-eurfood ACL:
    dept-A = detail    （u001, u002 所屬部門）
    user u002 = deny   ← user(30) > department(10)，deny 優先

  測試步驟：
  1. u001（dept-A，無 user 規則） → 應拿到 detail
  2. u002（dept-A，user=deny）   → 應拿到 deny，即使 dept-A=detail
  3. 動態修改 u002 的 user 規則為 summary，再搜尋 → 應拿到 summary
  4. 還原 u002 的 user 規則為 deny
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import requests
from config import *

header("06  ACL User Override — 優先順序驗證")

ACL_HEADERS = {"X-Acl-Secret": ACL_ADMIN_SECRET, "Content-Type": "application/json"}
TARGET_DOC   = "test-eurfood"
TARGET_UUID  = EXISTING_DOCS[TARGET_DOC]
QUERY        = "European food regulation"

def search_access(user_id):
    r = requests.post(
        f"{RETRIEVE_API}/v1/search",
        json={"query": QUERY, "user_id": user_id, "document_ids": [TARGET_UUID], "top_k": 5},
        timeout=SEARCH_TIMEOUT,
    )
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    data = r.json()
    access = data.get("access", {}).get(TARGET_UUID, "unknown")
    hits   = len(data.get("hits", []))
    return access, hits

def write_user_rule(user_id, effect):
    r = requests.post(
        f"{DOCUMENT_API}/v1/acl/write-map",
        headers=ACL_HEADERS,
        json={
            "document_id": TARGET_UUID,
            "access_rules": [{"principal_type": "user", "principal_id": user_id, "effect": effect}],
        },
        timeout=10,
    )
    return r.status_code == 200 and r.json().get("ok")

# ── 1. u001：dept-A=detail，無 user 規則 → detail ─────────────
info("場景 1：u001（dept-A=detail，無 user 規則）")
access, hits = search_access(USERS["u001"])
if access == "detail":
    ok(f"u001  access={access}  hits={hits}")
else:
    fail(f"u001  預期 detail，got={access}")

# ── 2. u002：dept-A=detail，user=deny → deny ─────────────────
info("場景 2：u002（dept-A=detail，but user=deny）")
access, hits = search_access(USERS["u002"])
if access == "deny" and hits == 0:
    ok(f"u002  access={access}  hits={hits}  user 覆蓋 dept 成功")
else:
    fail(f"u002  預期 deny/0 hits，got access={access} hits={hits}")

# ── 3. 動態修改 u002 user 規則為 summary → 應變 summary ──────
info("場景 3：動態改 u002 user 規則 deny→summary")
if write_user_rule(USERS["u002"], "summary"):
    ok("write-map u002=summary 成功")
    access, hits = search_access(USERS["u002"])
    if access == "summary":
        ok(f"u002 改規則後  access={access}  hits={hits}  優先順序正確")
    else:
        fail(f"u002 改規則後  預期 summary，got={access}")
else:
    fail("write-map u002=summary 失敗")

# ── 4. 還原 u002 user 規則為 deny ────────────────────────────
info("場景 4：還原 u002 user 規則為 deny")
if write_user_rule(USERS["u002"], "deny"):
    access, hits = search_access(USERS["u002"])
    if access == "deny":
        ok(f"u002 還原後  access={access}  hits={hits}")
    else:
        fail(f"u002 還原後  預期 deny，got={access}")
else:
    fail("write-map u002=deny 失敗（還原失敗）")

# ── 5. 確認 u001 不受影響 ────────────────────────────────────
info("場景 5：確認 u001 不受 u002 規則變更影響")
access, hits = search_access(USERS["u001"])
if access == "detail":
    ok(f"u001 不受影響  access={access}  hits={hits}")
else:
    fail(f"u001 受到影響  access={access}（預期 detail）")

summary()
