"""
04 Node ACL Write & Delete
PUT /v1/nodes/{document_id}/acl — 取代舊版 POST /v1/acl/write-map/delete-map。

新模型是「整批取代節點自己的 entries」，不是逐條增刪；node 預設
inherit_acl=true，沒有自己的 entries 時完全繼承 parent 資料夾的權限。
所以「刪除規則」在新模型下 = 重新 PUT 一份不含該規則的 entries 清單。

這份文件掛在 dept-A 資料夾下，資料夾本身只授權 dept-A（見
scripts/migrate_fb1_nodes_acl.sql 的遷移結果），不會透過資料夾繼承讓
dept-B 看到——所以跟 05/06 一樣，改成自己在開頭用 PUT 建立一個已知
baseline（dept-B allow），驗證 deny 覆蓋 baseline、還原 baseline 後
deny 消失，不依賴「清空後靠資料夾繼承恢復」這個對這份文件不成立的假設。

驗證：
  0. 建立 baseline：dept-B allow（文件自己的 entry，不靠資料夾繼承）
  1. 寫入 2 條明確 deny（覆蓋 baseline 的 allow）
  2. 搜尋確認：目標部門/使用者變成看不到，其他部門不受影響
  3. 還原成 baseline（回到 dept-B allow）
  4. 搜尋確認：deny 消失，恢復可查詢
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import requests
from config import *

header("04  Node ACL Write & Delete")

DOC_ID   = "deptA_IT-OT_Network_Policy"
DOC_UUID = EXISTING_DOCS[DOC_ID]
OWNER_KM_USER = USERS["u001"]  # 假設是這份文件所在部門（A）的 KM


def used_ids(user_id):
    r = requests.post(
        f"{RETRIEVE_API}/v1/search",
        json={"query": "IT OT 網路", "user_id": user_id, "document_ids": [DOC_UUID], "top_k": 5},
        timeout=ACL_TIMEOUT,
    )
    if r.status_code != 200:
        return None
    return set(r.json().get("document_ids_used", []))


# ── 0. 建立 baseline：dept-B allow ──────────────────────────────
info(f"PUT /v1/nodes/{DOC_UUID}/acl  建立 baseline（{DEPT_B}=allow）")
baseline_entries = [
    {"subject_type": "department", "subject_id": DEPT_B, "actions": ["browse", "query", "read"], "effect": "allow"},
]
r = write_node_acl(DOC_UUID, OWNER_KM_USER, baseline_entries)
if r.status_code != 200:
    fail(f"建立 baseline 失敗 → HTTP {r.status_code}  body={r.text[:300]}")
    summary()
else:
    ok(f"baseline 已建立 → permission_revision={r.json().get('permission_revision')}")

# ── 1. 寫入 2 條明確 deny（覆蓋 baseline 的 allow）───────────────
info(f"PUT /v1/nodes/{DOC_UUID}/acl  document_id={DOC_UUID}")
entries = [
    {"subject_type": "department", "subject_id": DEPT_B, "actions": ["browse", "query", "read"], "effect": "deny"},
    {"subject_type": "user", "subject_id": USERS["u004"], "actions": ["browse", "query", "read"], "effect": "deny"},
]
r = write_node_acl(DOC_UUID, OWNER_KM_USER, entries)
if r.status_code != 200:
    fail(f"PUT acl → HTTP {r.status_code}  body={r.text[:300]}")
    summary()
else:
    body = r.json()
    ok(f"PUT acl → permission_revision={body.get('permission_revision')}")

# ── 2. 搜尋驗證：dept-B、u004 應變成看不到，u001（同部門）不受影響 ──
info("搜尋驗證 deny 生效")

cases = [
    (USERS["u001"], "u001(A部門, owner-KM)", True),
    (USERS["u003"], "u003(B部門)", False),
    (USERS["u004"], "u004(明確 deny)", False),
]
for user_id, label, expect_visible in cases:
    used = used_ids(user_id)
    if used is None:
        fail(f"search({label}) 失敗")
        continue
    visible = DOC_UUID in used
    if visible == expect_visible:
        ok(f"{label}  可見={visible}（符合預期）")
    else:
        fail(f"{label}  可見={visible}，預期={expect_visible}")

# ── 3. 還原成 baseline（回到 dept-B allow）──────────────────────
info(f"PUT /v1/nodes/{DOC_UUID}/acl（還原成 baseline：{DEPT_B}=allow）")
r = write_node_acl(DOC_UUID, OWNER_KM_USER, baseline_entries)
if r.status_code == 200:
    ok(f"還原成功 → permission_revision={r.json().get('permission_revision')}")
else:
    fail(f"還原失敗 → HTTP {r.status_code}  body={r.text[:200]}")

# ── 4. 驗證還原後 dept-B 使用者恢復可見 ─────────────────────────
info("搜尋驗證還原效果（B 部門使用者應恢復可見）")
used = used_ids(USERS["u003"])
if used is not None and DOC_UUID in used:
    ok(f"還原後 u003 可見（baseline 恢復生效）")
else:
    fail(f"還原後 u003 仍不可見（預期 baseline 恢復）：document_ids_used={used}")

summary()
