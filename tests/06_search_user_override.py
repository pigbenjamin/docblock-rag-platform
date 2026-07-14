"""
06 ACL User Override — 優先順序驗證
聚焦在「user 規則覆蓋 department 規則」的核心場景，新模型下用同一節點的
acl_entries 表達：同節點內 user-type entry 比 department-type entry 優先
（見 docblock_core/authz.py 的判定規則）。

跟舊版一樣，測試自己動態寫入/修改 ACL，不依賴預先存在的 fixture 狀態：

  1. 建立初始狀態：DEPT_A=allow，user u002=deny
  2. u001（無 user 覆蓋） → 應可見
  3. u002（user=deny）    → 應不可見，即使 DEPT_A=allow
  4. 動態把 u002 的規則改成 allow → 應變可見
  5. 動態改回 deny → 應變不可見
  6. 全程確認 u001 不受影響
  7. 還原：清空 entries
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import requests
from config import *

header("06  ACL User Override — 優先順序驗證")

TARGET_DOC   = "test-eurfood"
TARGET_UUID  = EXISTING_DOCS[TARGET_DOC]
QUERY        = "European food regulation"
OWNER_KM_USER = USERS["u001"]  # 假設是這份文件所在部門（A）的 KM


def is_visible(user_id):
    r = requests.post(
        f"{RETRIEVE_API}/v1/search",
        json={"query": QUERY, "user_id": user_id, "document_ids": [TARGET_UUID], "top_k": 5},
        timeout=SEARCH_TIMEOUT,
    )
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    data = r.json()
    used = set(data.get("document_ids_used", []))
    hits = len(data.get("hits", []))
    return (TARGET_UUID in used), hits


def set_u002_rule(effect):
    entries = [
        {"subject_type": "department", "subject_id": DEPT_A, "actions": ["browse", "query", "read"], "effect": "allow"},
        {"subject_type": "user", "subject_id": USERS["u002"], "actions": ["browse", "query", "read"], "effect": effect},
    ]
    r = write_node_acl(TARGET_UUID, OWNER_KM_USER, entries)
    return r.status_code == 200


# ── 0. 建立初始狀態：DEPT_A=allow，u002=deny ─────────────────
info("建立初始狀態：DEPT_A=allow，u002=deny")
if not set_u002_rule("deny"):
    fail("建立初始 ACL 狀態失敗")
    summary()
ok("初始狀態已建立")

# ── 1. u001：無 user 規則 → 應可見 ─────────────────────────────
info("場景 1：u001（DEPT_A=allow，無 user 規則）")
visible, hits = is_visible(USERS["u001"])
if visible:
    ok(f"u001  可見={visible}  hits={hits}")
else:
    fail(f"u001  預期可見，got 可見={visible}")

# ── 2. u002：DEPT_A=allow，user=deny → 應不可見 ────────────────
info("場景 2：u002（DEPT_A=allow，but user=deny）")
visible, hits = is_visible(USERS["u002"])
if visible is False and hits == 0:
    ok(f"u002  可見={visible}  hits={hits}  user 覆蓋 dept 成功")
else:
    fail(f"u002  預期不可見/0 hits，got 可見={visible} hits={hits}")

# ── 3. 動態修改 u002 規則為 allow → 應變可見 ──────────────────
info("場景 3：動態改 u002 規則 deny→allow")
if set_u002_rule("allow"):
    ok("PUT acl u002=allow 成功")
    visible, hits = is_visible(USERS["u002"])
    if visible:
        ok(f"u002 改規則後  可見={visible}  hits={hits}  優先順序正確")
    else:
        fail(f"u002 改規則後  預期可見，got 可見={visible}")
else:
    fail("PUT acl u002=allow 失敗")

# ── 4. 還原 u002 規則為 deny ──────────────────────────────────
info("場景 4：還原 u002 規則為 deny")
if set_u002_rule("deny"):
    visible, hits = is_visible(USERS["u002"])
    if visible is False:
        ok(f"u002 還原後  可見={visible}  hits={hits}")
    else:
        fail(f"u002 還原後  預期不可見，got 可見={visible}")
else:
    fail("PUT acl u002=deny 失敗（還原失敗）")

# ── 5. 確認 u001 不受影響 ─────────────────────────────────────
info("場景 5：確認 u001 不受 u002 規則變更影響")
visible, hits = is_visible(USERS["u001"])
if visible:
    ok(f"u001 不受影響  可見={visible}  hits={hits}")
else:
    fail(f"u001 受到影響  可見={visible}（預期仍可見）")

# ── 還原：清空 entries，回到純繼承 ────────────────────────────
info("清空測試 ACL entries")
r = write_node_acl(TARGET_UUID, OWNER_KM_USER, [])
if r.status_code == 200:
    ok("已還原為純繼承")
else:
    fail(f"還原失敗 → HTTP {r.status_code}  body={r.text[:200]}")

summary()
