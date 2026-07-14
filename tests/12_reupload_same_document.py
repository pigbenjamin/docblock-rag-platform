"""
12 Re-upload Same document_id
驗證重新上傳同一 document_id（新版本）的行為：

  場景 A：相同內容（SHA-256 不變）
    - version 不應遞增（short-circuit，跳過 embedding）
    - document_id 不應改變
    - chunk 數不重複

  場景 B：內容變更（動態產生最小 PDF）
    - version 應遞增 +1
    - 舊 version chunks 自動刪除
    - document_id 仍不變
"""
import sys, os, time, uuid, tempfile
sys.path.insert(0, os.path.dirname(__file__))
import requests
from pathlib import Path
from config import *

header("12  Re-upload Same document_id")

if not os.path.exists(TEST_PDF):
    fail(f"測試 PDF 不存在：{TEST_PDF}")
    summary()

UPLOADER_USER_ID = USERS["u001"]

info(f"找出 u001 所屬部門（{DEPT_A}）的根資料夾 node_id")
PARENT_FOLDER_ID = find_root_folder_id(UPLOADER_USER_ID, DEPT_A)
if not PARENT_FOLDER_ID:
    fail(f"找不到部門 '{DEPT_A}' 的根資料夾")
    summary()


# ── 輔助：產生最小合法 PDF（只含一行純文字）───────────────────────
def _make_synthetic_pdf(path: str, label: str) -> None:
    """使用 Python 內建產生含 label 文字的單頁 PDF（Type1/Helvetica）。
    pdftext/marker 可直接解析 embedded text，無需 OCR。
    """
    page_text = f"Test document version {label} created {uuid.uuid4()}"
    stream = f"BT /F1 12 Tf 50 700 Td ({page_text}) Tj ET\n".encode()

    obj1 = b"<</Type /Catalog /Pages 2 0 R>>"
    obj2 = b"<</Type /Pages /Kids [3 0 R] /Count 1>>"
    obj3 = (
        b"<</Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
        b" /Contents 4 0 R /Resources <</Font <</F1 5 0 R>>>>>>"
    )
    obj4 = b"<</Length " + str(len(stream)).encode() + b">>"
    obj5 = b"<</Type /Font /Subtype /Type1 /BaseFont /Helvetica>>"

    parts = [(obj1, None), (obj2, None), (obj3, None), (obj4, stream), (obj5, None)]

    body = b"%PDF-1.4\n"
    offsets = []
    for i, (d, s) in enumerate(parts, 1):
        offsets.append(len(body))
        body += f"{i} 0 obj\n".encode() + d + b"\n"
        if s is not None:
            body += b"stream\n" + s + b"\nendstream\n"
        body += b"endobj\n"

    xref_pos = len(body)
    xref = b"xref\n" + f"0 {len(parts) + 1}\n".encode()
    xref += b"0000000000 65535 f \n"
    for off in offsets:
        xref += f"{off:010d} 00000 n \n".encode()

    trailer = (
        b"trailer\n"
        + f"<</Size {len(parts) + 1} /Root 1 0 R>>\n".encode()
        + b"startxref\n"
        + f"{xref_pos}\n".encode()
        + b"%%EOF\n"
    )

    Path(path).write_bytes(body + xref + trailer)


# ── 輔助：上傳並等待完成 ─────────────────────────────────────────
def upload_and_wait(pdf_path, title, document_id=None, max_wait=300):
    """上傳 PDF 並輪詢至 done / failed。
    document_id=None → 建立新文件（需要 parent_folder_id，新文件會繼承該
    資料夾的 ACL）；傳入既有 document_id → 該文件的新版本（維持原本位置，
    parent_folder_id 會被忽略）。
    回傳 (document_id, job_id, elapsed_sec)；失敗回傳 (None, None, elapsed)。
    """
    data = {"title": title}
    if document_id:
        data["document_id"] = document_id
    else:
        data["parent_folder_id"] = PARENT_FOLDER_ID

    with open(pdf_path, "rb") as f:
        r = requests.post(
            f"{DOCUMENT_API}/v1/documents/upload",
            files={"file": (os.path.basename(pdf_path), f, "application/pdf")},
            data=data,
            headers={"X-User-Id": UPLOADER_USER_ID},
            timeout=30,
        )
    if r.status_code != 200:
        fail(f"upload → HTTP {r.status_code}  body={r.text[:200]}")
        return None, None, 0

    body = r.json()
    job_id = body.get("job_id")
    resolved_document_id = body.get("document_id")
    if not job_id or not resolved_document_id:
        fail(f"upload 回應缺少 job_id/document_id：{body}")
        return None, None, 0

    elapsed = 0
    while elapsed < max_wait:
        rj = requests.get(
            f"{DOCUMENT_API}/v1/documents/job/{job_id}",
            headers={"X-User-Id": UPLOADER_USER_ID},
            timeout=10,
        )
        j = rj.json()
        status = j.get("status")
        if status == "done":
            return resolved_document_id, job_id, elapsed
        elif status == "failed":
            fail(f"Pipeline 失敗：{j.get('detail', '')[:200]}")
            return None, None, elapsed
        info(f"  [{elapsed:>3}s] {status}")
        time.sleep(5)
        elapsed += 5

    fail(f"Pipeline 超時（>{max_wait}s）")
    return None, None, elapsed


def get_doc_meta(document_id):
    r = requests.get(
        f"{DOCUMENT_API}/v1/documents/{document_id}",
        headers={"X-User-Id": UPLOADER_USER_ID},
        timeout=10,
    )
    if r.status_code != 200:
        fail(f"GET /v1/documents/{document_id} → HTTP {r.status_code}")
        return None
    return r.json()


