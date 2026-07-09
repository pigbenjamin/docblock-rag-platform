# 系統架構

## 總覽

docblock-rag-platform 是多租戶企業文件 RAG 平台，核心能力：

- **PDF 全流程 ingest**：Marker OCR → 語意切塊 → 向量嵌入（透過 LiteLLM）→ pgvector
- **多模態搜尋**：文字 dense + 表格 dense/BM25 + 圖片 CLIP + 摘要
- **文件層級 ACL**：每份文件可對 user／department 設定 detail／summary／deny
- **Keycloak 整合**：用戶群組異動透過 webhook 自動同步至 `user_principal`
- **LiteLLM 整合**：embedding、rerank、chat 以 OpenAI-compatible 格式直連外部 LiteLLM

---

## 服務拓樸

```
                       外部 Client
                          │
          ┌───────────────┼───────────────┐
          ▼               ▼               ▼
    retrieve-api    document-api    webhook-service
      :8761             :8765            :8763
        │                 │                │
        │     ┌───────────┘                │
        │     ▼                            │
        │  ingest-worker                   │
        │     :8762                        │
        │       │                          │
        │       ▼                          │
        │  外部 LiteLLM（marker / embedding / rerank / chat）
        │                                  │
        └───────┬──────────────────────────┘
                ▼
      PostgreSQL + pgvector
           :5437 (host)
           :5432 (container)

外部依賴
```

---

### 服務職責

| 服務 | 對外 Port | 職責 |
|------|-----------|------|
| `retrieve-api` | 8761 | 語意搜尋 + RAG 問答（REST + MCP） |
| `document-api` | 8765 | 文件上傳管理 + ACL 設定 |
| `ingest-worker` | 8762 | PDF ingest 任務排程 |
| `webhook-service` | 8763 | 接收 Keycloak 事件，同步 `user_principal` |
| `postgres` | 5437 | pgvector 向量資料庫 |

> PDF → Markdown OCR（Marker）由 **firdi-litellm** 平台承載（`marker/pdf-to-md` 模型路由），
> 不是本平台部署的服務。

> LLM 推論（marker / embedding / rerank / chat）由**外部 LiteLLM** 提供，
> 所有服務透過 `LITELLM_BASE_URL` 以 OpenAI-compatible 格式直連。

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
documents (tenant_id, document_id UUID PK, external_ref TEXT NULL, ...)
    │
    ├── text_chunks    (chunk_index, version, content, embedding vector(768))
    ├── table_chunks   (chunk_index, version, raw_table_md, embedding, tsvector)
    ├── image_chunks   (chunk_index, version, clip_embedding vector(768), text_embedding)
    ├── summary_chunks (one row per document, embedding vector(768))
    ├── document_sum   (semantic_summary, retrieval_summary JSONB)
    └── document_acl   (principal_type, principal_id, effect)

user_principal (tenant_id, user_id UUID, principal_type, principal_id)
```

### 主鍵設計

| 欄位 | 類型 | 說明 |
|------|------|------|
| `document_id` | UUID | 唯一識別碼，上傳時由 document-api 生成（新文件）或由呼叫端指定既有值（新版本） |
| `external_ref` | TEXT（可空） | 選填，外部系統代碼（如 Outline），僅供參考，不參與唯一性或版本判斷 |
| `active_version` | INT | 現行版本號，content_sha256 改變時自動遞增 |

### 版本策略

上傳時帶既有 `document_id`（同一份邏輯文件）：內容相同（sha256 不變）→ 不建新版本；內容有變 → `active_version + 1`，舊版 chunks 保留但搜尋時僅查現行版本。不帶 `document_id` → 一律視為新文件，生成新 UUID。

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
| 文字 chunks | EmbeddingGemma-300m | 768 | LiteLLM 直連 |
| 表格 | EmbeddingGemma-300m | 768 | LiteLLM 直連 |
| 圖片文字 | EmbeddingGemma-300m | 768 | LiteLLM 直連 |
| 圖片視覺 | CLIP ViT-L/14 | 768 | 本地（ingest-worker） |
| 摘要 | EmbeddingGemma-300m | 768 | LiteLLM 直連 |

向量索引：PostgreSQL HNSW（pgvector）

---

## 部署架構（Compose）

```yaml
# docker-compose.yml 重點
services:
  ingest-worker:
    environment:
      LITELLM_PROXY_URL: http://10.90.20.55:30400   # marker/pdf-to-md 直連外部 LiteLLM

  # 所有服務透過 LITELLM_BASE_URL 直連外部 LiteLLM
  retrieve-api:
    env_file: .env   # LITELLM_BASE_URL=http://10.90.20.55:30400
```

---

## 模型依賴

| 模型 | 用途 | 執行位置 |
|------|------|----------|
| `embeddinggemma-300m` | 向量嵌入 | 外部 LiteLLM（直連） |
| `Qwen3-Reranker-8B` | Reranker | 外部 LiteLLM（直連） |
| `qwen3:8b` 或其他 | RAG 問答 + Query routing | 外部 LiteLLM（直連） |
| Marker / Surya | PDF → Markdown OCR | 外部 LiteLLM（firdi-litellm 平台承載） |
| CLIP ViT-L/14 | 圖片向量 | ingest-worker container |
