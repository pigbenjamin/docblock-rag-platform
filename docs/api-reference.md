# API 參考手冊

各服務的完整 API 規格。互動式文件可在服務啟動後訪問 `/docs`（Swagger UI）。

---

## CORS

`retrieve-api`、`document-api` 皆掛載 `CORSMiddleware`，允許的來源由 `ALLOWED_ORIGINS` 環境變數控制（逗號分隔多個網域）。目前預設為空字串——代表尚未開放任何瀏覽器跨網域存取（server-to-server 呼叫不受影響）。待前端網域定案後，需將該網域填入此環境變數，詳見〈部署指南〉。

---

## retrieve-api（Port 8761）

語意搜尋與 RAG 問答服務。

---

### POST /v1/search

跨文件語意搜尋。授權完全基於 `query` action 的 allow/deny（見 `docblock_core/authz.py`
的 node-tree 判定規則）：不帶 `document_ids` 時，先取 tenant 全部 document_id 當候選，
再用 `NodeAuthz.filter_allowed` 篩掉沒有 `query` 權限的；`document_ids` 若有帶，僅在
給定範圍內篩選。呼叫端本身不做任何身分驗證（`user_id` 由呼叫端提供），部署時應放在
受信任的服務網路內，不要直接暴露給瀏覽器。

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
| `document_ids` | UUID string[] | — | 限定搜尋文件範圍；省略則搜尋 tenant 內所有有權限文件 |
| `top_k` | int | — | 回傳筆數（預設 10） |
| `top_k_per_doc` | int | — | 每份文件最多筆數（預設 20） |
| `routing` | bool | — | 是否啟用 query routing（預設 true） |
| `router_model` | string | — | 路由用 LLM 模型名稱 |
| `enable_table_lex` | bool | — | 是否啟用表格 BM25 搜尋（預設 true） |
| `max_docs` | int | — | 無 `document_ids` 時，候選文件數上限（預設 5000） |

**Response**

