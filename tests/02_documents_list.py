"""
02 Documents List & Get
GET /v1/documents/              — 列出使用者可 browse 的文件（D1：現在要求身分）
GET /v1/documents/{document_id} — 查詢單一文件（同樣要求 browse 權限）
GET /v1/documents/{不存在的 UUID} — 應回傳 404
GET /v1/documents/not-a-uuid    — 應回傳 400
GET /v1/documents/（不帶身分） — 應回傳 401（D1 收緊後，讀取端點不再對外開放）
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import requests
from config import *

header("02  Documents List & Get")

AUTH = {"X-User-Id": USERS["u001"]}

# ── 0. 不帶身分 → 401（D1）─────────────────────────────────────
info("GET /v1/documents/（不帶身分）")
r = requests.get(f"{DOCUMENT_API}/v1/documents/", timeout=10)
if r.status_code == 401:
    ok("不帶身分 → 401（正確，讀取端點已收緊）")
else:
    fail(f"不帶身分 → 預期 401，got HTTP {r.status_code}")

# ── 1. 列出 u001 可 browse 的文件 ────────────────────────────────
info("GET /v1/documents/（u001 身分）")
r = requests.get(f"{DOCUMENT_API}/v1/documents/", headers=AUTH, timeout=10)
if r.status_code != 200:
    fail(f"list → HTTP {r.status_code}")
else:
    docs = r.json()
    document_ids = {d["document_id"] for d in docs}
    ok(f"list → {len(docs)} 份 u001 可見的文件")
    for d in docs:
        info(f"  document_id={d['document_id']}  title={d.get('title')}  version={d['active_version']}")

    # 已知文件是否在清單中取決於 u001 對它們是否有 browse 權限（遷移後的
    # owner_department_id 決定），這裡只記錄觀察值，不強制斷言存在與否。
    for label, document_id in EXISTING_DOCS.items():
        if document_id in document_ids:
            info(f"文件 '{label}' ({document_id}) 在 u001 可見清單中")
        else:
            info(f"文件 '{label}' ({document_id}) 不在 u001 可見清單中（可能 u001 對它沒有 browse，或文件不存在）")

# ── 2. 查詢單一文件（用文件所在部門的成員身分）──────────────────
info("GET /v1/documents/{document_id}（逐一嘗試已知使用者，找出誰能看到）")
for label, document_id in EXISTING_DOCS.items():
    found_with = None
    for user_key in ("u001", "u002", "u003", "u004", "u005"):
        r = requests.get(
            f"{DOCUMENT_API}/v1/documents/{document_id}",
            headers={"X-User-Id": USERS[user_key]},
            timeout=10,
        )
        if r.status_code == 200:
            found_with = user_key
            d = r.json()
            break
    if found_with:
        checks_ok = d.get("document_id") == document_id and d.get("active_version", 0) >= 1
        if checks_ok:
            ok(f"get '{label}' → 可用 {found_with} 身分看到，document_id={d['document_id']}  version={d['active_version']}")
        else:
            fail(f"get '{label}' → 欄位不符，got: {d}")
    else:
        info(f"get '{label}' → 已知測試使用者中沒有人可見（403/404），略過欄位驗證")

# ── 3. 查詢不存在的文件（合法 UUID 但不存在）────────────────────
# 節點不存在時 require_node_action 先回 404，語意跟舊版一致。
NONEXISTENT_UUID = "00000000-0000-0000-0000-000000000000"
info(f"GET /v1/documents/{NONEXISTENT_UUID}")
r = requests.get(f"{DOCUMENT_API}/v1/documents/{NONEXISTENT_UUID}", headers=AUTH, timeout=10)
if r.status_code == 404:
    ok("不存在的 document_id → 正確回傳 404")
else:
    fail(f"不存在的 document_id → 預期 404，got HTTP {r.status_code}")

# ── 4. 查詢非 UUID 格式（預期 400）──────────────────────────────
info("GET /v1/documents/not-a-uuid")
r = requests.get(f"{DOCUMENT_API}/v1/documents/not-a-uuid", headers=AUTH, timeout=10)
if r.status_code == 400:
    ok("非 UUID 格式 → 正確回傳 400")
else:
    fail(f"非 UUID 格式 → 預期 400，got HTTP {r.status_code}")

summary()
