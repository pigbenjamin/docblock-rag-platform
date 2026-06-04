# docblock-rag-platform

企業文件 RAG（Retrieval-Augmented Generation）平台的 container 化版本。
整合 Keycloak 用戶權限管理、文件 ACL 控制、多模態語意搜尋與 LLM 問答生成。
LLM 推論（embedding / rerank / chat）透過 **Nostr 協定**加密路由至外部 LiteLLM。

---

## 架構概覽

```
docblock-rag-platform/
├── libs/
│   └── docblock-core/          # 共用 Python 套件（向量搜尋、嵌入、ACL 邏輯）
│
├── services/
│   ├── retrieve-api/           # 語意搜尋 + RAG 問答（REST API + MCP）     :8761
│   ├── admin-api/              # 文件上傳管理 + ACL 設定                    :8765
│   ├── ingest-worker/          # 文件 ingest pipeline（PDF → 向量 DB）      :8762
│   ├── webhook-service/        # Keycloak 用戶同步（webhook 接收）          :8763
│   ├── marker-service/         # PDF → Markdown OCR（GPU，獨立服務）        :8766
│   ├── nostr-proxy/            # OpenAI-compatible HTTP → Nostr 事件        :8800
│   └── nostr-consumer/         # Nostr 事件訂閱 → LiteLLM 推論（無 port）
│
├── deployments/
│   ├── compose/                # Docker Compose（本地開發）
│   │   ├── docker-compose.yml
│   │   ├── .env                # 環境變數（含 Nostr 金鑰，不進 git）
│   │   └── litellm_config.yaml # LiteLLM proxy 路由設定（marker/pdf-to-md）
│   ├── docker/postgres/init/   # PostgreSQL 初始化 Schema
│   ├── keycloak/plugins/       # Keycloak Java 事件監聽插件
│   ├── helm/                   # Helm Charts（k8s 部署）
│   ├── k8s/                    # Kubernetes manifests
│   ├── build-all.sh            # Build 全部 service image
│   ├── push-all.sh             # Push 全部 image 至 GHCR
│   └── RUNBOOK.md              # Image 管理操作手冊
│
└── tests/                      # 整合測試腳本（01～14）
```

---

## Nostr 通訊層

所有 LLM 推論請求透過 Nostr 協定路由：

```
retrieve-api
  │  LITELLM_BASE_URL=http://nostr-proxy:8800
  ▼
nostr-proxy（OpenAI-compatible HTTP）
  ├─ POST /v1/embeddings       → Kind 2000 ─┐
  ├─ POST /v1/rerank           → Kind 2001 ─┤ Nostr Relay
  └─ POST /v1/chat/completions → Kind 2002 ─┘
                                              │
                                         nostr-consumer
                                              │
                                         外部 LiteLLM
```

可透過環境變數切換 Nostr / 直連：`EMBED_VIA_NOSTR`、`RERANK_VIA_NOSTR`、`CHAT_VIA_NOSTR`（預設全部 `true`）

---

## 服務說明

### retrieve-api（Port 8761）

語意搜尋與 RAG 問答服務，同時提供 REST API 和 MCP 協定。

| 方法 | 路徑 | 說明 |
|------|------|------|
| `POST` | `/v1/search` | 跨文件語意搜尋（ACL 過濾） |
| `POST` | `/v1/search/open` | 跨文件搜尋（略過 ACL，管理員用） |
| `POST` | `/v1/answer` | 單文件 RAG 問答生成（ACL 過濾） |
| `GET`  | `/healthz` | 健康檢查 |
| `GET`  | `/readyz` | 就緒檢查（DB + LiteLLM） |

**MCP 工具**（掛載於 `/mcp`）：`rag_answer`、`rag_search`、`rag_search_open`、`rag_search_open_hits_string`、`rag_gen_check`

---

### nostr-proxy（Port 8800）

OpenAI-compatible HTTP 入口，將請求轉換為 Nostr 事件並等待回覆。

| 方法 | 路徑 | Kind | 說明 |
|------|------|------|------|
| `POST` | `/v1/embeddings` | 2000 | 向量嵌入（OpenAI format） |
| `POST` | `/v1/rerank` | 2001 | 文件重排序 |
| `POST` | `/v1/chat/completions` | 2002 | 聊天補全 |
| `POST` | `/api/embeddings` | 2000 | Ollama-compat legacy |
| `GET`  | `/health` | — | 健康檢查 |

---

### nostr-consumer（無 HTTP port）

