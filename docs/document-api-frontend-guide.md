# Document API 前端整合指南

> 目標讀者：要串接文件上傳/管理功能的前端開發者。
> 本文只講「前端需要知道的規則與流程」；完整端點規格見 [api-reference.md](api-reference.md)。

---

## 1. 服務位址與 CORS

| 項目 | 值 |
|------|-----|
| Base URL（k8s dev） | `http://10.90.20.55:31765` |
| API prefix | `/v1` |
| 健康檢查 | `GET /healthz` |

**CORS 目前是全關狀態**：`ALLOWED_ORIGINS` 環境變數為空，任何瀏覽器跨網域請求都會被擋。前端網域定案後，需將網域（含 scheme 與 port，如 `http://10.90.20.55:5173`）填入 `deployments/k8s/02-configmap.yaml` 的 `ALLOWED_ORIGINS`（逗號分隔可多個）並重啟 document-api / retrieve-api pod。**在那之前，瀏覽器端的呼叫一律會失敗，這不是 bug。**

---

## 2. 認證（前端必讀）

### 2.1 正式方式：Keycloak OIDC + Bearer token

1. 前端引導使用者到 Keycloak 登入（realm：`FIRDI-AI-Platform`，**大小寫敏感**）
2. 登入成功拿到 access token
3. 之後每個需要身份的 API 呼叫都帶 header：

```
Authorization: Bearer <access_token>
```

document-api 會本地驗簽（JWKS），從 token 的 `sub` 取得 user_id，不需要前端額外傳任何身份欄位。

> **尚未就緒的前置作業**：目前 Keycloak 只有後端用的 confidential client（`user-sync-service`），**還沒有建立給前端用的 public client**（含 PKCE 與 redirect URI 設定）。前端開始串 OIDC 登入之前，需要先在 Keycloak 建立這個 client。

### 2.2 過渡期方式：`X-User-Id` header（將移除）

開發測試期間，可以不帶 Authorization、改帶 `X-User-Id: <keycloak user uuid>`，後端行為與 JWT 路徑一致（同樣查 DB 做權限檢查），只是身份未經驗證。**正式前端一律用 Bearer token，這個 fallback 之後會拿掉，不要依賴它設計。**

### 2.3 token 過期處理

access token 過期後 API 回 401（`detail` 內含 "invalid token"）。前端應攔截 401 → 用 refresh token 換新 access token 重試，或引導重新登入。

---

## 3. 權限模型：誰能做什麼

權限**不看 token 裡的 role claim**，而是查後端資料庫（`user_principal` 表，由 webhook-service 從 Keycloak group 成員關係同步過來）。前端無法也不需要自行判斷權限——直接呼叫，後端會回 403。

### 3.1 KM 角色

Keycloak Groups 結構：每個部門（`A`/`B`/`C`）底下有 `Dev`/`KM`/`User` 三個子群組。
**`/{部門}/KM` 的成員 = 該部門的 Knowledge Manager**，可以上傳/管理該部門的文件。KM 是部門範圍的：`/A/KM` 管不到部門 B 的文件。

### 3.2 動作 × 權限對照表

| 動作 | 端點 | 需要的權限 |
|------|------|-----------|
| 上傳文件 | `POST /v1/documents/upload` | 列出的 `departments` 中**至少一個**部門的 KM |
| 上傳新版本 | 同上（帶既有 `document_id`） | 同上 |
| 刪除文件 | `DELETE /v1/documents/{id}` | 該文件**管理部門**之一的 KM |
| 查看文件 ACL | `GET /v1/acl/{id}` | 該文件管理部門之一的 KM |
| 修改/分享 ACL | `POST /v1/acl/write-map` | 該文件管理部門之一的 KM |
| 移除 ACL 規則 | `POST /v1/acl/delete-map` | 該文件管理部門之一的 KM |
| 列出文件 | `GET /v1/documents/` | （目前無需認證，見 §7 注意事項） |
| 查單一文件 metadata | `GET /v1/documents/{id}` | （目前無需認證） |
| 查 ingest 進度 | `GET /v1/documents/job/{id}` | （目前無需認證） |
| 列出部門 | `GET /v1/departments` | （目前無需認證） |

