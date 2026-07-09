"""
02 Documents List & Get
GET /v1/documents/              — 列出所有文件
GET /v1/documents/{document_id} — 查詢單一文件
GET /v1/documents/{不存在的 UUID} — 應回傳 404
GET /v1/documents/not-a-uuid    — 應回傳 400
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import requests
from config import *

header("02  Documents List & Get")

# ── 1. 列出所有文件 ────────────────────────────────────────────
info("GET /v1/documents/")
r = requests.get(f"{DOCUMENT_API}/v1/documents/", timeout=10)
if r.status_code != 200:
    fail(f"list → HTTP {r.status_code}")
else:
    docs = r.json()
    document_ids = {d["document_id"] for d in docs}
    ok(f"list → {len(docs)} 份文件")
    for d in docs:
        info(f"  document_id={d['document_id']}  title={d.get('title')}  version={d['active_version']}")

    # 確認已知文件都在清單中
    for label, document_id in EXISTING_DOCS.items():
        if document_id in document_ids:
            ok(f"文件 '{label}' ({document_id}) 存在於清單中")
        else:
            fail(f"文件 '{label}' ({document_id}) 不在清單中")

# ── 2. 查詢單一文件 ────────────────────────────────────────────
for label, document_id in EXISTING_DOCS.items():
    info(f"GET /v1/documents/{document_id}")
    r = requests.get(f"{DOCUMENT_API}/v1/documents/{document_id}", timeout=10)
    if r.status_code != 200:
        fail(f"get '{label}' → HTTP {r.status_code}")
        continue
    d = r.json()
    # 驗證關鍵欄位
    checks_ok = (
        d.get("document_id") == document_id and
        d.get("active_version", 0) >= 1
    )
    if checks_ok:
        ok(f"get '{label}' → document_id={d['document_id']}  version={d['active_version']}")
    else:
        fail(f"get '{label}' → 欄位不符，got: {d}")

# ── 3. 查詢不存在的文件（合法 UUID 但不存在，預期 404）──────────
NONEXISTENT_UUID = "00000000-0000-0000-0000-000000000000"
info(f"GET /v1/documents/{NONEXISTENT_UUID}")
r = requests.get(f"{DOCUMENT_API}/v1/documents/{NONEXISTENT_UUID}", timeout=10)
if r.status_code == 404:
    ok("不存在的 document_id → 正確回傳 404")
else:
    fail(f"不存在的 document_id → 預期 404，got HTTP {r.status_code}")

# ── 4. 查詢非 UUID 格式（預期 400）──────────────────────────────
info("GET /v1/documents/not-a-uuid")
r = requests.get(f"{DOCUMENT_API}/v1/documents/not-a-uuid", timeout=10)
if r.status_code == 400:
    ok("非 UUID 格式 → 正確回傳 400")
else:
    fail(f"非 UUID 格式 → 預期 400，got HTTP {r.status_code}")

summary()
