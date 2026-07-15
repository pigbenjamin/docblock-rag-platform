"""
13 Department Admins（FB-6）
GET/PUT /v1/departments/{department}/admins、GET/PUT /v1/global-admins

前置條件：
  - dev DB 已跑過 scripts/migrate_fb6_department_admins.sql（u001 的
    dept:A:role:KM role row 會回填成 department_admins 的 A 部門管理員；
    2026-07-15 已執行，含事前的 dept-A→A 命名統一）
  - u003 是 B 部門成員、不是 A 部門管理員

測試項目：
  1. GET A 部門管理員名單 → 200 且包含 u001（遷移回填）
  2. GET 不存在的部門 → 404
  3. 非管理員 PUT A 部門名單 → 403
  4. u001 加 u002 進 A 部門管理員 → u002 對 A 部門根資料夾 manage_acl 生效
     （端到端驗證 authz 讀的是 department_admins 表）；還原後 u002 恢復 403
  5. 部門管理員 PUT 空名單 → 400（anti-lockout，只有全域管理員可以清空）
  6. user_ids 帶非 UUID → 400
  7. GET /v1/global-admins → 200；非全域管理員 PUT → 403
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import requests
from config import *

header("13  Department Admins（FB-6）")

U001 = USERS["u001"]
U002 = USERS["u002"]
U003 = USERS["u003"]

ADMINS_URL = f"{DOCUMENT_API}/v1/departments/{DEPT_A}/admins"


def get_admins(user_id):
    return requests.get(ADMINS_URL, headers={"X-User-Id": user_id}, timeout=10)


def put_admins(user_id, user_ids):
    return requests.put(
        ADMINS_URL, headers={"X-User-Id": user_id},
        json={"user_ids": user_ids}, timeout=10,
    )


# ── 1. 名單包含遷移回填的 u001 ───────────────────────────────
info(f"GET /v1/departments/{DEPT_A}/admins")
baseline = None
r = get_admins(U001)
if r.status_code == 200:
    baseline = [a["user_id"] for a in r.json().get("admins", [])]
    if U001 in baseline:
        ok(f"名單包含 u001（回填自 KM role row）：{baseline}")
    else:
        fail(f"名單不含 u001：{baseline}——FB-6 遷移是否已對這個 DB 執行？")
else:
    fail(f"GET admins → HTTP {r.status_code}  body={r.text[:150]}")

# ── 2. 不存在的部門 → 404 ───────────────────────────────────
r = requests.get(
    f"{DOCUMENT_API}/v1/departments/no-such-dept-xyz/admins",
    headers={"X-User-Id": U001}, timeout=10,
)
if r.status_code == 404:
    ok("不存在的部門 → 404")
else:
    fail(f"不存在的部門 → 預期 404，got {r.status_code}")

# ── 3. 非管理員寫入 → 403 ───────────────────────────────────
r = put_admins(U003, [U003])
if r.status_code == 403:
    ok("u003（非 dept-A 管理員）PUT → 403")
else:
    fail(f"u003 PUT → 預期 403，got {r.status_code}  body={r.text[:150]}")

# ── 4. 加管理員 → owner-KM 捷徑端到端生效，還原後失效 ────────
if baseline is not None:
    root_id = find_root_folder_id(U001, DEPT_A)
    if not root_id:
        fail(f"找不到 {DEPT_A} 根資料夾，跳過 owner-KM 端到端驗證")
    else:
        r0 = get_node_acl(root_id, U002)
        if r0.status_code == 403:
            ok("前置：u002 目前對根資料夾無 manage_acl（403）")
        else:
            info(f"前置：u002 GET acl → {r0.status_code}（預期 403，繼續執行）")

        r = put_admins(U001, baseline + [U002])
        if r.status_code == 200:
            ok(f"u001 把 u002 加進 {DEPT_A} 管理員")
            r2 = get_node_acl(root_id, U002)
            if r2.status_code == 200:
                ok("u002 立刻對部門根資料夾有 manage_acl（authz 讀表生效）")
            else:
                fail(f"u002 GET acl → 預期 200，got {r2.status_code}")
        else:
            fail(f"加 u002 → 預期 200，got {r.status_code}  body={r.text[:150]}")

        # 還原
        r = put_admins(U001, baseline)
        if r.status_code == 200 and get_admins(U001).status_code == 200:
            r3 = get_node_acl(root_id, U002)
            if r3.status_code == 403:
                ok("還原名單後 u002 恢復 403")
            else:
                fail(f"還原後 u002 GET acl → 預期 403，got {r3.status_code}")
        else:
            fail(f"還原名單失敗 → HTTP {r.status_code}（請人工檢查 {DEPT_A} 管理員名單！）")

# ── 5. 部門管理員清空名單 → 400 ─────────────────────────────
r = put_admins(U001, [])
if r.status_code == 400:
    ok("部門管理員 PUT 空名單 → 400（anti-lockout）")
elif r.status_code == 200:
    fail("空名單被接受了——立刻手動還原！（部門管理員不該能清空名單）")
    put_admins(U001, baseline or [U001])
else:
    fail(f"空名單 → 預期 400，got {r.status_code}")

# ── 6. 非 UUID → 400 ────────────────────────────────────────
r = put_admins(U001, ["not-a-uuid"])
if r.status_code == 400:
    ok("非 UUID user_id → 400")
else:
    fail(f"非 UUID → 預期 400，got {r.status_code}")

# ── 7. global-admins ────────────────────────────────────────
r = requests.get(f"{DOCUMENT_API}/v1/global-admins", headers={"X-User-Id": U003}, timeout=10)
if r.status_code == 200:
    ok(f"GET /v1/global-admins → 200（{len(r.json().get('admins', []))} 位）")
else:
    fail(f"GET global-admins → 預期 200，got {r.status_code}")

r = requests.put(
    f"{DOCUMENT_API}/v1/global-admins",
    headers={"X-User-Id": U001}, json={"user_ids": [U001]}, timeout=10,
)
if r.status_code == 403:
    ok("非全域管理員 PUT /v1/global-admins → 403")
elif r.status_code == 200:
    fail("u001 竟然是全域管理員？名單已被改動，請人工檢查 global_admins 表！")
else:
    fail(f"PUT global-admins → 預期 403，got {r.status_code}  body={r.text[:150]}")

summary()
