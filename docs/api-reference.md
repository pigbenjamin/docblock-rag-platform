# API 參考手冊

各服務的完整 API 規格。互動式文件可在服務啟動後訪問 `/docs`（Swagger UI）。

---

## nostr-proxy（Port 8800）

OpenAI-compatible HTTP 入口，將請求轉換為 Nostr 事件，透過 relay 送至 nostr-consumer 處理。

---

### POST /v1/embeddings

向量嵌入（OpenAI format）。

**Request Body**

```json
{
  "model": "Qwen3-Embedding-8B",
  "input": "什麼是氮氣？"
}
```

**Response**

```json
{
  "object": "list",
  "data": [
    {
      "object": "embedding",
      "index": 0,
      "embedding": [0.0076, 0.0418, -0.0102, ...]
    }
  ]
}
```

`embedding` 維度視模型而定（Qwen3-Embedding-8B 為 4096）。

---

### POST /v1/rerank

文件重排序（OpenAI-compatible format）。

**Request Body**

```json
{
  "model": "Qwen3-Reranker-8B",
  "query": "什麼是氮氣？",
  "documents": [
    "氮氣是大氣中含量最多的氣體，約佔 78%。",
    "氧氣用於呼吸和燃燒。"
  ]
}
```

**Response**

```json
{
  "results": [
    { "index": 0, "relevance_score": 0.9952 },
    { "index": 1, "relevance_score": 0.0049 }
  ]
}
```

`results` 依 `relevance_score` 降冪排序，`index` 對應原始 `documents` 陣列索引。

---

### POST /v1/chat/completions

聊天補全（OpenAI format）。

**Request Body**

```json
{
  "model": "qwen3:8b",
  "messages": [
    { "role": "system", "content": "You are a helpful assistant." },
    { "role": "user",   "content": "What is nitrogen?" }
  ],
  "stream": false
}
```

**Response**

```json
{
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": "Nitrogen is a colorless, odorless chemical element..."
      }
    }
  ],
  "usage": { "prompt_tokens": 24, "completion_tokens": 42, "total_tokens": 66 }
}
```

---

### POST /api/embeddings（Legacy）

Ollama-compatible embedding endpoint，內部自動轉換為 OpenAI format 後走 Nostr 路徑。

**Request Body**（Ollama 格式）

```json
{
  "model": "Qwen3-Embedding-8B",
  "prompt": "什麼是氮氣？"
}
```

**Response**：與 `/v1/embeddings` 相同（OpenAI format）。

---

### POST /api/chat（Legacy pass-through）

直接透傳至 `OLLAMA_DIRECT_URL/api/chat`，不走 Nostr。

### POST /api/generate（Legacy pass-through）

直接透傳至 `OLLAMA_DIRECT_URL/api/generate`，不走 Nostr。

---

### GET /health

```json
{ "status": "ok" }
```

---

### 環境變數（nostr-proxy）

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `RELAY_URL` | — | Nostr relay WebSocket URL |
| `NOSTR_PRIV_KEY` | — | 64-char hex 私鑰 |
| `NOSTR_PUB_KEY` | — | 64-char hex 公鑰（需在 consumer allowlist） |
| `OLLAMA_DIRECT_URL` | — | fallback 直連 URL |
| `EMBED_VIA_NOSTR` | `true` | 是否透過 Nostr 路由 embedding |
| `RERANK_VIA_NOSTR` | `true` | 是否透過 Nostr 路由 rerank |
| `CHAT_VIA_NOSTR` | `true` | 是否透過 Nostr 路由 chat |

---

## retrieve-api（Port 8761）

語意搜尋與 RAG 問答服務。

---

### POST /v1/search

跨文件語意搜尋，套用 ACL 過濾。

**Request Body**

