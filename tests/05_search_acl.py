"""
05 ACL Search — 多用戶 allow/deny 驗證

舊版依賴 test-eurfood 上預先設好的 detail/summary/deny 規則；FB 系列改造把
分級授權整條拿掉（D5），這份舊 fixture 語意已經不相容，而且遷移後實際變成
什麼樣子需要 live DB 才能確認。改成測試自己在開頭用 PUT /v1/nodes/{id}/acl
設好已知狀態，驗證完再還原——不依賴外部隱藏的 fixture 假設。

設定的 ACL 狀態：
  DEPT_A          allow (browse/query/read)
  user u002       deny   ← A 部門成員，user 規則覆蓋 department
  DEPT_B          deny
  user u003       allow  ← B 部門成員，user 規則覆蓋 department

預期：
  u001 (A，無覆蓋)         → 可見
  u002 (A，user=deny)      → 不可見
  u003 (B，user=allow)     → 可見
  u004 (B，無覆蓋)         → 不可見
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import requests
from config import *

header("05  ACL Search — 多用戶 allow/deny 驗證")

TARGET_DOC  = "test-eurfood"
TARGET_UUID = EXISTING_DOCS[TARGET_DOC]
QUERY       = "European food regulation"
OWNER_KM_USER = USERS["u001"]  # 假設是這份文件所在部門（A）的 KM


def used_ids(user_id):
    r = requests.post(
        f"{RETRIEVE_API}/v1/search",
        json={"query": QUERY, "user_id": user_id, "document_ids": [TARGET_UUID], "top_k": 5},
        timeout=SEARCH_TIMEOUT,
    )
    if r.status_code != 200:
        return None, r.status_code
    data = r.json()
    return set(data.get("document_ids_used", [])), len(data.get("hits", []))


# ── 0. 建立測試用 ACL 狀態 ───────────────────────────────────────
info("PUT /v1/nodes/{id}/acl 建立已知 allow/deny 狀態")
entries = [
    {"subject_type": "department", "subject_id": DEPT_A, "actions": ["browse", "query", "read"], "effect": "allow"},
    {"subject_type": "user", "subject_id": USERS["u002"], "actions": ["browse", "query", "read"], "effect": "deny"},
    {"subject_type": "department", "subject_id": DEPT_B, "actions": ["browse", "query", "read"], "effect": "deny"},
    {"subject_type": "user", "subject_id": USERS["u003"], "actions": ["browse", "query", "read"], "effect": "allow"},
]
r = write_node_acl(TARGET_UUID, OWNER_KM_USER, entries)
if r.status_code != 200:
    fail(f"建立測試 ACL 失敗 → HTTP {r.status_code}  body={r.text[:300]}")
    summary()
ok(f"測試 ACL 已建立 → permission_revision={r.json().get('permission_revision')}")

# (user_key, 描述, 預期可見)
test_cases = [
    ("u001", f"{DEPT_A}，無覆蓋",        True),
    ("u002", f"{DEPT_A}，user=deny",     False),
    ("u003", f"{DEPT_B}，user=allow",    True),
    ("u004", f"{DEPT_B}，無覆蓋",        False),
]

for user_key, label, expect_visible in test_cases:
    user_id = USERS[user_key]
    used, extra = used_ids(user_id)
    if used is None:
        fail(f"{user_key}({label}) → HTTP {extra}")
        continue

    visible = TARGET_UUID in used
    n_hits = extra
    if visible == expect_visible:
        ok(f"{user_key}({label})  可見={visible}  hits={n_hits}")
    else:
        fail(f"{user_key}({label})  可見={visible}，預期={expect_visible}")

# ── 還原：清空 entries，回到純繼承 ───────────────────────────────
info("還原：清空測試 ACL entries")
r = write_node_acl(TARGET_UUID, OWNER_KM_USER, [])
if r.status_code == 200:
    ok("已還原為純繼承")
else:
    fail(f"還原失敗 → HTTP {r.status_code}  body={r.text[:200]}")

summary()
