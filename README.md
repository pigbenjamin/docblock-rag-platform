# docblock-rag-platform

企業文件 RAG（Retrieval-Augmented Generation）平台的 container 化版本。
整合 Keycloak 用戶權限管理、文件 ACL 控制、多模態語意搜尋與 LLM 問答生成。

---

## 架構概覽

```
docblock-rag-platform/
├── libs/
│   └── docblock-core/          # 共用 Python 套件（向量搜尋、嵌入、ACL 邏輯）
│
├── services/
│   ├── retrieve-api/           # 語意搜尋 + RAG 問答（REST API + MCP）     :8761
│   ├── webhook-service/        # Keycloak 用戶同步（webhook 接收）          :8763
│   ├── ingest-worker/          # 文件 ingest pipeline（PDF → 向量 DB）      :8762
│   ├── admin-api/              # 文件上傳管理 + ACL 設定                    :8765
│   └── marker-service/         # PDF → Markdown OCR（GPU，獨立服務）        :8766
│
├── deployments/
│   ├── compose/                # Docker Compose（本地開發）
│   │   └── litellm_config.yaml # LiteLLM proxy 路由設定（marker/pdf-to-md）
│   ├── docker/postgres/init/   # PostgreSQL 初始化 Schema
│   ├── keycloak/plugins/       # Keycloak Java 事件監聽插件
│   ├── helm/                   # Helm Charts（k8s 部署）
│   └── k8s/                    # Kubernetes manifests
│
├── monitoring/                 # 監控設定（待規劃）
└── scripts/                    # 建置與啟動腳本
```

---

## 服務說明

### retrieve-api（Port 8761）

語意搜尋與 RAG 問答服務，同時提供 REST API 和 MCP 協定。

**REST 端點**

| 方法 | 路徑 | 說明 |
|------|------|------|
| `POST` | `/v1/search` | 跨文件語意搜尋（ACL 過濾） |
| `POST` | `/v1/answer` | 單文件 RAG 問答生成（ACL 過濾） |
| `POST` | `/v1/search/open` | 跨文件搜尋（不套用 ACL，管理員用） |
| `GET`  | `/healthz` | 健康檢查 |

**MCP 工具**（掛載於 `/mcp`）

| 工具名 | 說明 |
|--------|------|
| `rag_answer` | 單文件 RAG 問答（ACL 強制執行） |
| `rag_search` | 跨文件搜尋（ACL 強制執行） |
| `rag_search_open` | 跨文件搜尋（略過 ACL） |
| `rag_search_open_hits_string` | 搜尋結果以純文字返回（供 LLM 直接餵入） |
| `rag_gen_check` | 驗證 LLM 答案與搜尋結果的一致性（幻覺檢查） |

---

### webhook-service（Port 8763）

接收 Keycloak 事件，將用戶／群組關係同步至 PostgreSQL `user_principal` 表。

| 方法 | 路徑 | Header | 說明 |
|------|------|--------|------|
| `POST` | `/keycloak/user-sync` | `X-Webhook-Secret` | 接收 Keycloak 用戶異動事件 |
| `GET`  | `/healthz` | — | 健康檢查 |

> 需搭配 `deployments/keycloak/plugins/` 下的 Java 插件，部署至 Keycloak server 後自動觸發 webhook。

---

### ingest-worker（Port 8762）

接收 ingest 任務並以背景執行，支援分段提交或一次性全流程執行。

**Pipeline 流程**
```
PDF → [marker-service via litellm-proxy] → Markdown → [build_chunks] → chunk_block.json → [ingest] → PostgreSQL
```

| 方法 | 路徑 | 說明 |
|------|------|------|
| `POST` | `/jobs/pipeline` | 全流程：PDF → 向量 DB（推薦） |
| `POST` | `/jobs/marker` | 只執行 PDF → Markdown |
| `POST` | `/jobs/build-chunks` | 只執行 Markdown → chunk JSON |
| `POST` | `/jobs/ingest` | 只執行 chunk JSON → DB |
| `GET`  | `/jobs/{job_id}` | 查詢 job 執行狀態 |
| `GET`  | `/healthz` | 健康檢查 |

---

### marker-service（Port 8766）

PDF → Markdown OCR 獨立服務（需 GPU），由 ingest-worker 透過 litellm-proxy 呼叫。