```json
{
  "query": "IT/OT 網路隔離政策",
  "user_id": "11111111-0001-0001-0001-000000000001",
  "doc_ids": ["doc_001", "doc_002"],
  "top_k": 10,
  "top_k_per_doc": 20,
  "routing": true,
  "router_model": "qwen3:8b",
  "enable_table_lex": true,
  "preview_chars": 400,
  "max_docs": 5000
}
```

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| `query` | string | ✓ | 搜尋問題或關鍵字 |
| `user_id` | UUID string | ✓ | 用戶 ID，用於 ACL 查詢 |
| `doc_ids` | string[] | — | 限定搜尋文件範圍；省略則搜尋所有有權限文件 |
| `top_k` | int | — | 回傳筆數（預設 10） |
| `top_k_per_doc` | int | — | 每份文件最多筆數（預設 20） |
| `routing` | bool | — | 是否啟用 query routing（預設 true） |
| `router_model` | string | — | 路由用 LLM 模型名稱 |
| `enable_table_lex` | bool | — | 是否啟用表格 BM25 搜尋（預設 true） |

**Response**

```json
{
  "query": "IT/OT 網路隔離政策",
  "user": {
    "user_id": "11111111-0001-0001-0001-000000000001",
    "principals": [
      ["user", "11111111-0001-0001-0001-000000000001"],
      ["department", "dept-A"]
    ]
  },
  "doc_ids_used": ["doc_001"],
  "access": { "doc_001": "detail", "doc_002": "deny" },
  "routing": {
    "enabled": true,
    "profile": "text_focus",
    "weights": { "text": 1.5, "table_dense": 1.0 }
  },
  "hits": [
    {
      "rank": 1,
      "doc_id": "doc_001",
      "document_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "source": "text",
      "score": 0.9240,
      "chunk_index": 42,
      "page_start": 10,
      "page_end": 11,
      "heading_path": ["Chapter 3", "3.2 網路隔離政策"],
      "preview": "IT/OT 網路隔離政策規定所有工業控制系統須...",
      "content": "（完整 chunk 內容）",
      "metadata": { "raw_score": 0.88, "fused_score": 0.9240 }
    }
  ]
}
```

`source` 可能值：`text` | `table_dense` | `table_lex` | `image_text` | `summary` | `summary_lex`

---

### POST /v1/search/open

跨文件搜尋，**不套用 ACL**（管理員用途）。

```json
{
  "query": "...",
  "doc_ids": ["doc_001"],
  "top_k": 10,
  "routing": true,
  "rerank": true,
  "rerank_model": "Qwen3-Reranker-8B"
}
```

| 額外欄位 | 說明 |
|----------|------|
| `rerank` | 是否啟用重排序（預設 false） |
| `rerank_model` | Reranker 模型名稱（透過 nostr-proxy /v1/rerank） |

---

### POST /v1/answer

單文件 RAG 問答，套用 ACL（需 detail 層級）。

**Request Body**

```json
{
  "doc_id": "deptA_IT-OT_Network_Policy",
  "question": "IT/OT 網路隔離的主要規定有哪些？",
  "user_id": "11111111-0001-0001-0001-000000000001",
  "top_k": 10,
  "routing": true
}
```

**Response**

```json
{
  "answer": "根據 IT/OT 網路隔離政策，主要規定包括：[1] 工業控制系統須與辦公室網路實體隔離...",
  "hits": [ /* 同 /v1/search 的 hits 格式 */ ],
  "context": "[1] doc_id=doc_001, chunk_index=42\n...",
  "model": "qwen3:8b",
  "usage": { "prompt_tokens": 512, "completion_tokens": 128, "total_tokens": 640 }
}
```

---

### GET /healthz

```json
{ "status": "ok" }
```

### GET /readyz

```json
{ "status": "ready" }
```

或（未就緒）：

```json
{ "status": "not_ready", "db": "connection refused", "ollama": "timeout" }
```

> **注意**：`readyz` 的 `ollama` 檢查現在實際上是在檢查 `LITELLM_BASE_URL`（nostr-proxy）的連通性，欄位名稱維持向後相容。

---

### MCP 工具（/mcp）

| 工具 | 說明 |
|------|------|
| `rag_answer` | 單文件 RAG 問答（ACL 強制執行） |
| `rag_search` | 跨文件搜尋（ACL 強制執行） |
| `rag_search_open` | 跨文件搜尋（略過 ACL） |
| `rag_search_open_hits_string` | 同上，回傳純文字 |
| `rag_gen_check` | 答案一致性驗證（幻覺偵測） |

---

## admin-api（Port 8765）

文件管理入口與 ACL 設定。

---

### POST /v1/documents/upload

上傳 PDF，自動觸發 ingest-worker 全流程。