訂閱 Nostr Relay 的 Kind 2000/2001/2002 事件，轉發至外部 LiteLLM 並回覆結果。

- 處理結果寫入 `audit.db`（稽核記錄）
- 已實作 SIGTERM handler，支援 docker / k8s 優雅停止

---

### admin-api（Port 8765）

| 方法 | 路徑 | 說明 |
|------|------|------|
| `POST` | `/v1/documents/upload` | 上傳 PDF，自動觸發 ingest pipeline |
| `GET`  | `/v1/documents/job/{job_id}` | 查詢 ingest 進度 |
| `GET`  | `/v1/documents/` | 列出所有文件 |
| `DELETE` | `/v1/documents/{doc_id}` | 刪除文件（Cascade） |
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

### marker-service（Port 8766）

PDF → Markdown OCR，需 GPU。由 ingest-worker 透過 litellm-proxy 呼叫。

---

### litellm-proxy（Port 4000）

LLM 路由 proxy，目前**僅路由 `marker/pdf-to-md`**（PDF OCR 用）。  
Embedding / Rerank / Chat 由 nostr-consumer 直接呼叫外部 LiteLLM。

---

## 快速啟動（Docker Compose）

### 前置需求

- Docker & Docker Compose v2
- 外部 Nostr Relay（wss://...）
- 外部 LiteLLM（提供 embedding / rerank / chat 模型）
- NVIDIA GPU + driver（marker-service 需要）

### 步驟

```bash
# 1. Clone 專案
git clone <repo-url> docblock-rag-platform
cd docblock-rag-platform

# 2. 設定環境變數
# 編輯 deployments/compose/.env，填入 Relay URL、Nostr 金鑰、LiteLLM 地址等

# 3. 啟動所有服務
cd deployments/compose
docker compose up -d

# 4. 確認狀態
docker compose ps
docker logs compose-nostr-consumer-1 -f
```

服務啟動後：
- Retrieve API：http://localhost:8761/docs
- Admin API：http://localhost:8765/docs
- nostr-proxy health：http://localhost:8800/health

### 確認 Nostr 路徑通暢

```bash
NOSTR_PROXY=http://localhost:8800 python3 tests/14_nostr_proxy.py
```

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
| `LITELLM_BASE_URL` | nostr-proxy URL（docblock-core 所有 LLM 呼叫的入口） |
| `EMBED_MODEL` | 嵌入模型名稱 |
| `RERANK_MODEL` | Reranker 模型名稱 |
| `CHAT_MODEL` | Chat 模型名稱 |
| `RELAY_URL` | Nostr Relay WebSocket URL |
| `NOSTR_PRIV_KEY` | nostr-proxy 簽名私鑰 |
| `NOSTR_PUB_KEY` | nostr-proxy 公鑰（需在 consumer allowlist） |
| `BOT_PRIVATE_KEY` | nostr-consumer 回覆簽名私鑰 |
| `BOT_PUBKEY` | nostr-consumer 公鑰 |
| `LITELLM_API_KEY` | litellm-proxy master key |
| `ACL_ADMIN_SECRET` | ACL 管理 API 驗證密鑰 |
| `WEBHOOK_SECRET` | Keycloak webhook 驗證密鑰 |

> ⚠️ `.env` 已加入 `.gitignore`，請勿 commit 含有私鑰的 `.env`。

---

## 整合測試

```bash
python3 tests/01_health_check.py      # 所有服務健康檢查（含 nostr-proxy）
python3 tests/05_search_acl.py        # ACL 搜尋驗證
python3 tests/08_rag_answer.py        # RAG 問答端對端測試

# Nostr 路徑專項測試（需 nostr-proxy + consumer 在線）
NOSTR_PROXY=http://localhost:8800 python3 tests/14_nostr_proxy.py
```

---

## 資料庫 Schema

PostgreSQL + pgvector，init script：`deployments/docker/postgres/init/01_schema.sql`

| 資料表 | 說明 |
|--------|------|
| `documents` | 文件 metadata（tenant_id, doc_id, version…） |
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
| [docs/architecture.md](docs/architecture.md) | 系統架構、Nostr 通訊層、DB Schema |
| [docs/deployment.md](docs/deployment.md) | 完整部署指南（Compose + K8s） |
| [docs/api-reference.md](docs/api-reference.md) | 所有服務 API 規格 |
| [docs/internal-logic.md](docs/internal-logic.md) | 搜尋引擎、RAG、ACL 內部邏輯 |
| [deployments/RUNBOOK.md](deployments/RUNBOOK.md) | Image build / push / k8s 操作手冊 |
