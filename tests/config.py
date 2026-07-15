"""
共用測試設定。
所有測試腳本從此 import，只需改這一個地方即可切換環境。

環境選擇：
  本地 docker-compose：預設值（localhost）
  k8s（10.90.20.55）：設定 TEST_ENV=k8s 或手動帶 env var
"""
import os
import sys

import requests

_NODE = "10.90.20.55"
_K8S  = os.getenv("TEST_ENV", "").lower() == "k8s"

def _url(local_port: int, node_port: int) -> str:
    if _K8S:
        return f"http://{_NODE}:{node_port}"
    return os.getenv(f"_UNUSED_{local_port}", f"http://localhost:{local_port}")

# ── 服務 URL ─────────────────────────────────────────────────
RETRIEVE_API    = os.getenv("RETRIEVE_API",    f"http://{_NODE}:31761" if _K8S else "http://localhost:8761")
DOCUMENT_API    = os.getenv("DOCUMENT_API",    f"http://{_NODE}:31765" if _K8S else "http://localhost:8765")
INGEST_WORKER   = os.getenv("INGEST_WORKER",   f"http://{_NODE}:31762" if _K8S else "http://localhost:8762")
WEBHOOK_SERVICE = os.getenv("WEBHOOK_SERVICE", f"http://{_NODE}:31763" if _K8S else "http://localhost:8763")
LITELLM_PROXY   = os.getenv("LITELLM_PROXY",   f"http://{_NODE}:30400")

# ── 密鑰 ─────────────────────────────────────────────────────
# ACL_ADMIN_SECRET 只給 DELETE /v1/documents/{id} 的 X-Acl-Secret bypass 用
# （node ACL 端點 PUT/GET /v1/nodes/{id}/acl 沒有 admin-secret bypass，一律要
# 真實使用者身分且通過 manage_acl 檢查——見 FB-5，node ACL 路由掛在
# get_current_user_id，不像舊版 acl.py 支援 get_current_user_id_or_admin_secret）
ACL_ADMIN_SECRET = os.getenv("ACL_ADMIN_SECRET", "acl-admin-secret-changeme" if _K8S else "dev-secret-change-me")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET",   "dev-webhook-secret")

# ── Timeout ─────────────────────────────────────────────────
TIMEOUT_SCALE  = 1                   # 統一倍數
SEARCH_TIMEOUT = 120                 # 搜尋（含 embed + routing）
ACL_TIMEOUT    = 120                 # ACL 搜尋驗證
RAG_TIMEOUT    = 300                 # RAG 問答生成

# ── 租戶 ─────────────────────────────────────────────────────
TENANT_ID = "firdi"

# ── 現有測試文件（已在 DB 中）──────────────────────────────────
# 2026-07-15 對 live k8s/dev DB 跑 02 確認：FB-1 遷移前的這兩個 document_id
# 在 nodes 裡已經不存在了（GET/PUT 回 404，不是權限問題），不是遷移遺漏
# ——遷移只會新增 node，不會刪除既有的，代表這兩筆本來就沒有被遷移到
# （dev DB 在更早的階段就已經不含它們）。已用 u001 重新上傳兩份同名替代
# fixture 到 dept-A 資料夾下，document_id 換成新的，其餘假設不變
# （u001 = dept-A 的 owner-KM，對這兩份文件有 manage_acl）。
EXISTING_DOCS = {
    "test-eurfood": "506eb46e-9aaa-4a1f-9b53-830a5d48497a",
    "deptA_IT-OT_Network_Policy": "8451b5b1-b62e-459a-9a92-5b2532654957",
}

# ── 測試用戶（已在 user_principal 中）────────────────────────
#   u001: A 部門（假設也是該部門 KM，見下方 DEPT_A 的用法）
#   u002: A 部門
#   u003: B 部門
#   u004: B 部門
#   u005: C 部門
USERS = {
    "u001": "11111111-0001-0001-0001-000000000001",
    "u002": "11111111-0001-0001-0001-000000000002",
    "u003": "11111111-0001-0001-0001-000000000003",
    "u004": "11111111-0001-0001-0001-000000000004",
    "u005": "11111111-0001-0001-0001-000000000005",
}

