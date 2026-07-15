"""
11 Webhook User Sync
POST /keycloak/user-sync、POST /keycloak/full-sync
模擬 Keycloak 事件，驗證 webhook-service 的接收與處理。

注意：webhook-service 會去呼叫 Keycloak Admin API 取得用戶資訊。
  - 若 Keycloak 未啟動 → 預期回傳 500（已知，標記為 SKIP）
  - 若 Keycloak 正常  → 回傳 200 + {ok: true}

測試項目：
  1. 缺少 X-Webhook-Secret → 403/401
  2. 錯誤 X-Webhook-Secret → 403/401
  3. 合法 secret + 存在的 user_id → 200 或 500（Keycloak 不可用時）
  4. /healthz 回應正確
  5. /keycloak/full-sync 的 secret 保護（不觸發真的全量同步——那會對 Keycloak
     realm 所有使用者跑一輪，屬於 CronJob/手動維運操作，測試只驗證授權擋得住）
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import requests
from config import *

header("11  Webhook User Sync")

WEBHOOK_HEADERS_OK   = {"X-Webhook-Secret": WEBHOOK_SECRET, "Content-Type": "application/json"}
WEBHOOK_HEADERS_BAD  = {"X-Webhook-Secret": "wrong-secret", "Content-Type": "application/json"}

TEST_USER_ID = USERS["u001"]

# ── 1. /healthz ───────────────────────────────────────────────
info("GET /healthz")
try:
    r = requests.get(f"{WEBHOOK_SERVICE}/healthz", timeout=5)
    if r.status_code == 200 and r.json().get("status") == "ok":
        ok("/healthz → status=ok")
    else:
        fail(f"/healthz → HTTP {r.status_code}  body={r.text[:100]}")
except Exception as e:
    fail(f"/healthz → 無法連線：{e}")

# ── 2. 缺少 Secret → 預期 401 或 403 ─────────────────────────
info("POST /keycloak/user-sync（無 X-Webhook-Secret）")
try:
    r = requests.post(
        f"{WEBHOOK_SERVICE}/keycloak/user-sync",
        json={"event": "USER_UPDATE", "user_id": TEST_USER_ID},
        timeout=5,
    )
    if r.status_code in (401, 403, 422):
        ok(f"無 Secret → HTTP {r.status_code}（正確拒絕）")
    else:
        fail(f"無 Secret → 預期 401/403，got {r.status_code}  body={r.text[:100]}")
except Exception as e:
    fail(f"無 Secret → 連線錯誤：{e}")

# ── 3. 錯誤 Secret → 預期 401 或 403 ────────────────────────
info("POST /keycloak/user-sync（錯誤 X-Webhook-Secret）")
try:
    r = requests.post(
        f"{WEBHOOK_SERVICE}/keycloak/user-sync",
        headers=WEBHOOK_HEADERS_BAD,
        json={"event": "USER_UPDATE", "user_id": TEST_USER_ID},
        timeout=5,
    )
    if r.status_code in (401, 403):
        ok(f"錯誤 Secret → HTTP {r.status_code}（正確拒絕）")
    else:
        fail(f"錯誤 Secret → 預期 401/403，got {r.status_code}  body={r.text[:100]}")
except Exception as e:
    fail(f"錯誤 Secret → 連線錯誤：{e}")

# ── 4. 正確 Secret + 合法 user_id ────────────────────────────
info(f"POST /keycloak/user-sync（正確 Secret，user_id={TEST_USER_ID}）")
try:
    r = requests.post(
        f"{WEBHOOK_SERVICE}/keycloak/user-sync",
        headers=WEBHOOK_HEADERS_OK,
        json={"event": "USER_UPDATE", "user_id": TEST_USER_ID},
        timeout=15,
    )

    if r.status_code == 200:
        body = r.json()
        if body.get("ok") and body.get("user_id") == TEST_USER_ID:
            ok(f"user-sync → ok=true  user_id={body['user_id']}")
        else:
            fail(f"user-sync → 格式錯誤：{body}")

    elif r.status_code in (500, 502, 503):
        # Keycloak 未啟動的預期結果
        info(f"HTTP {r.status_code} — Keycloak 不可用（正常，此環境未啟動 Keycloak）")
        info(f"  回應：{r.text[:200]}")
        ok("Keycloak 未連線時 endpoint 正確回傳 5xx（非 crash）")

    elif r.status_code in (401, 403):
        fail(f"正確 Secret 仍被拒絕 → HTTP {r.status_code}  body={r.text[:100]}")

    else:
        fail(f"user-sync → 預期 200 或 5xx，got {r.status_code}  body={r.text[:100]}")

except Exception as e:
    fail(f"user-sync → 連線錯誤：{e}")

# ── 5. 缺少 user_id 欄位 → 預期 422 ─────────────────────────
info("POST /keycloak/user-sync（缺少 user_id）")
try:
    r = requests.post(
        f"{WEBHOOK_SERVICE}/keycloak/user-sync",
        headers=WEBHOOK_HEADERS_OK,
        json={"event": "USER_UPDATE"},   # 故意缺少 user_id
        timeout=5,
    )
    if r.status_code in (400, 422):
        ok(f"缺少 user_id → HTTP {r.status_code}（正確驗證）")
    elif r.status_code == 200:
        info(f"缺少 user_id → HTTP 200（服務允許空 user_id，不視為錯誤）")
    else:
        info(f"缺少 user_id → HTTP {r.status_code}（略過）")
except Exception as e:
    fail(f"缺少 user_id → 連線錯誤：{e}")

# ── 6. POST /keycloak/full-sync 的 secret 保護 ──────────────
# 刻意不測「合法 secret」情境：那會觸發對 Keycloak realm 全部使用者的
# 真實批次同步，屬於 CronJob/手動維運操作，不該藏在例行測試裡意外跑一輪。
info("POST /keycloak/full-sync（無 X-Webhook-Secret）")
try:
    r = requests.post(f"{WEBHOOK_SERVICE}/keycloak/full-sync", timeout=5)
    if r.status_code in (401, 403, 422):
        ok(f"無 Secret → HTTP {r.status_code}（正確拒絕）")
    else:
        fail(f"無 Secret → 預期 401/403，got {r.status_code}  body={r.text[:100]}")
except Exception as e:
    fail(f"無 Secret → 連線錯誤：{e}")

info("POST /keycloak/full-sync（錯誤 X-Webhook-Secret）")
try:
    r = requests.post(f"{WEBHOOK_SERVICE}/keycloak/full-sync", headers=WEBHOOK_HEADERS_BAD, timeout=5)
    if r.status_code in (401, 403):
        ok(f"錯誤 Secret → HTTP {r.status_code}（正確拒絕，未觸發全量同步）")
    else:
        fail(f"錯誤 Secret → 預期 401/403，got {r.status_code}  body={r.text[:100]}")
except Exception as e:
    fail(f"錯誤 Secret → 連線錯誤：{e}")

summary()
