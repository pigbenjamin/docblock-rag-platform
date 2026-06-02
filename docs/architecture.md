# 系統架構

## 總覽

docblock-rag-platform 是多租戶企業文件 RAG 平台，核心能力：

- **PDF 全流程 ingest**：Marker OCR → 語意切塊 → 向量嵌入（透過 LiteLLM）→ pgvector
- **多模態搜尋**：文字 dense + 表格 dense/BM25 + 圖片 CLIP + 摘要
- **文件層級 ACL**：每份文件可對 user／department 設定 detail／summary／deny
- **Keycloak 整合**：用戶群組異動透過 webhook 自動同步至 `user_principal`
- **Nostr 通訊層**：embedding、rerank、chat 透過 Nostr relay 加密路由至 LiteLLM

---

## 服務拓樸

```
                       外部 Client
                          │
          ┌───────────────┼───────────────┐
          ▼               ▼               ▼
    retrieve-api      admin-api     webhook-service
      :8761             :8765            :8763
        │                 │                │
        │     ┌───────────┘                │
        │     ▼                            │
        │  ingest-worker                   │
        │     :8762                        │
        │       │                          │
        │       ▼                          │
        │  litellm-proxy (marker only)     │
        │     :4000                        │
        │       │                          │
        │       ▼                          │
        │  marker-service  (GPU)           │
        │     :8766                        │
        │                                  │
        │  ┌────────────────────────────┐  │
        │  │  Nostr 通訊層              │  │
        │  │                            │  │
        └──┤  nostr-proxy :8800         │  │
           │    │ Kind 2000/2001/2002   │  │
           │    ▼                       │  │
           │  Nostr Relay (外部)        │  │
           │    │                       │  │
           │    ▼                       │  │
           │  nostr-consumer            │  │
           │    │                       │  │
           │    ▼                       │  │
           │  LiteLLM (外部)            │  │
           └────────────────────────────┘  │
                                           │
        └──────────────┬────────────────────┘
                       ▼
             PostgreSQL + pgvector
                  :5437 (host)
                  :5432 (container)

外部依賴
  Nostr Relay      ←  wss://...（轉發 Kind 2000/2001/2002 事件）
  LiteLLM (外部)   ←  embedding / rerank / chat 模型
  Keycloak         ←  webhook-service 使用
```

---

## Nostr 通訊層說明

所有 LLM 推論請求（embedding、rerank、chat）透過 Nostr 協定路由，流程如下：

```
retrieve-api / RagClient
  │  LITELLM_BASE_URL = http://nostr-proxy:8800
  ▼
nostr-proxy（OpenAI-compatible HTTP server）
  │  依 endpoint 選擇 Nostr Kind
  ├─ POST /v1/embeddings       → Kind 2000
  ├─ POST /v1/rerank           → Kind 2001
  └─ POST /v1/chat/completions → Kind 2002
  │
  ▼ 簽名 Nostr 事件，發送至 Relay
Nostr Relay（wss://...）
  │
  ▼ nostr-consumer 訂閱 Kind 2000/2001/2002
nostr-consumer
  │  LITELLM_BASE_URL = http://外部-litellm:port
  ├─ Kind 2000 → POST /v1/embeddings
  ├─ Kind 2001 → POST /v1/rerank
  └─ Kind 2002 → POST /v1/chat/completions
  │
  ▼ 簽名回覆事件（Kind 1000），發送至 Relay
Nostr Relay
  │
  ▼ nostr-proxy 等待 Kind 1000 reply
nostr-proxy → HTTP response → retrieve-api
```

**繞過 Nostr（fallback）：** 各操作可透過 env var 關閉 Nostr routing，直接打 `OLLAMA_DIRECT_URL`：
- `EMBED_VIA_NOSTR=false`
- `RERANK_VIA_NOSTR=false`
- `CHAT_VIA_NOSTR=false`

---

### 服務職責