**「管理部門」的定義**：文件 ACL 中 `effect='detail'` 的 department。上傳時列出的部門自動成為管理部門；之後被分享進來的部門只有 `summary`（唯讀），沒有管理權。

---

## 4. 文件生命週期與版本語意

- **不帶 `document_id` 上傳** → 建立新文件，伺服器生成新 UUID 並在回應中返回
- **帶既有 `document_id` 上傳** → 該文件的新版本：內容有變 → `active_version + 1`；內容完全相同（sha256 一致）→ 版本不變
- **前端必須自己保存 document_id**：這是覆版的唯一鑰匙，系統不會用檔名去猜「這是不是同一份文件」
- 伺服器只保留每份文件**最新 5 個版本**的實體檔案，更舊的自動刪除
- 刪除文件是**硬刪除**：文件 + 所有 chunks + ACL 一起消失（CASCADE），沒有資源回收桶

---

## 5. 上傳流程（含進度輪詢）

### 5.1 發起上傳

`POST /v1/documents/upload`，`multipart/form-data`：

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| `file` | File | ✓ | 僅接受 `.pdf` + `application/pdf`，上限 100MB |
| `departments` | string，可重複 | ✓ | 至少一個。FormData 中同名欄位 append 多次：`fd.append('departments','A'); fd.append('departments','B')` |
| `title` | string | — | 文件標題 |
| `document_id` | UUID string | — | 僅在覆版時帶 |

部門值用 `GET /v1/departments` 回傳的 `name`（如 `A`），不要自己造字串。

**成功回應（200）**：

```json
{
  "job_id": "…uuid…",
  "document_id": "…uuid…",
  "filename": "test.pdf",
  "departments": ["A"],
  "status": "submitted",
  "ingest_worker_response": { "job_id": "…", "status": "pending" }
}
```

注意：200 只代表「已受理」，文件此時**還不存在**，要等 pipeline 跑完。

### 5.2 輪詢進度

`GET /v1/documents/job/{job_id}`，建議每 3–5 秒一次：

```json
{ "job_id": "…", "status": "running", "stage": "marker", "detail": "stage: marker" }
```

`status`：`pending` → `running` → `done` / `failed`
`stage` 順序：`marker`（PDF 轉檔，最耗時，大文件可能數分鐘到 30 分鐘）→ `build_chunks` → `ingest` → `acl` → `finalize_storage` → `done`

前端建議顯示 stage 對應的進度提示。`failed` 時 `detail` 內含錯誤原因（技術性 traceback，適合摘要顯示 + 提供展開）。

### 5.3 完成後

`status=done` 後呼叫 `GET /v1/documents/{document_id}` 取得完整 metadata（title、version、file_size、時間戳等），此時文件開始可被搜尋。

### 5.4 上傳自動產生的權限

上傳成功後，後端自動寫入 ACL，前端不需（也不能）在上傳時自訂：

- 每個列出的 department → `detail`（該部門成員可看內容、該部門 KM 可管理）
- 上傳者本人（user）→ `detail`

要給其他部門/使用者權限，上傳完成後另外呼叫 ACL 端點（見 §6）。

---

## 6. ACL 檢視與分享

### 6.1 effect 的意義

| effect | 對 department | 對 user |
|--------|--------------|---------|
| `detail` | 部門成員可看全文；**部門 KM 有管理權** | 可看全文 |
| `summary` | 部門成員只能看摘要，無管理權 | 只能看摘要 |
| `deny` | 明確拒絕（優先權高於部門規則） | 明確拒絕 |

優先序：user 規則 > department 規則（例如部門 A 是 detail，但某成員被單獨設 deny，該成員看不到）。

### 6.2 查詢：`GET /v1/acl/{document_id}`

回傳該文件目前所有規則，適合做「分享設定」面板的初始資料。

