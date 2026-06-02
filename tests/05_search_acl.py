"""
05 ACL Search — 多用戶存取層級驗證
對 test-eurfood 的搜尋，驗證各用戶實際拿到的 access 符合 DB 中的 ACL 設定：

  test-eurfood ACL:
    dept-A      = detail
    dept-B      = summary
    user u002   = deny   （dept-A 用戶，user 規則覆蓋）
    user u003   = detail （dept-B 用戶，user 規則覆蓋）
    user u005   = summary（dept-C，user 規則，無 dept 規則）

預期結果：
  u001 (dept-A)            → detail，有 hits
  u002 (dept-A, user deny) → deny，無 hits
  u003 (dept-B, user detail)→ detail，有 hits
  u004 (dept-B)            → summary，hits 來自 summary_chunks
  u005 (dept-C, user sum.) → summary
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import requests
from config import *

header("05  ACL Search — 多用戶存取層級驗證")

TARGET_DOC = "test-eurfood"
QUERY      = "European food regulation"

# (user_key, 描述, 預期 access, 是否預期有 hits)
test_cases = [
    ("u001", "dept-A，無覆蓋",        "detail",  True),
    ("u002", "dept-A，user=deny",     "deny",    False),
    ("u003", "dept-B，user=detail",   "detail",  True),
    ("u004", "dept-B，無覆蓋",        "summary", None),   # summary：視 summary_chunks 是否有資料
    ("u005", "dept-C，user=summary",  "summary", None),
]

for user_key, label, expected_access, expect_hits in test_cases:
    user_id = USERS[user_key]
    r = requests.post(
        f"{RETRIEVE_API}/v1/search",
        json={
            "query":   QUERY,
            "user_id": user_id,
            "doc_ids": [TARGET_DOC],
            "top_k":   5,
        },
        timeout=20,
    )
    if r.status_code != 200:
        fail(f"{user_key}({label}) → HTTP {r.status_code}")
        continue

    data    = r.json()
    access  = data.get("access", {}).get(TARGET_DOC, "unknown")
    hits    = data.get("hits", [])
    n_hits  = len(hits)
    sources = list({h["source"] for h in hits})

    if access != expected_access:
        fail(f"{user_key}({label})  access={access}  預期={expected_access}")
        continue

    # hits 數量判斷
    if expect_hits is True and n_hits == 0:
        fail(f"{user_key}({label})  access={access} 正確，但 hits=0（預期有結果）")
    elif expect_hits is False and n_hits > 0:
        fail(f"{user_key}({label})  access={access} 正確，但 hits={n_hits}（預期無結果）")
    else:
        ok(f"{user_key}({label[:20]})  access={access}  hits={n_hits}  sources={sources}")

    # detail access 的結果不應包含 summary source
    if access == "detail" and any("summary" in s for s in sources):
        fail(f"{user_key}  detail access 不應有 summary source，got: {sources}")

    # deny access 應無 hits
    if access == "deny" and n_hits > 0:
        fail(f"{user_key}  deny access 不應有 hits，got: {n_hits}")

summary()
