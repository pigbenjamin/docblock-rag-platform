"""
共用測試設定。
所有測試腳本從此 import，只需改這一個地方即可切換環境。
"""
import os
import sys

# ── 服務 URL ─────────────────────────────────────────────────
RETRIEVE_API    = os.getenv("RETRIEVE_API",    "http://localhost:8761")
ADMIN_API       = os.getenv("ADMIN_API",       "http://localhost:8765")
INGEST_WORKER   = os.getenv("INGEST_WORKER",   "http://localhost:8762")
WEBHOOK_SERVICE = os.getenv("WEBHOOK_SERVICE", "http://localhost:8763")
MARKER_SERVICE  = os.getenv("MARKER_SERVICE",  "http://localhost:8766")
LITELLM_PROXY   = os.getenv("LITELLM_PROXY",   "http://localhost:4000")
NOSTR_PROXY     = os.getenv("NOSTR_PROXY",     "http://localhost:8800")

# ── 密鑰 ─────────────────────────────────────────────────────
ACL_ADMIN_SECRET = os.getenv("ACL_ADMIN_SECRET", "dev-secret-change-me")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET",   "dev-webhook-secret")

# ── 租戶 ─────────────────────────────────────────────────────
TENANT_ID = "firdi"

# ── 現有測試文件（已在 DB 中）──────────────────────────────────
EXISTING_DOCS = {
    "test-eurfood": "aaaaaaaa-0001-0001-0001-000000000001",
    "deptA_IT-OT_Network_Policy": "bd84084f-2ef1-4a6d-b77e-200978dfc5b2",
}

# ── 測試用戶（已在 user_principal 中）────────────────────────
#   u001: dept-A
#   u002: dept-A，但對 test-eurfood 有 user deny 覆蓋
#   u003: dept-B，但對 test-eurfood 有 user detail 覆蓋
#   u004: dept-B（無 user 覆蓋）
#   u005: dept-C，對 test-eurfood 有 user summary 覆蓋
USERS = {
    "u001": "11111111-0001-0001-0001-000000000001",
    "u002": "11111111-0001-0001-0001-000000000002",
    "u003": "11111111-0001-0001-0001-000000000003",
    "u004": "11111111-0001-0001-0001-000000000004",
    "u005": "11111111-0001-0001-0001-000000000005",
}

# test-eurfood 上的 ACL 規則（測試 05、06 的預期依據）
EURFOOD_ACL = {
    USERS["u001"]: "detail",    # dept-A=detail，無 user 覆蓋
    USERS["u002"]: "deny",      # dept-A=detail，但 user=deny 優先
    USERS["u003"]: "detail",    # dept-B=summary，但 user=detail 優先
    USERS["u004"]: "summary",   # dept-B=summary，無 user 覆蓋
    USERS["u005"]: "summary",   # dept-C 無規則，但 user=summary
}

# ── 測試 PDF（用於上傳測試）────────────────────────────────────
# fixtures/test.pdf 是從 ingest-worker volume 複製出來的真實 PDF
TEST_PDF = os.path.join(os.path.dirname(__file__), "fixtures", "test.pdf")

# ── Container 內路徑（用於 ingest-worker 分階段測試）─────────
CONTAINER_PDF      = "/data/uploads/104fa00d-4609-4368-a1f8-e9edd35bab9b/deptA_IT-OT_Network_Policy.pdf"
CONTAINER_WORK_DIR = "/data/uploads/104fa00d-4609-4368-a1f8-e9edd35bab9b"


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