| 方法 | 路徑 | 說明 |
|------|------|------|
| `POST` | `/v1/convert` | 直接 REST 呼叫 PDF → Markdown |
| `POST` | `/v1/chat/completions` | OpenAI-compatible（供 litellm-proxy 路由） |
| `GET`  | `/healthz` | 健康檢查 |

---

### litellm-proxy（Port 4000）

LLM / 工具呼叫路由 proxy，目前路由 `marker/pdf-to-md` → marker-service。

| 方法 | 路徑 | 說明 |
|------|------|------|
| `POST` | `/v1/chat/completions` | 依 `model` 欄位路由至對應後端 |
| `GET`  | `/v1/models` | 列出已設定的模型路由 |
| `GET`  | `/health/liveliness` | 健康檢查 |

---

### admin-api（Port 8765）

文件管理入口與 ACL 設定服務。

**文件管理**

| 方法 | 路徑 | 說明 |
|------|------|------|
| `POST` | `/v1/documents/upload` | 上傳 PDF，自動觸發 ingest-worker 全流程 |
| `GET`  | `/v1/documents/job/{job_id}` | 查詢 ingest 進度 |
| `GET`  | `/v1/documents/` | 列出所有文件 |
| `GET`  | `/v1/documents/{doc_id}` | 查詢單一文件 metadata |
| `DELETE` | `/v1/documents/{doc_id}` | 刪除文件（Cascade 刪除所有 chunks + ACL） |

**ACL 設定**（需 `X-Acl-Secret` Header）

| 方法 | 路徑 | 說明 |
|------|------|------|
| `POST` | `/v1/acl/write-map` | 設定文件存取權限（`detail` / `summary` / `deny`） |
| `POST` | `/v1/acl/delete-map` | 刪除文件存取權限 |

---

## 共用函式庫：docblock-core

`libs/docblock-core/` 是所有服務共用的 Python 套件，各服務 Docker build 時會先安裝它。

| 模組 | 功能 |
|------|------|
| `config.py` | 統一設定（讀取環境變數） |
| `search.py` | 多模態搜尋（向量 + BM25 + RRF 融合） |
| `acl.py` | ACL 計算與過濾 |
| `rag.py` | RAG 問答生成 |
| `ingest.py` | 向量嵌入計算 + 寫入 DB |
| `chunk_builder.py` | Markdown → chunk block |
| `gen_sum.py` | 文件摘要生成 |
| `sql_utils.py` | PostgreSQL CRUD helpers |

---

## 快速啟動（本地開發）

### 前置需求

- Docker & Docker Compose v2
- Ollama 已啟動並載入所需模型（`bge-m3`, `qwen3:8b` 等）
- Keycloak（可選，僅 webhook-service 需要）

### 步驟

```bash
# 1. Clone 專案
git clone <repo-url> docblock-rag-platform
cd docblock-rag-platform

# 2. 建立 .env
cp deployments/compose/.env.example deployments/compose/.env
# 編輯 .env，填入 Ollama URL、Keycloak 設定等

# 3. 啟動所有服務
./scripts/dev-up.sh

# 或手動執行
docker compose -f deployments/compose/docker-compose.yml --env-file deployments/compose/.env up --build
```

啟動後可存取：
- Admin API Docs：http://localhost:8765/docs
- Retrieve API Docs：http://localhost:8761/docs
- Webhook Service Docs：http://localhost:8763/docs
- Ingest Worker Docs：http://localhost:8762/docs
- Marker Service Docs：http://localhost:8766/docs
- LiteLLM Proxy UI：http://localhost:4000

### 單獨 build 所有 image

```bash
./scripts/build-all.sh

# 加上 --push 同時推送至 registry
./scripts/build-all.sh --push
```

---

## 環境變數

所有服務共用 `deployments/compose/.env`，主要設定項目：

