"""
13  Marker Service + LiteLLM Proxy
驗證 marker-service 與 litellm-proxy 服務是否正常運作。

測試項目：
  1. marker-service  /healthz            — 服務存活
  2. litellm-proxy   /health/liveliness  — proxy 存活
  3. litellm-proxy   model list          — marker/pdf-to-md 路由已載入
  4. marker-service  POST /v1/convert    — 直接轉換（同步，需等待 marker 完成）
  5. litellm-proxy   POST /v1/chat/completions — 透過 proxy 路由到 marker-service

注意：測試 4、5 會實際執行 marker，耗時可能長達數分鐘。
      若不想跑轉換，可設環境變數 SKIP_CONVERT=1 跳過。
"""
import sys, os, json, time, uuid
sys.path.insert(0, os.path.dirname(__file__))
import requests
from config import *

SKIP_CONVERT = os.getenv("SKIP_CONVERT", "0").strip() in ("1", "true", "yes")
MARKER_TIMEOUT = int(os.getenv("MARKER_TIMEOUT", "1800"))

header("13  Marker Service + LiteLLM Proxy")

# ─────────────────────────────────────────────────────────────────
# 1. marker-service /healthz
# ─────────────────────────────────────────────────────────────────
info("─ 1. marker-service /healthz")
if os.getenv("TEST_ENV", "").lower() == "k8s":
    info("k8s 模式：marker-service 無 NodePort，跳過直連測試（改由 litellm-service 路由驗證）")
else:
    try:
        r = requests.get(f"{MARKER_SERVICE}/healthz", timeout=5)
        body = r.json()
        if r.status_code == 200 and body.get("status") == "ok":
            ok(f"marker-service 存活  →  {body}")
        else:
            fail(f"marker-service /healthz → HTTP {r.status_code}  body={body}")
    except Exception as e:
        fail(f"marker-service /healthz → {e}")


# ─────────────────────────────────────────────────────────────────
# 2. litellm-proxy /health/liveliness
# ─────────────────────────────────────────────────────────────────
info("─ 2. litellm-proxy /health/liveliness")
try:
    r = requests.get(f"{LITELLM_PROXY}/health/liveliness", timeout=5)
    if r.status_code == 200:
        ok(f"litellm-proxy 存活  →  {r.text[:80]}")
    else:
        fail(f"litellm-proxy /health/liveliness → HTTP {r.status_code}")
except Exception as e:
    fail(f"litellm-proxy /health/liveliness → {e}")


# ─────────────────────────────────────────────────────────────────
# 3. litellm-proxy model list — 確認 marker/pdf-to-md 已載入
# ─────────────────────────────────────────────────────────────────
info("─ 3. litellm-proxy model list")
LITELLM_API_KEY = os.getenv("LITELLM_API_KEY", "sk-litellm-internal")
try:
    r = requests.get(
        f"{LITELLM_PROXY}/v1/models",
        headers={"Authorization": f"Bearer {LITELLM_API_KEY}"},
        timeout=10,
    )
    if r.status_code == 200:
        models = [m["id"] for m in r.json().get("data", [])]
        if "marker/pdf-to-md" in models:
            ok(f"marker/pdf-to-md 路由已載入  models={models}")
        else:
            fail(f"marker/pdf-to-md 不在 model list 中  models={models}")
    else:
        fail(f"GET /v1/models → HTTP {r.status_code}  body={r.text[:200]}")
except Exception as e:
    fail(f"GET /v1/models → {e}")


# ─────────────────────────────────────────────────────────────────
# 4. marker-service POST /v1/convert（直接呼叫）
# ─────────────────────────────────────────────────────────────────
info("─ 4. marker-service POST /v1/convert（直接）")
if os.getenv("TEST_ENV", "").lower() == "k8s":
    info("k8s 模式：marker-service 無 NodePort，跳過直連測試")
elif SKIP_CONVERT:
    info("SKIP_CONVERT=1，跳過實際轉換測試")
else:
    convert_doc_id = f"ms-test-{uuid.uuid4().hex[:6]}"
    convert_out_dir = f"/data/test-runs/{convert_doc_id}"
    info(f"doc_id={convert_doc_id!r}  out_dir={convert_out_dir!r}")
    info(f"PDF={CONTAINER_PDF}  （等待最多 {MARKER_TIMEOUT}s）")
    try:
        t0 = time.perf_counter()
        r = requests.post(
            f"{MARKER_SERVICE}/v1/convert",
            json={
                "pdf_path": CONTAINER_PDF,
                "doc_id":   convert_doc_id,
                "out_dir":  convert_out_dir,
                "job_id":   f"test-direct-{convert_doc_id}",
            },
            timeout=MARKER_TIMEOUT + 30,
        )
        elapsed = time.perf_counter() - t0
        if r.status_code == 200:
            data = r.json()
            ok(f"轉換完成  md_path={data['md_path']}  elapsed={elapsed:.1f}s")
        else:
            fail(f"POST /v1/convert → HTTP {r.status_code}  body={r.text[:300]}")
    except requests.exceptions.Timeout:
        fail(f"POST /v1/convert 超時（>{MARKER_TIMEOUT}s）")
    except Exception as e:
        fail(f"POST /v1/convert → {e}")


# ─────────────────────────────────────────────────────────────────
# 5. litellm-proxy POST /v1/chat/completions（透過 proxy 路由）
# ─────────────────────────────────────────────────────────────────
info("─ 5. litellm-proxy POST /v1/chat/completions → marker/pdf-to-md")
if SKIP_CONVERT:
    info("SKIP_CONVERT=1，跳過實際轉換測試")
else:
    proxy_doc_id  = f"proxy-test-{uuid.uuid4().hex[:6]}"
    proxy_out_dir = f"/data/test-runs/{proxy_doc_id}"
    info(f"doc_id={proxy_doc_id!r}  out_dir={proxy_out_dir!r}")
    info(f"PDF={CONTAINER_PDF}  （等待最多 {MARKER_TIMEOUT}s）")
    try:
        t0 = time.perf_counter()
        r = requests.post(
            f"{LITELLM_PROXY}/v1/chat/completions",
            headers={"Authorization": f"Bearer {LITELLM_API_KEY}"},
            json={
                "model": "marker/pdf-to-md",
                "messages": [{
                    "role": "user",
                    "content": json.dumps({
                        "pdf_path": CONTAINER_PDF,
                        "doc_id":   proxy_doc_id,
                        "out_dir":  proxy_out_dir,
                        "job_id":   f"test-proxy-{proxy_doc_id}",
                    }),
                }],
            },
            timeout=MARKER_TIMEOUT + 60,
        )
        elapsed = time.perf_counter() - t0
        if r.status_code == 200:
            data = r.json()
            md_path = data["choices"][0]["message"]["content"]
            ok(f"proxy 路由完成  md_path={md_path}  elapsed={elapsed:.1f}s")
        else:
            fail(f"POST /v1/chat/completions → HTTP {r.status_code}  body={r.text[:300]}")
    except requests.exceptions.Timeout:
        fail(f"litellm-proxy 路由超時（>{MARKER_TIMEOUT}s）")
    except Exception as e:
        fail(f"litellm-proxy POST → {e}")


summary()
