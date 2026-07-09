# docblock-rag-platform

企業文件 RAG（Retrieval-Augmented Generation）平台的 container 化版本。
整合 Keycloak 用戶權限管理、文件 ACL 控制、多模態語意搜尋與 LLM 問答生成。
LLM 推論（marker / embedding / rerank / chat）以 OpenAI-compatible 格式直連外部 LiteLLM。

---

## 架構概覽

```
docblock-rag-platform/
├── libs/
│   └── docblock-core/          # 共用 Python 套件（向量搜尋、嵌入、ACL 邏輯）
│
├── services/
│   ├── retrieve-api/           # 語意搜尋 + RAG 問答（REST API + MCP）     :8761
│   ├── document-api/           # 文件上傳管理 + ACL 設定                    :8765
│   ├── ingest-worker/          # 文件 ingest pipeline（PDF → 向量 DB）      :8762
│   └── webhook-service/        # Keycloak 用戶同步（webhook 接收）          :8763
│     （PDF → Markdown OCR 由外部 firdi-litellm 平台承載，非本 repo 服務）
│
├── deployments/
│   ├── compose/                # Docker Compose（本地開發/測試）
│   │   ├── docker-compose.yml
│   │   └── .env                # 環境變數（含密鑰，不進 git）
│   ├── docker/postgres/init/   # PostgreSQL 初始化 Schema
│   ├── keycloak/plugins/       # Keycloak Java 事件監聽插件
│   ├── helm/                   # Helm Charts（k8s 部署）
│   ├── k8s/                    # Kubernetes manifests（正式環境；01-secrets.yaml
│   │                           #   機密不進 git，02-configmap.yaml 進 git，兩者手動維護）
│   ├── build-all.sh            # Build 全部 service image
│   ├── push-all.sh             # Push 全部 image 至 GHCR
│   └── RUNBOOK.md              # Image 管理操作手冊
│
└── tests/                      # 整合測試腳本
```

---

## 服務說明

### retrieve-api（Port 8761）

語意搜尋與 RAG 問答服務，同時提供 REST API 和 MCP 協定。

| 方法 | 路徑 | 說明 |
|------|------|------|
| `POST` | `/v1/search` | 跨文件語意搜尋（ACL 過濾） |
| `POST` | `/v1/answer` | 單文件 RAG 問答生成（ACL 過濾） |
| `GET`  | `/healthz` | 健康檢查 |
| `GET`  | `/readyz` | 就緒檢查（DB + LiteLLM） |

**MCP 工具**（掛載於 `/mcp`）：`rag_answer`、`rag_search`、`rag_gen_check`

---

### document-api（Port 8765）

| 方法 | 路徑 | 說明 |
|------|------|------|
| `POST` | `/v1/documents/upload` | 上傳 PDF，自動觸發 ingest pipeline（省略 document_id 建新文件；帶既有 document_id 上傳新版本） |
| `GET`  | `/v1/documents/job/{job_id}` | 查詢 ingest 進度 |
| `GET`  | `/v1/documents/` | 列出所有文件 |
| `DELETE` | `/v1/documents/{document_id}` | 刪除文件（Cascade） |
| `POST` | `/v1/acl/write-map` | 設定文件存取規則 |
| `POST` | `/v1/acl/delete-map` | 刪除文件存取規則 |

---

### ingest-worker（Port 8762）

| 方法 | 路徑 | 說明 |
|------|------|------|
| `POST` | `/jobs/pipeline` | 全流程：PDF → 向量 DB |
| `POST` | `/jobs/marker` | 只執行 PDF → Markdown |
| `POST` | `/jobs/build-chunks` | 只執行 Markdown → chunk JSON |
| `POST` | `/jobs/ingest` | 只執行 chunk JSON → DB |
| `GET`  | `/jobs/{job_id}` | 查詢 job 狀態 |

---

> PDF → Markdown OCR（Marker）、Embedding、Rerank、Chat 皆由**外部 LiteLLM** 提供
> （由 firdi-litellm 平台承載，marker 對應 `marker/pdf-to-md` 模型路由）。

---

## 快速啟動（Docker Compose）

### 前置需求