| 服務 | 對外 Port | 職責 |
|------|-----------|------|
| `retrieve-api` | 8761 | 語意搜尋 + RAG 問答（REST + MCP） |
| `admin-api` | 8765 | 文件上傳管理 + ACL 設定 |
| `ingest-worker` | 8762 | PDF ingest 任務排程（marker 以外的 pipeline） |
| `webhook-service` | 8763 | 接收 Keycloak 事件，同步 `user_principal` |
| `marker-service` | 8766 | PDF → Markdown OCR（GPU，獨立服務） |
| `litellm-proxy` | 4000 | LLM 路由 proxy（目前僅 marker/pdf-to-md） |
| `nostr-proxy` | 8800 | OpenAI-compatible HTTP → Nostr 事件轉換 |
| `nostr-consumer` | — | Nostr 事件訂閱 → LiteLLM 推論，回覆結果 |
| `postgres` | 5437 | pgvector 向量資料庫 |

---

## 共用函式庫：docblock-core

所有服務共用 `libs/docblock-core/`，Dockerfile 建置時 `pip install` 安裝。

```
libs/docblock-core/docblock_core/
├── config.py          # 統一設定（環境變數 → dataclass）
├── search.py          # 多模態搜尋 + ACL 過濾 + RRF 融合
├── acl.py             # ACL 計算（principal 解析 + SQL CTE 優先順序）
├── ingest.py          # 向量嵌入 + 批次寫入 DB
├── chunk_builder.py   # Markdown → chunk_block.json
├── marker_runner.py   # Marker CLI 外部程序邊界
├── rag.py             # RAG 問答生成（search + LiteLLM chat）
├── jobs.py            # Job 狀態機 + SHA256 工具
├── sql_utils.py       # psycopg2 CRUD helpers
├── md_semantic_chunk_plus.py  # Markdown 語意切塊器
├── gen_sum.py         # 文件摘要生成
├── clip_embed.py      # CLIP 圖片向量（vision）
└── logging_utils.py   # 統一 logging
```

**API 格式**：docblock-core 全面使用 OpenAI-compatible（LiteLLM）格式，透過 `LITELLM_BASE_URL` 統一路由。Ollama 特有的 `/api/chat`、`/api/embeddings` 格式已不再使用。

---

## 資料庫 Schema

```
documents (tenant_id, document_id UUID PK, doc_id TEXT UNIQUE, ...)
    │
    ├── text_chunks    (chunk_index, version, content, embedding vector(1024))
    ├── table_chunks   (chunk_index, version, raw_table_md, embedding, tsvector)
    ├── image_chunks   (chunk_index, version, clip_embedding vector(768), text_embedding)
    ├── summary_chunks (one row per document, embedding vector(1024))
    ├── document_sum   (semantic_summary, retrieval_summary JSONB)
    └── document_acl   (principal_type, principal_id, effect)

user_principal (tenant_id, user_id UUID, principal_type, principal_id)
```

### 主鍵設計

| 欄位 | 類型 | 說明 |
|------|------|------|
| `document_id` | UUID | DB 內部 PK，應用端提供（ingest 時生成） |
| `doc_id` | TEXT | 邏輯識別碼（外部系統如 Outline 提供），tenant 內唯一 |
| `active_version` | INT | 現行版本號，content_sha256 改變時自動遞增 |

### 版本策略

重複 ingest 同一 `doc_id`（相同內容）→ 不建新版本；內容有變 → `active_version + 1`，舊版 chunks 保留但搜尋時僅查現行版本。

### ACL 表結構

```sql
document_acl (
  tenant_id       TEXT,
  document_id     UUID,    -- FK → documents
  principal_type  TEXT,    -- "user" | "department"
  principal_id    TEXT,    -- user UUID 或 dept 名稱
  effect          TEXT     -- "detail" | "summary" | "deny"
)
```

> `role` 僅作為用戶屬性儲存於 `user_principal`，**不用於文件 ACL**。

---

## ACL 優先順序

