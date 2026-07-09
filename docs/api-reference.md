# API 參考手冊

各服務的完整 API 規格。互動式文件可在服務啟動後訪問 `/docs`（Swagger UI）。

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
  "document_ids": ["xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"],
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
| `document_ids` | UUID string[] | — | 限定搜尋文件範圍；省略則搜尋所有有權限文件 |
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
  "document_ids_used": ["xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"],
  "access": { "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx": "detail" },
  "routing": {
    "enabled": true,
    "profile": "text_focus",
    "weights": { "text": 1.5, "table_dense": 1.0 }
  },
  "hits": [
    {
      "rank": 1,
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

### POST /v1/answer

單文件 RAG 問答，套用 ACL（需 detail 層級）。

**Request Body**

```json
{
  "document_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
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
  "context": "[1] document_id=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx, chunk_index=42\n...",
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

> **注意**：`readyz` 的 `ollama` 檢查實際上是在檢查 `LITELLM_BASE_URL`（外部 LiteLLM）的連通性，欄位名稱維持向後相容。

---

### MCP 工具（/mcp）

| 工具 | 說明 |
|------|------|
| `rag_answer` | 單文件 RAG 問答（ACL 強制執行） |
| `rag_search` | 跨文件搜尋（ACL 強制執行） |
| `rag_gen_check` | 答案一致性驗證（幻覺偵測） |

---

## document-api（Port 8765）

文件管理入口與 ACL 設定。

---

### POST /v1/documents/upload

上傳 PDF，自動觸發 ingest-worker 全流程。

**Request**：`multipart/form-data`

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| `file` | File | ✓ | PDF 檔案 |
| `document_id` | UUID string | — | 省略 → 建立新文件（伺服器生成新 UUID）；帶既有 document_id → 該文件的新版本 |
| `title` | string | — | 文件標題 |

**Response**

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "document_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "status": "submitted"
}
```

---

### GET /v1/documents/job/{job_id}

查詢 ingest 進度。`status` 可能值：`pending` | `running` | `done` | `failed`

---

### GET /v1/documents/

列出所有文件。

### GET /v1/documents/{document_id}

取得單一文件 metadata。`document_id` 須為合法 UUID，否則回傳 400；不存在則回傳 404。

### DELETE /v1/documents/{document_id}

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
  "document_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "source_path": "deptA/IT-OT_Policy.pdf",
  "title": "IT-OT Network Policy"
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

> PDF → Markdown（Marker）由**外部 LiteLLM** 的 `marker/pdf-to-md` 模型路由提供
> （由 firdi-litellm 平台承載，非本平台部署的服務）。ingest-worker 透過
> `LITELLM_PROXY_URL` 以 OpenAI-compatible `/v1/chat/completions` 格式呼叫。

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
| 500 | 伺服器內部錯誤（含 LLM timeout） |
