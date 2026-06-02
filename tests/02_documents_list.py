"""
02 Documents List & Get
GET /v1/documents/       — 列出所有文件
GET /v1/documents/{doc_id} — 查詢單一文件
GET /v1/documents/nonexistent — 應回傳 404
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import requests
from config import *

header("02  Documents List & Get")

# ── 1. 列出所有文件 ────────────────────────────────────────────
info("GET /v1/documents/")
r = requests.get(f"{ADMIN_API}/v1/documents/", timeout=10)
if r.status_code != 200:
    fail(f"list → HTTP {r.status_code}")
else:
    docs = r.json()
    doc_ids = {d["doc_id"] for d in docs}
    ok(f"list → {len(docs)} 份文件")
    for d in docs:
        info(f"  doc_id={d['doc_id']}  version={d['active_version']}  document_id={d['document_id']}")

    # 確認已知文件都在清單中
    for expected_id in EXISTING_DOCS:
        if expected_id in doc_ids:
            ok(f"文件 '{expected_id}' 存在於清單中")
        else:
            fail(f"文件 '{expected_id}' 不在清單中")

# ── 2. 查詢單一文件 ────────────────────────────────────────────
for doc_id, document_id in EXISTING_DOCS.items():
    info(f"GET /v1/documents/{doc_id}")
    r = requests.get(f"{ADMIN_API}/v1/documents/{doc_id}", timeout=10)
    if r.status_code != 200:
        fail(f"get '{doc_id}' → HTTP {r.status_code}")
        continue
    d = r.json()
    # 驗證關鍵欄位
    checks_ok = (
        d.get("doc_id") == doc_id and
        d.get("document_id") == document_id and
        d.get("active_version", 0) >= 1
    )
    if checks_ok:
        ok(f"get '{doc_id}' → document_id={d['document_id']}  version={d['active_version']}")
    else:
        fail(f"get '{doc_id}' → 欄位不符，got: {d}")

# ── 3. 查詢不存在的文件（預期 404）────────────────────────────
info("GET /v1/documents/nonexistent-doc-id-xyz")
r = requests.get(f"{ADMIN_API}/v1/documents/nonexistent-doc-id-xyz", timeout=10)
if r.status_code == 404:
    ok("不存在文件 → 正確回傳 404")
else:
    fail(f"不存在文件 → 預期 404，got HTTP {r.status_code}")

summary()
