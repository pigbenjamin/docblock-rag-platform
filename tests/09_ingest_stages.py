"""
09 Ingest Worker 分階段執行
分別呼叫三個獨立端點，驗證每個階段都能單獨成功：

  POST /jobs/marker       — PDF → Markdown
  POST /jobs/build-chunks — Markdown → chunk_block.json
  POST /jobs/ingest       — chunk_block.json → PostgreSQL

使用 container 內現有的 PDF（CONTAINER_PDF），
以新的 doc_id 避免覆蓋現有資料。
"""
import sys, os, time, uuid
sys.path.insert(0, os.path.dirname(__file__))
import requests
from config import *

header("09  Ingest Worker 分階段執行")

STAGE_DOC_ID = f"stage-test-{uuid.uuid4().hex[:6]}"
info(f"使用 doc_id={STAGE_DOC_ID!r}")
info(f"PDF 路徑（container 內）：{CONTAINER_PDF}")

def wait_job(job_id, label, timeout=300):
    """輪詢 job 直到 done / failed，回傳最終 status。"""
    elapsed = 0
    while elapsed < timeout:
        r = requests.get(f"{INGEST_WORKER}/jobs/{job_id}", timeout=10)
        if r.status_code != 200:
            return None, f"poll HTTP {r.status_code}"
        j = r.json()
        s = j.get("status")
        if s in ("done", "failed"):
            return s, j.get("detail", "")
        time.sleep(5)
        elapsed += 5
        info(f"  [{label}] {elapsed:>3}s  status={s}")
    return None, "timeout"

# ─────────────────────────────────────────────────────────────
# Stage 1：marker（PDF → Markdown）
# ─────────────────────────────────────────────────────────────
info("─ Stage 1：/jobs/marker")
job_id_1 = f"marker-{uuid.uuid4().hex[:8]}"
r = requests.post(
    f"{INGEST_WORKER}/jobs/marker",
    json={
        "job_id":     job_id_1,
        "pdf_path":   CONTAINER_PDF,
        "output_dir": CONTAINER_WORK_DIR,
    },
    timeout=15,
)
if r.status_code != 200:
    fail(f"提交 marker job → HTTP {r.status_code}")
else:
    ok(f"marker job 已提交  job_id={job_id_1}")
    status, detail = wait_job(job_id_1, "marker", timeout=300)
    if status == "done":
        ok(f"Stage 1 marker 完成")
    else:
        fail(f"Stage 1 marker {status}：{detail[:200]}")

# marker 輸出的 Markdown 路徑（marker 依 pdf stem 命名目錄）
import os.path as osp
pdf_stem = osp.splitext(osp.basename(CONTAINER_PDF))[0]
MD_PATH = f"{CONTAINER_WORK_DIR}/{pdf_stem}/raw.md"
OUT_JSON = f"{CONTAINER_WORK_DIR}/{STAGE_DOC_ID}.chunk_block.json"
info(f"預期 md_path={MD_PATH}")
info(f"out_json={OUT_JSON}")

# ─────────────────────────────────────────────────────────────
# Stage 2：build-chunks（Markdown → chunk_block.json）
# ─────────────────────────────────────────────────────────────
info("─ Stage 2：/jobs/build-chunks")
job_id_2 = f"build-{uuid.uuid4().hex[:8]}"
r = requests.post(
    f"{INGEST_WORKER}/jobs/build-chunks",
    json={
        "job_id":      job_id_2,
        "fixed_md":    MD_PATH,
        "out_json":    OUT_JSON,
        "doc_id":      STAGE_DOC_ID,
        "source_path": "stage-test/deptA_IT-OT_Network_Policy.pdf",
    },
    timeout=15,
)
if r.status_code != 200:
    fail(f"提交 build-chunks job → HTTP {r.status_code}  body={r.text[:200]}")
else:
    ok(f"build-chunks job 已提交  job_id={job_id_2}")
    status, detail = wait_job(job_id_2, "build-chunks", timeout=300)
    if status == "done":
        ok("Stage 2 build-chunks 完成")
    else:
        fail(f"Stage 2 build-chunks {status}：{detail[:300]}")

# ─────────────────────────────────────────────────────────────
# Stage 3：ingest（chunk_block.json → PostgreSQL）
# ─────────────────────────────────────────────────────────────
info("─ Stage 3：/jobs/ingest")
job_id_3 = f"ingest-{uuid.uuid4().hex[:8]}"
r = requests.post(
    f"{INGEST_WORKER}/jobs/ingest",
    json={
        "job_id":           job_id_3,
        "chunk_block_json": OUT_JSON,
    },
    timeout=15,
)
if r.status_code != 200:
    fail(f"提交 ingest job → HTTP {r.status_code}  body={r.text[:200]}")
else:
    ok(f"ingest job 已提交  job_id={job_id_3}")
    status, detail = wait_job(job_id_3, "ingest", timeout=180)
    if status == "done":
        ok("Stage 3 ingest 完成")
    else:
        fail(f"Stage 3 ingest {status}：{detail[:300]}")

# ─────────────────────────────────────────────────────────────
# 驗證：文件已進入 DB
# ─────────────────────────────────────────────────────────────
info(f"驗證文件 {STAGE_DOC_ID!r} 出現於 GET /v1/documents/")
r = requests.get(f"{ADMIN_API}/v1/documents/{STAGE_DOC_ID}", timeout=10)
if r.status_code == 200:
    d = r.json()
    ok(f"文件已建立  document_id={d['document_id']}  version={d['active_version']}")
else:
    fail(f"文件未找到 → HTTP {r.status_code}（可能 ingest 失敗）")

summary()