**Request**：`multipart/form-data`

| 欄位 | 類型 | 必填 |
|------|------|------|
| `file` | File | ✓ |
| `doc_id` | string | ✓ |
| `title` | string | — |

**Response**

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "doc_id": "deptA_policy_v1",
  "status": "submitted"
}
```

---

### GET /v1/documents/job/{job_id}

查詢 ingest 進度。`status` 可能值：`pending` | `running` | `done` | `failed`

---

### GET /v1/documents/

列出所有文件。

### GET /v1/documents/{doc_id}

取得單一文件 metadata。

### DELETE /v1/documents/{doc_id}

刪除文件及所有 chunks 與 ACL（CASCADE）。

---

### POST /v1/acl/write-map

**Headers**：`X-Acl-Secret: <ACL_ADMIN_SECRET>`

```json
{
  "document_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "access_rules": [
    { "principal_type": "department", "principal_id": "dept-A", "effect": "detail" },
    { "principal_type": "user", "principal_id": "11111111-...", "effect": "deny" }
  ]
}
```

### POST /v1/acl/delete-map

**Headers**：`X-Acl-Secret: <ACL_ADMIN_SECRET>`

```json
{
  "document_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "principals": ["user:11111111-...", "department:dept-B"]
}
```

---

## ingest-worker（Port 8762）

PDF ingest pipeline，各端點背景執行並立即回傳 job_id。

---

### POST /jobs/pipeline（推薦）

全流程：PDF → Markdown → chunk_block.json → PostgreSQL。

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "pdf_path": "/data/uploads/550e8400.../document.pdf",
  "work_dir": "/data/uploads/550e8400...",
  "doc_id": "deptA_policy_v1",
  "document_id": null,
  "source_path": "deptA/IT-OT_Policy.pdf"
}
```

### POST /jobs/marker

只執行 PDF → Markdown 階段。

### POST /jobs/build-chunks

只執行 Markdown → chunk_block.json 階段。

### POST /jobs/ingest

只執行 chunk_block.json → PostgreSQL 階段。

### GET /jobs/{job_id}

查詢 job 狀態（`pending` | `running` | `done` | `failed`）。

> **注意**：job 狀態存於 in-memory dict，container 重啟後清空。

---

## marker-service（Port 8766）

PDF → Markdown 轉換服務（GPU）。

---

### POST /v1/convert

直接呼叫 marker 進行轉換（同步）。

```json
{
  "pdf_path": "/data/uploads/.../document.pdf",
  "doc_id": "deptA_policy_v1",
  "out_dir": "/data/uploads/...",
  "job_id": "marker-abc123"
}
```

**Response**：`{ "md_path": "/data/.../raw.md", "elapsed": 12.5 }`

---

### POST /v1/chat/completions

OpenAI-compatible endpoint，供 litellm-proxy 路由 `marker/pdf-to-md` 使用。

---

## litellm-proxy（Port 4000）

LLM 路由 proxy，**目前僅路由 PDF → Markdown**，不處理 embedding/rerank/chat（這些由 nostr-consumer 直接呼叫外部 LiteLLM）。

設定檔：`deployments/compose/litellm_config.yaml`

```yaml
model_list:
  - model_name: marker/pdf-to-md
    litellm_params:
      model: openai/marker-pdf-to-md
      api_base: http://marker-service:8766/v1
      api_key: "none"
general_settings:
  master_key: "sk-litellm-internal"
```

**Headers**：`Authorization: Bearer sk-litellm-internal`

---

## webhook-service（Port 8763）

接收 Keycloak 用戶事件，同步 `user_principal` 表。

### POST /keycloak/user-sync

**Headers**：`X-Webhook-Secret: <WEBHOOK_SECRET>`

```json
{ "event": "USER_UPDATE", "user_id": "47f097e7-..." }
```

---

## 錯誤格式

所有服務統一使用 FastAPI 預設格式：

```json
{ "detail": "錯誤訊息" }
```

| 代碼 | 說明 |
|------|------|
| 200 | 成功 |
| 403 | 權限不足（ACL 拒絕） |
| 404 | 資源不存在 |
| 422 | 請求格式錯誤 |
| 500 | 伺服器內部錯誤（含 Nostr timeout） |