- Docker & Docker Compose v2
- 外部 LiteLLM（提供 marker / embedding / rerank / chat 模型）

### 步驟

```bash
# 1. Clone 專案
git clone <repo-url> docblock-rag-platform
cd docblock-rag-platform

# 2. 設定環境變數
# 編輯 deployments/compose/.env，填入 LiteLLM 地址、API key 等

# 3. 啟動所有服務
cd deployments/compose
docker compose up -d

# 4. 確認狀態
docker compose ps
```

服務啟動後：
- Retrieve API：http://localhost:8761/docs
- Admin API：http://localhost:8765/docs

---

## Build & Push Image

詳細說明見 [deployments/RUNBOOK.md](deployments/RUNBOOK.md)。

```bash
export OWNER=<your-github-username-or-org>

# Build 全部 service
./deployments/build-all.sh

# Build 全部（不用 cache）
./deployments/build-all.sh --no-cache

# 登入 GHCR
echo $GITHUB_PAT | docker login ghcr.io -u $OWNER --password-stdin

# Push 全部 image
./deployments/push-all.sh
```

Image 存放於：`ghcr.io/<OWNER>/docblock-rag-platform/<service>:latest`

---

## 環境變數

完整說明見 `deployments/compose/.env`：

| 變數 | 說明 |
|------|------|
| `PG_DSN` | PostgreSQL 連線字串 |
| `TENANT_ID` | 租戶 ID（預設 `firdi`） |
| `LITELLM_BASE_URL` | 外部 LiteLLM URL（docblock-core 所有 LLM 呼叫的入口） |
| `LITELLM_API_KEY` | LiteLLM API key |
| `EMBED_MODEL` | 嵌入模型名稱 |
| `RERANK_MODEL` | Reranker 模型名稱 |
| `CHAT_MODEL` | Chat 模型名稱 |
| `ACL_ADMIN_SECRET` | ACL 管理 API 驗證密鑰 |
| `WEBHOOK_SECRET` | Keycloak webhook 驗證密鑰 |

> ⚠️ `.env` 已加入 `.gitignore`，請勿 commit 含有私鑰的 `.env`。

---

## 整合測試

```bash
python3 tests/01_health_check.py      # 所有服務健康檢查
python3 tests/05_search_acl.py        # ACL 搜尋驗證
python3 tests/08_rag_answer.py        # RAG 問答端對端測試
```

---

## 資料庫 Schema

PostgreSQL + pgvector，init script：`deployments/docker/postgres/init/01_schema.sql`

| 資料表 | 說明 |
|--------|------|
| `documents` | 文件 metadata（tenant_id, document_id, version…） |
| `text_chunks` | 文字段落向量（4096 維） |
| `table_chunks` | 表格向量 + BM25 全文索引 |
| `image_chunks` | 圖片 CLIP 向量 + 文字向量 |
| `summary_chunks` | 文件摘要向量 |
| `user_principal` | 用戶 → Principal 映射（user/department/role） |
| `document_acl` | 文件存取規則（detail/summary/deny） |

---

## ACL 說明

| effect | 說明 |
|--------|------|
| `detail` | 完整存取（所有 chunk 類型） |
| `summary` | 僅摘要存取 |
| `deny` | 拒絕存取（預設） |

優先順序：`user(30) > department(10)`，`deny > detail > summary`

```bash
curl -X POST http://localhost:8765/v1/acl/write-map \
  -H "X-Acl-Secret: your-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "document_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "access_rules": [
      {"principal_type": "department", "principal_id": "dept-A", "effect": "detail"},
      {"principal_type": "user", "principal_id": "user-uuid", "effect": "summary"}
    ]
  }'
```

---

## 文件

| 文件 | 說明 |
|------|------|
| [docs/architecture.md](docs/architecture.md) | 系統架構、DB Schema |
| [docs/deployment.md](docs/deployment.md) | 完整部署指南（Compose + K8s） |
| [docs/api-reference.md](docs/api-reference.md) | 所有服務 API 規格 |
| [docs/internal-logic.md](docs/internal-logic.md) | 搜尋引擎、RAG、ACL 內部邏輯 |
| [deployments/RUNBOOK.md](deployments/RUNBOOK.md) | Image build / push / k8s 操作手冊 |