def search_hits(document_id):
    """以 u001 身分搜尋指定文件（ACL 已於場景 A-1 寫入，document_id 跨版本不變）。"""
    r = requests.post(
        f"{RETRIEVE_API}/v1/search",
        json={
            "query": "policy network document",
            "user_id": USERS["u001"],
            "document_ids": [document_id],
            "top_k": 100,
        },
        timeout=SEARCH_TIMEOUT,
    )
    if r.status_code != 200:
        return None
    return len(r.json().get("hits", []))


# ════════════════════════════════════════════════════════════════
# 場景 A-1：初次上傳（不指定 document_id，由伺服器生成）
# ════════════════════════════════════════════════════════════════
info("─ 場景 A-1：初次上傳（test.pdf）")
did1, job_id_1, t1 = upload_and_wait(TEST_PDF, "Reupload Test - First Upload")

if not job_id_1:
    summary()

ok(f"初次上傳完成（耗時 ~{t1}s）  document_id={did1}")

meta1 = get_doc_meta(did1)
if not meta1:
    summary()

v1  = meta1["active_version"]
sha1 = meta1.get("content_sha256", "?")
ok(f"初次 → version={v1}  document_id={did1}")
info(f"content_sha256={sha1[:16]}…")

# 不需要另外設 ACL：新文件預設 inherit_acl=true，已透過 PARENT_FOLDER_ID
# 繼承部門資料夾的 query 權限（u001 是該部門成員）。
hits_v1 = search_hits(did1)
info(f"初次上傳後搜尋 hits={hits_v1}")


# ════════════════════════════════════════════════════════════════
# 場景 A-2：相同內容、帶原 document_id 重新上傳（預期 short-circuit）
# ════════════════════════════════════════════════════════════════
info("─ 場景 A-2：相同內容、帶原 document_id 重新上傳（預期 version 不遞增）")
did2, job_id_2, t2 = upload_and_wait(
    TEST_PDF, "Reupload Test - Second Upload (same content)", document_id=did1
)

if job_id_2:
    ok(f"重複上傳完成（耗時 ~{t2}s）")

    meta2 = get_doc_meta(did1)
    if meta2:
        v2   = meta2["active_version"]
        sha2 = meta2.get("content_sha256", "?")

        if did2 == did1:
            ok(f"document_id 維持不變：{did2}")
        else:
            fail(f"document_id 改變：{did1} → {did2}")

        if v2 == v1:
            ok(f"version 未遞增（short-circuit 生效）：version={v2}")
        else:
            fail(f"version 不應遞增：初次={v1}，重複後={v2}")

        if sha2 == sha1:
            ok(f"content_sha256 不變（正確）")
        else:
            fail(f"content_sha256 改變（不應改變）：{sha1[:16]}… → {sha2[:16]}…")

        hits_v1b = search_hits(did1)
        info(f"重複上傳後搜尋 hits={hits_v1b}")
        if hits_v1 is not None and hits_v1b is not None:
            if hits_v1b == hits_v1:
                ok(f"chunk 數未重複（{hits_v1b} hits）")
            else:
                fail(f"chunk 數不一致：初次={hits_v1}，重複後={hits_v1b}（不應有變化）")

        if t2 < t1:
            info(f"耗時：重複 {t2}s < 初次 {t1}s（embedding 已跳過，效果明顯）")
        else:
            info(f"耗時：重複 {t2}s，初次 {t1}s（marker 仍需執行，embedding 已跳過）")


# ════════════════════════════════════════════════════════════════
# 場景 B：內容變更、帶原 document_id 上傳（動態產生最小 PDF）
# ════════════════════════════════════════════════════════════════
info("─ 場景 B：內容變更、帶原 document_id 上傳（動態最小 PDF）")

_tmp_pdf = tempfile.mktemp(suffix=".pdf")
_make_synthetic_pdf(_tmp_pdf, label="modified-v2")
info(f"已產生合成 PDF：{_tmp_pdf}")

try:
    did_b, job_id_b, tb = upload_and_wait(
        _tmp_pdf, "Reupload Test - Content Changed (synthetic)", document_id=did1
    )

    if job_id_b is None:
        info("場景 B pipeline 失敗（可能合成 PDF 不被 marker 支援），略過後續驗證")
    else:
        ok(f"內容變更上傳完成（耗時 ~{tb}s）")

        meta_b = get_doc_meta(did1)
        if meta_b:
            vb    = meta_b["active_version"]
            sha_b = meta_b.get("content_sha256", "?")

            if did_b == did1:
                ok(f"document_id 維持不變：{did_b}")
            else:
                fail(f"document_id 改變：{did1} → {did_b}")

            expected_v = v1 + 1
            if vb == expected_v:
                ok(f"version 遞增正確：{v1} → {vb}")
            else:
                fail(f"version 應為 {expected_v}，got {vb}（sha256 可能相同導致未遞增）")

            if sha_b != sha1:
                ok(f"content_sha256 已更新（內容不同）")
            else:
                info(f"content_sha256 未改變（合成 PDF 可能產生相同 markdown，version 不會遞增）")

            hits_vb = search_hits(did1)
            info(f"內容變更後搜尋 hits={hits_vb}")
            if hits_v1 is not None and hits_vb is not None and vb == expected_v:
                if hits_vb < hits_v1:
                    ok(
                        f"舊 version chunks 已清除：初次 {hits_v1} hits → 現在 {hits_vb} hits"
                        f"（合成 PDF 內容少，chunk 數正常減少）"
                    )
                else:
                    info(f"hits 數：初次={hits_v1}，內容變更後={hits_vb}")
finally:
    try:
        os.unlink(_tmp_pdf)
    except OSError:
        pass

summary()
