# 內部邏輯說明

各核心模組的實作細節、資料流與設計決策。

---

## 1. PDF Ingest Pipeline

### 1.1 整體資料流

```
PDF
 │
 ▼ [Marker CLI — marker_runner.py]
Markdown (.md)
 │
 ▼ [chunk_builder.py → md_semantic_chunk_plus.py]
chunk_block.json
 │
 ▼ [ingest.py]
PostgreSQL text_chunks / table_chunks / image_chunks / summary_chunks
```

---

### 1.2 Marker 階段（marker_runner.py）

**呼叫鏈**：ingest-worker → litellm-proxy:4000 → marker-service:8766（OpenAI format）

**輸出目錄命名邏輯**：

```
PDF 路徑：/data/uploads/{job_id}/document.pdf
Marker 輸出：/data/uploads/{job_id}/document/
重命名為：  /data/uploads/{job_id}/{doc_id}/
複製為：    /data/uploads/{job_id}/{doc_id}/raw.md
```

**模型快取**：Surya OCR 模型從 host 掛載唯讀：
```yaml
volumes:
  - ${HOME}/.cache/datalab/models:/datalab_cache:ro
environment:
  MODEL_CACHE_DIR: /datalab_cache
```

---

### 1.3 語意切塊（chunk_builder.py + md_semantic_chunk_plus.py）

| block_type | 來源 | embed_text 組成 |
|------------|------|-----------------|
| `text` | Markdown 段落 | heading_path + 段落文字 |
| `table` | Markdown 表格 | table_title + 欄位名稱 + row 摘要 |
| `image` | `![]()` 語法 | alt text + LLM 生成 caption |

**chunk_block.json 格式**：

```json
{
  "version": "2.0",
  "doc": {
    "tenant_id": "firdi",
    "document_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "doc_id": "deptA_policy_v1",
    "source_path": "deptA/IT-OT_Policy.pdf",
    "content_sha256": "sha256hex..."
  },
  "blocks": [
    {
      "block_type": "text",
      "chunk_index": 0,
      "embed_text": "1. 政策 > 1.1 適用範圍\n本政策適用於...",
      "payload": { "content": "本政策適用於..." }
    }
  ]
}
```

---

### 1.4 向量嵌入與 DB 寫入（ingest.py）

**嵌入呼叫（OpenAI format）**：

```python
# POST {LITELLM_BASE_URL}/v1/embeddings
{
  "model": "Qwen3-Embedding-8B",
  "input": embed_text
}
# response: {"data": [{"embedding": [...]}]}
```

`LITELLM_BASE_URL` 指向 nostr-proxy，nostr-proxy 透過 Nostr（Kind 2000）路由至外部 LiteLLM。

**文件版本管理**（`ensure_document_version()`）：

```sql
INSERT INTO documents (..., active_version, content_sha256)
VALUES (..., 1, 'newsha256')
ON CONFLICT (tenant_id, doc_id) DO UPDATE SET
  active_version = CASE
    WHEN documents.content_sha256 != EXCLUDED.content_sha256
      THEN documents.active_version + 1
    ELSE documents.active_version
  END,
  content_sha256 = EXCLUDED.content_sha256
```

---

## 2. 搜尋引擎（search.py）

### 2.1 Query Routing

搜尋前用 LLM 分類 query，決定各來源的權重：

```
Query
  │
  ▼ [POST {LITELLM_BASE_URL}/v1/chat/completions]
     → nostr-proxy → Kind 2002 → consumer → 外部 LiteLLM
Profile: balanced | text_focus | table_focus | lexical_focus | image_focus
  │
  ▼ ROUTING_PROFILES 權重
{
  "text":        1.30,
  "table_dense": 1.00,
  "table_lex":   1.00,
  "image_text":  0.80,
  "summary":     1.00,
}
```

Routing 失敗（LLM 逾時或格式錯誤）→ fallback `balanced`。

---

### 2.2 multi_search 完整流程