| 變數 | 說明 | 預設值 |
|------|------|--------|
| `PG_DSN` | PostgreSQL 連線字串 | `dbname=acl_FIRDI ...` |
| `TENANT_ID` | 租戶 ID | `firdi` |
| `OLLAMA_BASE_URL` | Ollama API 位址 | `http://localhost:11434` |
| `EMBED_MODEL` | 嵌入模型名稱 | `bge-m3` |
| `SEG_MODEL` | 文件切割 LLM | `qwen3:8b` |
| `KEYCLOAK_URL` | Keycloak 伺服器位址 | — |
| `KEYCLOAK_REALM` | Keycloak Realm | — |
| `KEYCLOAK_CLIENT_SECRET` | Keycloak 服務帳號密鑰 | — |
| `WEBHOOK_SECRET` | Keycloak webhook 驗證密鑰 | — |
| `ACL_ADMIN_SECRET` | ACL 管理 API 驗證密鑰 | — |
| `INGEST_WORKER_URL` | admin-api 連接 ingest-worker 的位址 | `http://ingest-worker:8762` |
| `LITELLM_PROXY_URL` | ingest-worker 連接 litellm-proxy 的位址 | `http://litellm-proxy:4000` |
| `LITELLM_API_KEY` | litellm-proxy master key | `sk-litellm-internal` |

---

## 資料庫 Schema

PostgreSQL（需 pgvector 擴充），init script 位於 `deployments/docker/postgres/init/01_schema.sql`。

| 資料表 | 說明 |
|--------|------|
| `documents` | 文件 metadata（tenant_id, doc_id, version…） |
| `text_chunks` | 文字段落向量（BGE-M3 1024 維） |
| `table_chunks` | 表格向量 + BM25 全文索引 |
| `image_chunks` | 圖片 CLIP 向量 + 文字向量 |
| `summary_chunks` | 文件段落摘要向量 |
| `document_sum` | 文件層級語意摘要 |
| `user_principal` | 用戶 → Principal 映射（user/department/role） |
| `document_acl` | 文件存取規則（detail/summary/deny） |

---

## Keycloak 插件

`deployments/keycloak/plugins/keycloak-user-sync-listener/` 為 Maven 專案，編譯後的 JAR 需部署至 Keycloak server 的 `providers/` 目錄。

```bash
cd deployments/keycloak/plugins/keycloak-user-sync-listener
mvn package -DskipTests
# 將 target/keycloak-user-sync-listener-1.0.0.jar 複製至 Keycloak providers/
```

插件會監聽 `USER_CREATE`、`USER_UPDATE`、`USER_DELETE` 事件，並 POST 至 `webhook-service` 的 `/keycloak/user-sync`。

---

## ACL 說明

### 存取層級

| effect | 說明 |
|--------|------|
| `detail` | 完整存取（所有 chunk 類型） |
| `summary` | 僅摘要存取（只能看到 summary_chunks） |
| `deny` | 拒絕存取 |

### Principal 優先順序

`user` > `role` > `department`，`deny` 效果優先於 `detail`/`summary`。
無匹配規則時預設為 `deny`。

### 設定範例

```bash
# 授予 department A 完整存取
curl -X POST http://localhost:8765/v1/acl/write-map \
  -H "X-Acl-Secret: your-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "document_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "access_rules": [
      {"principal_type": "department", "principal_id": "A", "effect": "detail"},
      {"principal_type": "department", "principal_id": "B", "effect": "summary"}
    ]
  }'
```

---

## 文件上傳流程

```
用戶 → POST /v1/documents/upload (admin-api:8765)
         ↓ 儲存 PDF 至 /data/uploads/
         ↓ POST /jobs/pipeline (ingest-worker:8762)
              ↓ [背景執行]
              ↓ Marker: POST /v1/chat/completions (litellm-proxy:4000)
                          ↓ → marker-service:8766  PDF → Markdown（GPU）
              ↓ build_chunks: Markdown → chunk_block.json
              ↓ ingest: chunk_block.json → PostgreSQL (向量 + metadata)
```

查詢進度：`GET /v1/documents/job/{job_id}`

---

## 原始專案

本專案從 `docblock-rag`（單機版）重構而來，對應關係：

| docblock-rag | docblock-rag-platform |
|---|---|
| `core/` | `libs/docblock-core/docblock_core/` |
| `webhook_app/keycloak/` + `db/` | `services/webhook-service/` |
| `rag_mcp/` | `services/retrieve-api/app/mcp/` |
| `pipeline/` | `services/ingest-worker/worker/tasks/` |
| `webhook_app/acl/` | `services/admin-api/app/` |
| `sql/new_postgresql_schema.sql` | `deployments/docker/postgres/init/01_schema.sql` |
| `keycloak_plugins/` | `deployments/keycloak/plugins/` |