```
優先順序：user (30) > department (10)
效果等級：deny (30) > detail (20) > summary (10)
```

```sql
WITH principals(principal_type, principal_id, priority) AS (
  VALUES ('user', '<user_id>', 30), ('department', 'dept-A', 10)
),
matches AS (
  SELECT a.document_id, a.effect,
         p.priority,
         CASE a.effect WHEN 'deny' THEN 30 WHEN 'detail' THEN 20 ELSE 10 END AS effect_rank
  FROM document_acl a
  JOIN principals p USING (principal_type, principal_id)
  WHERE a.tenant_id = 'firdi'
),
best AS (
  SELECT DISTINCT ON (document_id) document_id, effect
  FROM matches
  ORDER BY document_id, priority DESC, effect_rank DESC
)
SELECT ...
```

無匹配規則時預設 `deny`。

---

## 向量嵌入規格

| 資料類型 | 模型 | 向量維度 | 路由 |
|----------|------|----------|------|
| 文字 chunks | Qwen3-Embedding-8B | 4096 | nostr-proxy → LiteLLM |
| 表格 | Qwen3-Embedding-8B | 4096 | nostr-proxy → LiteLLM |
| 圖片文字 | Qwen3-Embedding-8B | 4096 | nostr-proxy → LiteLLM |
| 圖片視覺 | CLIP ViT-L/14 | 768 | 本地（ingest-worker） |
| 摘要 | Qwen3-Embedding-8B | 4096 | nostr-proxy → LiteLLM |

向量索引：PostgreSQL HNSW（pgvector）

---

## 部署架構（Compose）

```yaml
# docker-compose.yml 重點
services:
  nostr-proxy:
    ports: ["8800:8800"]            # ← OpenAI-compatible HTTP 入口
    environment:
      PYTHONUNBUFFERED: "1"

  nostr-consumer:                   # ← 無 port（純 subscriber）
    environment:
      LITELLM_BASE_URL: http://外部-litellm:port  # ← 覆寫指向外部 LiteLLM
      PYTHONUNBUFFERED: "1"
    volumes:
      - nostr_consumer_data:/app/data   # audit.db 持久化
    stop_grace_period: 15s              # SIGTERM 後等 15s 再 SIGKILL

  marker-service:                   # ← GPU 服務（獨立）
    volumes:
      - ingest_data:/data
      - ${HOME}/.cache/datalab/models:/datalab_cache:ro
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              capabilities: [gpu]

  litellm-proxy:                    # ← 僅路由 marker/pdf-to-md
    volumes:
      - ./litellm_config.yaml:/app/config.yaml:ro

  ingest-worker:
    environment:
      LITELLM_PROXY_URL: http://litellm-proxy:4000

  # retrieve-api 透過 LITELLM_BASE_URL 連接 nostr-proxy
  retrieve-api:
    env_file: .env   # LITELLM_BASE_URL=http://nostr-proxy:8800
```

**Nostr consumer 的 LITELLM_BASE_URL 說明：**  
docker-compose 內部的 `litellm-proxy` 只設定了 `marker/pdf-to-md` 路由，embedding/rerank/chat 由外部 LiteLLM 提供。consumer 的 `LITELLM_BASE_URL` 在 compose 層覆寫為外部 LiteLLM 地址，與 retrieve-api 使用的 nostr-proxy URL 不同。

---

## 模型依賴

| 模型 | 用途 | 執行位置 |
|------|------|----------|
| `Qwen3-Embedding-8B` | 向量嵌入 | 外部 LiteLLM（透過 Nostr） |
| `Qwen3-Reranker-8B` | Reranker | 外部 LiteLLM（透過 Nostr） |
| `qwen3:8b` 或其他 | RAG 問答 + Query routing | 外部 LiteLLM（透過 Nostr） |
| Marker / Surya | PDF → Markdown OCR | **marker-service** container（GPU） |
| CLIP ViT-L/14 | 圖片向量 | ingest-worker container |