```
multi_search(query, tenant_id, user_id, doc_ids)
│
├─ Step 1: 取得 access_map
│  └─ ACLService.fetch_doc_access_for_user()
│     → {doc_id: "detail" | "summary" | "deny"}
│
├─ Step 2: 拆分 detail_ids / summary_ids（過濾 deny）
│
├─ Step 3: 嵌入 query
│  └─ embed_query(query)
│     POST {LITELLM_BASE_URL}/v1/embeddings
│     → nostr-proxy → Kind 2000 → consumer → LiteLLM
│     → qvec[4096]
│
├─ Step 4: Query Routing
│  └─ route_profile(query)
│     POST {LITELLM_BASE_URL}/v1/chat/completions
│     → nostr-proxy → Kind 2002 → consumer → LiteLLM
│     → profile, weights
│
├─ Step 5: 對 detail 文件搜尋
│  ├─ Stage 5a: Lexical 路由（FTS 預篩）
│  │  └─ route_docs_text_lexical()
│  │
│  ├─ Stage 5b: 多來源 dense 搜尋（k=200）
│  │  ├─ search_text_dense_multi(qvec)
│  │  ├─ search_table_dense_multi(qvec)
│  │  ├─ search_table_lexical_multi(query)
│  │  └─ search_image_text_dense_multi(qvec)
│  │
│  └─ Stage 5c: RRF 融合
│     └─ fuse(..., weights=profile_weights, norm="rrf")
│
├─ Step 6: 對 summary 文件搜尋
│  ├─ search_summary_dense + search_summary_lexical
│  └─ fuse(..., weights=ROUTING_PROFILES["summary_only"])
│
├─ Step 7: 合併 + 去重 + 排序
│
├─ Step 8: Reranking（可選）
│  └─ rerank_hits_http(hits, query)
│     POST {LITELLM_BASE_URL}/v1/rerank
│     → nostr-proxy → Kind 2001 → consumer → LiteLLM
│
└─ Step 9: 回傳 top_k hits
```

---

### 2.3 RRF 融合（Reciprocal Rank Fusion）

公式：第 i 名的 RRF 分數 = `1 / (60 + i)`

```python
# 融合示意
fused_score[doc1, idx5] = (1/(60+1)) * weight_text + (1/(60+1)) * weight_table
                         = 0.0164 * 1.3 + 0.0164 * 1.0 = 0.0377
```

---

### 2.4 SearchHit 資料結構

```python
@dataclass
class SearchHit:
    source: str          # "text" | "table_dense" | "table_lex" | "image_text" | "summary" | "summary_lex"
    doc_id: str          # 邏輯識別碼
    document_id: str     # DB UUID
    chunk_index: int
    score: float         # fused 後的最終分數
    content: str
    metadata: dict       # source_path, page_start, raw_score, fused_score...
```

---

## 3. ACL 系統（acl.py）

### 3.1 Principal 型別

| principal_type | principal_id | 說明 |
|---|---|---|
| `user` | UUID（用戶 ID） | 個人規則，優先順序最高 |
| `department` | 部門名稱 | 部門規則 |
| `role` | 角色名稱 | Keycloak 同步，**不用於文件 ACL** |

### 3.2 ACL 優先順序

`user(30) > department(10)`，`deny(30) > detail(20) > summary(10)`

---

## 4. RAG 問答（rag.py）

### 4.1 RAG 流程

```
question + doc_id + user_id
  │
  ├─ search(doc_id, question, top_k=10)
  │
  ├─ 格式化 context
  │  [1] doc_id=xxx, chunk_index=42
  │  內容：...
  │
  ├─ 組合 prompt
  │  System: "You are a factual assistant..."
  │  User:   "Question: {question}\n\nContext:\n{context}"
  │
  └─ POST {LITELLM_BASE_URL}/v1/chat/completions
     → nostr-proxy → Kind 2002 → consumer → LiteLLM
     → answer text（從 choices[0].message.content 解析）
```

### 4.2 系統 prompt

```
You are a factual assistant. Use ONLY the provided context to answer.
- If the context does not contain enough information, say you don't know.
- Do NOT invent citations. Do NOT use outside knowledge.
- Keep the answer concise and structured.
```

---

## 5. Nostr 通訊層

### 5.1 Kind 對應

| Kind | 操作 | 請求方向 |
|------|------|---------|
| 2000 | Embedding | proxy → relay → consumer |
| 2001 | Rerank | proxy → relay → consumer |
| 2002 | Chat | proxy → relay → consumer |
| 1000 | Reply（回覆）| consumer → relay → proxy |

### 5.2 事件格式

proxy 發出的事件（以 Kind 2000 為例）：

```json
["EVENT", {
  "id": "<sha256 of canonical event>",
  "pubkey": "<NOSTR_PUB_KEY>",
  "created_at": 1748000000,
  "kind": 2000,
  "tags": [],
  "content": "{\"model\": \"Qwen3-Embedding-8B\", \"input\": \"什麼是氮氣？\"}",
  "sig": "<schnorr signature>"
}]
```

consumer 回覆的事件（Kind 1000）：

```json
["EVENT", {
  "pubkey": "<BOT_PUBKEY>",
  "kind": 1000,
  "tags": [
    ["e", "<original event id>", "<relay url>", "reply"],
    ["p", "<original pubkey>"]
  ],
  "content": "{\"data\": [{\"embedding\": [...], \"index\": 0}]}"
}]
```

### 5.3 nostr-consumer 安全機制