# ── 部門名稱 ────────────────────────────────────────────────
# 2026-07-15（FB-6 前置）：dev DB 的部門命名已統一成 Keycloak 頂層群組
# 原始名稱 "A"/"B"/"C"（原本 fixture 用舊版帶前綴的 "dept-A"，與 Keycloak
# 命名並存；FB-6 遷移前已把 user_principal/nodes/acl_entries 全部改名，
# webhook 同步進來的真實資料會無縫併入同一套資料夾）。
#
# u002-u005 的 department principal 缺口已補齊（dev DB user_principal 直接
# 補資料）：u001/u002=A、u003/u004=B、u005=C。04/05/06 現在是真的在驗證
# department-level allow/deny，不是靠 default-deny 巧合通過。
DEPT_A = os.getenv("TEST_DEPT_A", "A")
DEPT_B = os.getenv("TEST_DEPT_B", "B")

# ── 測試 PDF（用於上傳測試）────────────────────────────────────
# fixtures/test.pdf 是從 ingest-worker volume 複製出來的真實 PDF
TEST_PDF = os.path.join(os.path.dirname(__file__), "fixtures", "test.pdf")

# ── Container 內路徑（用於 ingest-worker 分階段測試）─────────
CONTAINER_PDF      = "/data/uploads/104fa00d-4609-4368-a1f8-e9edd35bab9b/deptA_IT-OT_Network_Policy.pdf"
CONTAINER_WORK_DIR = "/data/uploads/104fa00d-4609-4368-a1f8-e9edd35bab9b"


# ── node / ACL 輔助（FB-3/FB-5 之後的新 API，取代舊版 /v1/acl/*）────
def find_root_folder_id(user_id: str, department: str):
    """用 GET /v1/nodes（根目錄）找部門根資料夾的 node_id。FB-1 遷移後每個
    部門會有一個以部門名稱命名的根資料夾。找不到回傳 None。"""
    r = requests.get(f"{DOCUMENT_API}/v1/nodes", headers={"X-User-Id": user_id}, timeout=10)
    if r.status_code != 200:
        return None
    for item in r.json().get("items", []):
        if item.get("node_type") == "folder" and item.get("name") == department:
            return item.get("node_id")
    return None


def write_node_acl(node_id: str, user_id: str, entries: list, if_match: str = None):
    """PUT /v1/nodes/{node_id}/acl（取代舊版 POST /v1/acl/write-map）。

    entries 範例：
      [{"subject_type": "user", "subject_id": "...",
        "actions": ["browse", "query", "read"], "effect": "allow"}]

    呼叫者需要對該節點有 manage_acl 權限（部門 owner 的 KM 自動符合，不需要
    額外設定；舊版的 X-Acl-Secret admin bypass 在這支端點上不存在）。
    這是「整批取代」語意：一次呼叫會覆蓋節點目前所有的 entries。
    """
    headers = {"X-User-Id": user_id}
    if if_match:
        headers["If-Match"] = if_match
    return requests.put(
        f"{DOCUMENT_API}/v1/nodes/{node_id}/acl",
        headers=headers,
        json={"entries": entries},
        timeout=ACL_TIMEOUT,
    )


def get_node_acl(node_id: str, user_id: str):
    return requests.get(
        f"{DOCUMENT_API}/v1/nodes/{node_id}/acl",
        headers={"X-User-Id": user_id},
        timeout=10,
    )


# ── 輸出工具 ────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def ok(msg):    print(f"  {GREEN}[OK]{RESET}  {msg}")
def fail(msg):  print(f"  {RED}[FAIL]{RESET} {msg}"); _failures.append(msg)
def info(msg):  print(f"  {YELLOW}[--]{RESET}  {msg}")
def header(title):
    print(f"\n{BOLD}{'='*55}{RESET}")
    print(f"{BOLD}  {title}{RESET}")
    print(f"{BOLD}{'='*55}{RESET}")

_failures = []

def summary():
    print(f"\n{BOLD}{'─'*55}{RESET}")
    if _failures:
        print(f"{RED}{BOLD}  FAILED: {len(_failures)} check(s){RESET}")
        for f in _failures:
            print(f"    • {f}")
        sys.exit(1)
    else:
        print(f"{GREEN}{BOLD}  All checks passed ✓{RESET}")
