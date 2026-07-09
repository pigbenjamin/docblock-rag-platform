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

### 儲存清理與版本保留

- 上傳暫存於 job-scoped temp dir（`UPLOAD_DIR/{job_id}/`），ingest pipeline 解出版本號後由 `LocalFileStorage.finalize()` 搬到正式路徑 `{tenant}/{document_id}/v{n}/`。
- **Pipeline 失敗**：ingest-worker 立即 `shutil.rmtree` 整個 job temp dir，不留孤兒檔案；失敗原因/traceback 仍可透過 `GET /jobs/{job_id}`（持久化於 `ingest_jobs.detail`）查詢。
- **Pipeline 成功**：`finalize()` 之後也會刪除 job temp dir，清掉 marker/build_chunks 階段留下的中間產物——`finalize()` 自身的 `rmdir()` 只有目錄已空時才會成功，先前這些中間檔案會一路堆積下去。
- **版本保留**：每次成功 finalize 後呼叫 `LocalFileStorage.prune_old_versions(tenant_id, document_id, keep=5)`，只保留最新 5 個版本目錄，較舊的直接 `shutil.rmtree` 刪除。目前是寫死的常數 `MAX_VERSIONS_RETAINED = 5`（`ingest-worker/worker/main.py`），尚未提供環境變數可調整。

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

## 身份驗證與部門授權（document-api）

### JWT 驗證

`document-api` 解析呼叫者身份的方式（`app/auth.py`）：

- 優先使用 `Authorization: Bearer <token>`：本地以 Keycloak realm 的 JWKS 驗簽（RS256）。JWKS 快取 10 分鐘，`kid` 未命中時強制重新抓取一次（因應 Keycloak 金鑰輪替）。
- 若無 Authorization header，fallback 至舊版 `X-User-Id: <uuid>` header（待前端/測試全面遷移至 JWT 後移除）。
- 兩者皆缺 → 401。

**Issuer 與 JWKS URL 刻意來自不同來源**：

- `issuer` 從 Keycloak 的 `.well-known/openid-configuration` discovery 文件讀取，而非直接假設等於 `KEYCLOAK_URL`——Keycloak 對外宣告的 issuer 主機名稱可能與 `KEYCLOAK_URL`（document-api 實際可連到的內部位址）不同。
- JWKS URL 則直接用 `KEYCLOAK_URL` 組出（`/realms/{realm}/protocol/openid-connect/certs`），**不採用** discovery 文件內的 `jwks_uri`——該欄位所指的外部主機名稱可能無法從 document-api 連線到（已於 dev 環境確認：該主機名可解析但連線逾時，`KEYCLOAK_URL` 則正常）。

### 部門 KM 授權模型

Keycloak realm `FIRDI-AI-Platform` 下，每個部門（A/B/C/...）各自有 `Dev`/`KM`/`User` 子群組。屬於某部門的 `KM` 群組 = 有權上傳/管理該部門的文件（**部門範圍**，非全域權限）。

授權檢查**查詢資料庫，不查 JWT 的角色 claims**：`webhook-service` 將 Keycloak 群組成員異動同步進 `user_principal` 表，寫入 `principal_type='role', principal_id='dept:{X}:role:KM'`；`require_department_km(user_id, departments, mode="any"|"all")` 即查此表判斷呼叫者是否具備列出部門（任一或全部）的 KM 角色。

**「管理部門」的判斷**（`managing_departments(document_id)`）：一個部門對某文件是否具備管理權限，取決於它在 `document_acl` 的 effect 是否為 `'detail'`——上傳時列出的部門會被寫入 `detail`；之後才透過分享加入的部門則是 `'summary'`（僅供檢視，無管理權）。`require_document_km` 即以「文件目前所有 `detail` 部門」的聯集做 KM 檢查。

### 舊版相容 bypass

ACL 管理與刪除端點另外接受 `X-Acl-Secret: <ACL_ADMIN_SECRET>` header（`get_current_user_id_or_admin_secret`），驗證通過則完全略過逐一 KM 檢查——此為 JWT 導入前的舊行為，暫時保留，之後會移除。

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