```json
{
  "query": "IT/OT 網路隔離政策",
  "user_id": "11111111-0001-0001-0001-000000000001",
  "document_ids_used": ["xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"],
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

`document_ids_used` = 通過 ACL 篩選、實際拿去搜尋的文件（沒出現在裡面的 = 被 deny 或不存在）。
`source` 可能值：`text` | `table_dense` | `table_lex` | `image_text`（`summary`/`summary_lex`
已隨摘要分級授權一起移除）。回應**只給 `document_id`（UUID）**，不含檔名/路徑——顯示名稱要另外呼叫
document-api 的 `GET /v1/nodes` 或 `GET /v1/documents/{id}`（兩者都會依 `browse` 權限過濾，天然滿足
「query 權限不代表可以看到文件名稱」這條規則，retrieve-api 不需要另外做匿名化）。

---

### POST /v1/answer

單文件 RAG 問答，需要對該文件有 `query` 權限。

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

節點不存在 → 404；存在但無 `query` 權限 → 403（`detail` 內含 `ACL_DENY: ...`）。

**Response**

```json
{
  "answer": "根據 IT/OT 網路隔離政策，主要規定包括：[1] 工業控制系統須與辦公室網路實體隔離...",
  "hits": [ /* 同 /v1/search 的 hits 格式 */ ],
  "context": "[1] document_id=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx, chunk_index=42\n...",
  "citations": [
    { "index": 1, "document_id": "xxxxxxxx-...", "source": "text", "chunk_index": 42, "page_start": 10, "page_end": 11 }
  ],
  "model": "qwen3:8b",
  "user_id": "11111111-0001-0001-0001-000000000001",
  "document_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
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
| `rag_answer` | 單文件 RAG 問答（`query` 權限強制執行） |
| `rag_search` | 跨文件搜尋（`query` 權限強制執行） |
| `rag_gen_check` | 答案一致性驗證（幻覺偵測） |

---

## document-api（Port 8765）

文件/目錄樹管理入口。前端心智模型是 file-browser 風格的資料夾樹（`nodes` 表：
folder/document 兩種節點），權限掛在節點上，預設沿資料夾樹向下繼承（見〈架構〉文件與
`docblock_core/authz.py`）。document 節點的 id **就是** `document_id`（同一個 UUID），
不是另外一個 node_id。

**身份驗證**（除了 `DELETE /v1/documents/{id}` 額外支援 admin bypass 外，全部端點擇一）

| Header | 說明 |
|--------|------|
| `Authorization: Bearer <token>` | 建議方式。Keycloak access token，於 document-api 本地驗簽（JWKS） |
| `X-User-Id: <uuid>` | 舊版相容 fallback，待前端/測試全面遷移至 JWT 後移除 |

兩者皆缺 → 401。所有讀取端點（含列表）現在也要求身分，並依有效 `browse` 權限過濾——
沒有權限的節點/文件不會出現在清單裡，單一查詢會回 403/404。

---

### POST /v1/folders

建立資料夾。呼叫者需要對 `parent_id` 有 `upload` 權限（部門根資料夾的 owner 部門 KM
自動具備）。根資料夾（部門根）不透過這支 API 建立，由 `scripts/migrate_fb1_nodes_acl.sql`
依 Keycloak 部門群組產生。

**Request Body**

```json
{
  "parent_id": "xxxxxxxx-...",
  "name": "子資料夾",
  "owner_department_id": "A",
  "inherit_acl": true,
  "acl": [
    { "subject_type": "department", "subject_id": "B", "actions": ["browse", "query", "read"], "effect": "allow" }
  ]
}
```

| 欄位 | 必填 | 說明 |
|------|------|------|
| `parent_id` | ✓ | 父資料夾 node_id |
| `name` | ✓ | 顯示名稱，同一資料夾下需唯一（衝突回 409） |
| `owner_department_id` | — | 預設管理部門；省略則沿用父資料夾的 owner |
| `inherit_acl` | — | 本節點沒有自己的規則時，是否繼續往上層找（預設 true） |
| `acl` | — | 額外的初始 ACL entries（選填） |

**Response**：`{ "node_id", "name", "parent_id", "owner_department_id", "inherit_acl" }`

---

### GET /v1/nodes?parent_id={id}

列出資料夾的直接子節點（省略 `parent_id` = 列出根層級），依 `browse` 權限過濾。每個項目
附 `permissions` map（`browse`/`query`/`read`/`upload`/`update`/`delete`/`move`/`manage_acl`）
方便前端做按鈕顯示判斷——伺服器仍會在每次實際操作時重新檢查一次。

**Response**

```json
{
  "parent_id": "xxxxxxxx-...",
  "items": [
    {
      "node_id": "yyyyyyyy-...",
      "node_type": "document",
      "name": "IT-OT_Policy.pdf",
      "owner_department_id": "A",
      "permissions": { "browse": true, "query": true, "read": true, "upload": false, "update": false, "delete": false, "move": false, "manage_acl": false },
      "updated_at": "2026-07-14T09:00:00Z",
      "document_id": "yyyyyyyy-...",
      "status": "ready",
      "active_version": 1,
      "file_size": 245678
    }
  ]
}
```

### GET /v1/nodes/{node_id}

單一節點詳細資料（需要 `browse`）。回傳欄位同上一項的單一 item，另加 `permission_revision`。

### PATCH /v1/nodes/{node_id}

改名（需要 `update`）。只改 `nodes.name`，不影響 document_id、storage、chunks。

```json
{ "name": "新名稱" }
```

### POST /v1/nodes/{node_id}/move

搬移節點（需要來源節點的 `move` + 目標資料夾的 `upload`）。禁止搬進自己的子樹（400）。
Storage 檔案與向量不會搬動——路徑不承載授權意義。

```json
{ "new_parent_id": "xxxxxxxx-..." }
```

### DELETE /v1/nodes/{node_id}

硬刪除（需要 `delete`）：節點本身、整個子樹、底下所有文件的 chunks/ACL 一併消失
（FK CASCADE），沒有回收桶。根資料夾不可刪除（400）。

---

### GET /v1/nodes/{node_id}/acl

讀取節點自己的 ACL entries（不含繼承來的規則）。需要 `manage_acl`（owner 部門 KM 自動具備）。

**Response**

```json
{
  "node_id": "xxxxxxxx-...",
  "owner_department_id": "A",
  "inherit_acl": true,
  "permission_revision": 3,
  "entries": [
    { "subject_type": "department", "subject_id": "B", "effect": "allow", "inherit_to_children": true, "actions": ["browse", "query", "read"] }
  ]
}
```

### PUT /v1/nodes/{node_id}/acl

**整批取代**節點自己的 entries（取代舊版 `POST /v1/acl/write-map`/`delete-map`）。需要
`manage_acl`。可選帶 `If-Match: "<permission_revision>"` 做 optimistic locking——版本不符回
409，避免兩位管理者互相覆蓋。

```json
{
  "inherit_acl": true,
  "entries": [
    { "subject_type": "department", "subject_id": "B", "actions": ["browse", "query", "read"], "effect": "allow" },
    { "subject_type": "user", "subject_id": "11111111-...", "actions": ["browse", "query", "read"], "effect": "deny" }
  ]
}
```

`subject_type` 僅接受 `user` / `department`（`role` 是 `user_principal` 的用戶屬性，用於
owner-KM 判定，不會寫進節點 ACL）。`effect` 僅 `allow` / `deny`（detail/summary 分級已隨
D5 拿掉）。授予 `manage_acl` action = 把共同管理權交給該 subject；只給
`browse`/`query`/`read` =單純分享，不含管理權。

**Response**：`{ "node_id", "permission_revision", "inherit_acl" }`

---

### POST /v1/documents/upload

上傳 PDF，自動觸發 ingest-worker 全流程。文件的 node 在呼叫 ingest-worker **之前**就已
建立（`inherit_acl=true`），所以新文件預設直接繼承 `parent_folder_id` 的 ACL，不需要另外
設定就能被同部門成員查詢。

**Request**：`multipart/form-data`

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| `file` | File | ✓ | PDF 檔案。僅接受 `.pdf` 副檔名 + `application/pdf` content-type，否則回傳 415；大小上限 100MB，超過回傳 413 |
| `document_id` | UUID string | — | 省略 → 建立新文件（伺服器生成新 UUID，同時作為 node id）；帶既有 document_id → 該文件的新版本（`parent_folder_id` 會被忽略，文件留在原位置） |
| `parent_folder_id` | UUID string | 新文件必填 | 文件要建在哪個資料夾下；呼叫者需要對它有 `upload` 權限 |
| `title` | string | — | 文件標題（同時作為節點顯示名稱，省略則用原始檔名） |
| `owner_department_id` | string | — | 僅新文件；預設管理部門，省略則沿用 `parent_folder_id` 的 owner |
| `acl` | JSON string | — | 僅新文件；額外的初始 ACL entries（選填），格式同 `POST /v1/folders` 的 `acl` 欄位 |

> **注意（未來規劃）**：目前 pipeline 只處理 PDF（Marker OCR）。Office 格式（docx/xlsx/pptx）尚未支援——`ingest_jobs.source_type` 欄位雖存在，但目前恆為 `'pdf'`，尚無對應的轉檔路由邏輯。

**Response**

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "document_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "filename": "IT-OT_Policy.pdf",
  "status": "submitted",
  "ingest_worker_response": { "job_id": "550e8400-e29b-41d4-a716-446655440000", "status": "pending" }
}
```

---

### GET /v1/documents/job/{job_id}

查詢 ingest 進度（需要對該 job 對應文件的 `browse` 權限）。`status` 可能值：
`pending` | `running` | `done` | `failed`。job 失敗時，document-api 會自動清掉上傳當下
建立、但從未真正產出內容的 placeholder node，避免資料夾樹裡留下永遠卡在
「processing」的空節點。

---

### GET /v1/documents/

列出使用者有 `browse` 權限的文件（依有效權限過濾；過濾發生在分頁**之後**，回應筆數可能
少於 `limit`，這是已知的第一版限制，之後靠 `node_effective_permissions` 快取解決）。

### GET /v1/documents/{document_id}

取得單一文件 metadata（需要 `browse`）。`document_id` 須為合法 UUID，否則回傳 400；不存在
或沒有 `browse` 權限則回傳 404。

### GET /v1/documents/{document_id}/content

下載/預覽該文件目前版本的原始檔（需要 `read`，跟 `browse`/`query` 各自獨立——看得到名稱
或能被 RAG 使用，不代表能下載原始檔）。檔案未就緒（`status != 'ready'`）回 409。回應的
`Content-Disposition` 檔名用使用者上傳當下的原始檔名，不是內部 storage 路徑檔名。

### DELETE /v1/documents/{document_id}

硬刪除文件（node + 所有 chunks + ACL entries，CASCADE），沒有回收桶。需具備該文件的
`delete` 權限（owner 部門 KM 自動具備），或使用舊版 `X-Acl-Secret` admin bypass（唯一
還保留這個 bypass 的端點）。傳入資料夾 id 會被拒絕（400，提示改用
`DELETE /v1/nodes/{node_id}`）。

---

### GET /v1/departments

列出「結構上像部門」的 Keycloak 頂層群組——底下有 `KM` 子群組才算數，用來過濾掉像
`Public`（全部門可瀏覽/查詢/讀取的共用根資料夾，但不是一個真正的部門）這類群組，供前端
下拉選單使用（透過 `user-sync-service` client 即時查詢 Keycloak admin API）。

**Response**

```json
[
  { "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6", "name": "A" },
  { "id": "8f14e45f-ceea-4c9a-8b1a-000000000000", "name": "B" }
]
```

> 純資訊性質，**不參與任何授權判斷**——授權一律查詢 `user_principal` / `acl_entries`，不會查這支端點。

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

> `access_rules` 欄位已移除：ACL 現在完全由節點的資料夾繼承決定（document-api 在呼叫這支
> API 之前就已經建好繼承正確 ACL 的 node），ingest-worker 本身不再寫任何 ACL 資料。

### POST /jobs/marker

只執行 PDF → Markdown 階段。

### POST /jobs/build-chunks

只執行 Markdown → chunk_block.json 階段。

### POST /jobs/ingest

只執行 chunk_block.json → PostgreSQL 階段。

### GET /jobs/{job_id}

查詢 job 狀態（`pending` | `running` | `done` | `failed`）。

> **注意**：`/jobs/pipeline` 的 job（job_id/document_id 皆為 UUID）持久化於 `ingest_jobs` 表，container 重啟後仍可查詢；只有 `/jobs/marker` 等分階段測試端點使用非 UUID job_id 時才落在 in-memory dict、重啟後清空。

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
| 401 | 未認證（缺少 Authorization/X-User-Id，或 JWT 驗簽失敗） |
| 403 | 權限不足（節點存在但被 ACL 拒絕，或不具所需 action） |
| 404 | 節點/文件不存在，**或存在但沒有 `browse` 權限**（document-api 不區分這兩種情況，避免洩漏節點是否存在） |
| 409 | 同一資料夾下節點名稱衝突；或 `PUT /v1/nodes/{id}/acl` 帶的 `If-Match` 版號跟目前的 `permission_revision` 不符 |
| 413 | 上傳檔案超過大小限制（100MB） |
| 415 | 上傳檔案類型不受支援（僅接受 PDF） |
| 422 | 請求格式錯誤 |
| 500 | 伺服器內部錯誤（含 LLM timeout） |
| 502 | 上游服務異常（如 Keycloak admin API 無法連線或權限不足） |