consumer 收到事件後依序驗證：
1. `created_at >= start_time`（忽略啟動前的舊事件）
2. `pubkey` 在 `allowlist.json` 中
3. 事件 ID 重新計算（SHA256）驗證一致性
4. Schnorr 簽名驗證
5. 處理結果寫入 `audit.db`（`SUCCESS` / `REJECTED_*` / `LITELLM_EMPTY_REPLY`）

### 5.4 SIGTERM 處理（K8s / Docker 停止）

consumer 接收到 `SIGTERM` 或 `SIGINT` 時：

```python
def _handle_shutdown(sig, _):
    if _ws_app is not None:
        _ws_app.close()   # 解除 run_forever() 阻塞
    sys.exit(0)

signal.signal(signal.SIGTERM, _handle_shutdown)
signal.signal(signal.SIGINT,  _handle_shutdown)
```

docker-compose 設定 `stop_grace_period: 15s`，k8s 設定 `terminationGracePeriodSeconds: 15`。

---

## 6. 設定系統（config.py）

```python
settings.db.pg_dsn              # PostgreSQL 連線字串
settings.db.tenant_id           # 租戶 ID（預設 "firdi"）
settings.models.litellm_base_url  # LiteLLM / nostr-proxy URL（主要入口）
settings.models.ollama_base_url   # 向後相容保留，實際已不用於 API 呼叫
settings.models.embed_model       # 嵌入模型名稱
settings.models.rerank_model      # Reranker 模型名稱
settings.models.seg_model         # 切塊用 LLM
settings.models.embed_timeout     # 嵌入逾時秒數
settings.chunking.target_tokens   # 每個 chunk 目標 token 數
settings.tools.marker_cmd         # Marker CLI 命令模板
settings.tools.marker_timeout     # Marker 超時秒數
```

**關鍵設計**：
- `litellm_base_url` 是 docblock-core 所有 LLM 呼叫的唯一入口，指向 nostr-proxy（或可直接指向任何 OpenAI-compatible 端點）
- 所有 API 呼叫使用 OpenAI format（`/v1/embeddings`、`/v1/chat/completions`、`/v1/rerank`）
- `HF_HUB_OFFLINE=1` 強制設定，避免 container 啟動時下載模型

---

## 7. 工作目錄結構

```
/data/uploads/{job_id}/
├── deptA_policy.pdf
├── deptA_policy/
│   ├── deptA_policy.md
│   └── raw.md
├── deptA_policy.chunk_block.chunks.json
└── deptA_policy.chunk_block.json
```

---

## 8. Job 狀態機（ingest-worker）

```
[submit] → pending → running("stage: marker")
                          │
                          ▼
                    running("stage: build_chunks")
                          │
                          ▼
                    running("stage: ingest")
                          │
                          ▼
                         done

任一階段例外 → failed（detail 含完整 traceback）
```

**已知限制**：Job 狀態存於 in-memory dict，container 重啟後清空。

---

## 9. Keycloak 用戶同步

群組路徑 `/dept-A/km` 解析為：
- `("department", "dept-A")`
- `("role", "dept-A:km")`

原子替換策略：
```sql
BEGIN;
DELETE FROM user_principal WHERE tenant_id = 'firdi' AND user_id = '...';
INSERT INTO user_principal VALUES (...), (...);
COMMIT;
```

---

## 10. 資料庫索引策略

```sql
-- 向量索引（HNSW，cosine distance）
CREATE INDEX ON text_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON table_chunks USING hnsw (embedding vector_cosine_ops);

-- 全文搜尋（表格 tsvector）
lexical_vector tsvector GENERATED ALWAYS AS (
  to_tsvector('english', COALESCE(lexical_text, ''))
) STORED
```

---

## 11. 常見問題排查

### Embedding 失敗

```bash
docker logs compose-nostr-consumer-1 2>&1 | grep -E "Embedding|❌"
```

可能原因：
- `LITELLM_API_KEY` 未設定 → 401
- 模型名稱不符 → 400
- Nostr relay 連線中斷 → consumer 重連後自動恢復

### 搜尋無結果

```sql
SELECT * FROM document_acl WHERE document_id = '...';
SELECT * FROM user_principal WHERE user_id = '...';
SELECT d.active_version, COUNT(t.id) FROM documents d
JOIN text_chunks t ON t.document_id = d.document_id AND t.version = d.active_version
WHERE d.doc_id = '...' GROUP BY 1;
```

### Marker 逾時

```bash
# 在 .env 增加
MARKER_TIMEOUT=3600
```

### nostr-proxy chat 500 逾時

```bash
# 確認 consumer 有處理事件且 LiteLLM 有回應
docker logs compose-nostr-consumer-1 2>&1 | grep -E "💬|Chat|✅|❌" | tail -10

# 確認 consumer 的 LITELLM_BASE_URL 指向有 chat 模型的外部 LiteLLM
docker exec compose-nostr-consumer-1 env | grep LITELLM_BASE_URL
```