### 6.3 分享/修改：`POST /v1/acl/write-map`

```json
{
  "document_id": "…uuid…",
  "access_rules": [
    { "principal_type": "department", "principal_id": "B", "effect": "summary" },
    { "principal_type": "user", "principal_id": "…uuid…", "effect": "deny" }
  ]
}
```

**分享的硬規則（前端 UI 要配合）**：
- 把文件分享給新部門時，effect **只能是 `summary`**。想把新部門設成 `detail`（= 給出管理權）會被 403 擋下——管理權只在上傳時授予
- UI 建議：分享對話框中，「其他部門」的權限選項只出現「可檢視（摘要）」，不要出現「可管理」
- `principal_type` 只接受 `user` / `department`

### 6.4 移除規則：`POST /v1/acl/delete-map`

```json
{ "document_id": "…uuid…", "principals": ["department:B", "user:…uuid…"] }
```

principal 字串格式：`類型:識別值`。

---

## 7. 部門下拉選單

`GET /v1/departments` 即時查 Keycloak 頂層群組：

```json
[
  { "id": "a48e314a-…", "name": "A" },
  { "id": "ab8984ef-…", "name": "B" },
  { "id": "ce48af0b-…", "name": "C" },
  { "id": "9d435495-…", "name": "Public" }
]
```

- ACL 相關 API 用的部門值是 `name`（不是 `id`）
- **注意**：`Public` 是 Keycloak 裡的頂層群組但不是一般部門，目前後端不會過濾它——前端顯示前需要過濾，或等後端決定過濾規則（待定事項，見 §9）
- 這是純資訊端點，回傳內容不代表使用者有權限上傳到這些部門（選了沒 KM 權限的部門，送出時會 403）

---

## 8. 錯誤碼與 UI 對應建議

| 代碼 | 情境 | UI 建議 |
|------|------|---------|
| 401 | 沒帶 token / token 過期或無效 | 靜默 refresh 重試，失敗則導向登入 |
| 403 | 不具所需 KM 權限；或試圖給新部門 detail | 顯示「權限不足」，不要重試 |
| 404 | document_id / job_id 不存在 | 顯示找不到；job 404 可能是 ingest-worker 重啟且 job 非 pipeline 類型 |
| 413 | 檔案超過 100MB | **前端應在送出前先檢查檔案大小**，這個錯誤當保底 |
| 415 | 非 PDF | **前端應在檔案選擇器限制 `.pdf`**，這個錯誤當保底 |
| 422 | 缺必填欄位（如 departments） | 表單驗證問題，開發期錯誤 |
| 500 | 伺服器錯誤（含 LLM timeout） | 顯示一般性錯誤，提供重試 |
| 502 | Keycloak 連不上（departments 端點） | 顯示「暫時無法取得部門清單」 |

錯誤 body 統一為 `{ "detail": "訊息" }`（422 為 FastAPI 驗證錯誤陣列格式）。

---

## 9. 已知過渡狀態與待定事項

前端設計時要知道、但尚未定案的事：

1. **前端 Keycloak client 未建立**——串 OIDC 登入前的必要前置（§2.1）
2. **`ALLOWED_ORIGINS` 未設定**——前端網域定案後才會開 CORS（§1）
3. **`X-User-Id` fallback 將移除**——僅供過渡期測試（§2.2）
4. **讀取類端點目前無需認證**——`GET /v1/documents/`（含 title/檔名）、`GET /v1/documents/{id}`、`GET /v1/documents/job/{id}`、`GET /v1/departments` 任何人可呼叫。是否收緊尚未定案；前端不要假設「看得到清單 = 有權限看內容」（實際內容存取由 retrieve-api 按 ACL 過濾）
5. **`Public` 群組會出現在部門清單**——過濾規則待定（§7）
6. **detail/summary 內容分級可能簡化為 allow/deny**——已列入後續計畫（phase 6），屆時 §6.1 的語意會改變；分享 UI 建議先不要過度依賴「摘要」這個概念做視覺設計
