# Document API 前端整合指南

> 目標讀者：要串接文件上傳/管理功能的前端開發者。
> 本文只講「前端需要知道的規則與流程」；完整端點規格見 [api-reference.md](api-reference.md)。
>
> 前端心智模型是 **file-browser 風格的資料夾樹**：`nodes` 表把 folder/document
> 兩種節點串成一棵樹，權限掛在節點上、預設沿資料夾往下繼承。document 節點的 id
> **就是** `document_id`（同一個 UUID），不是另外一個 node_id。

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
3. 之後每個 API 呼叫都帶 header：

```
Authorization: Bearer <access_token>
```

document-api 會本地驗簽（JWKS），從 token 的 `sub` 取得 user_id，不需要前端額外傳任何身份欄位。**所有端點現在都要求身分**（含讀取/列表），沒帶 → 401。

> **尚未就緒的前置作業**：目前 Keycloak 只有後端用的 confidential client（`user-sync-service`），**還沒有建立給前端用的 public client**（含 PKCE 與 redirect URI 設定）。前端開始串 OIDC 登入之前，需要先在 Keycloak 建立這個 client。

### 2.2 過渡期方式：`X-User-Id` header（將移除）

開發測試期間，可以不帶 Authorization、改帶 `X-User-Id: <keycloak user uuid>`，後端行為與 JWT 路徑一致（同樣查 DB 做權限檢查），只是身份未經驗證。**正式前端一律用 Bearer token，這個 fallback 之後會拿掉，不要依賴它設計。**

### 2.3 token 過期處理

access token 過期後 API 回 401（`detail` 內含 "invalid token"）。前端應攔截 401 → 用 refresh token 換新 access token 重試，或引導重新登入。

---

## 3. 權限模型：誰能做什麼

權限**不看 token 裡的 role claim**，而是查後端資料庫。判斷分三層：

1. **全域管理員**（FB-6）：`global_admins` 表裡的人對所有節點自動擁有所有權限（含 Public），並負責指派各部門管理員。名單用 `GET/PUT /v1/global-admins` 讀寫；第一位由後端直接 seed 進 DB。
2. **部門管理員**（誰能管理，舊稱 KM）：查 `department_admins` 表——**不再從 Keycloak 群組結構推導**（Keycloak 與 HR 連動後部門下沒有 KM 子群組了），名單由全域管理員或該部門現任管理員透過 `GET/PUT /v1/departments/{department}/admins` 維護。一個節點的 `owner_department_id` 部門的管理員，對該節點自動擁有所有權限（含底下整個子樹）——這是「防鎖死」設計：不會有一條 deny 規則能把部門管理員擋在自己部門的資料夾外面。（部門「成員」資格仍來自 `user_principal`，由 webhook-service 從 Keycloak 頂層群組同步。）
3. **節點 ACL**（誰能看/查/管理特定節點）：查 `acl_entries` 表，8 種 action 各自獨立、沿資料夾樹往下繼承，deny 優先於 allow，user 規則優先於 department 規則。詳見〈架構〉文件。

前端無法也不需要自行判斷權限——直接呼叫，後端會回 403（或 404，見 §8）。清單類端點會依 `browse` 權限自動過濾，不會回傳沒有權限看的節點。

### 3.1 八種 action

| action | 意義 |
|--------|------|
| `browse` | 能在目錄樹/清單裡看到節點名稱與基本 metadata |
| `query` | RAG 檢索能使用這份文件的內容回答問題 |
| `read` | 能預覽/下載原始檔 |
| `upload` | 能在這個資料夾底下建立子資料夾或上傳文件 |
| `update` | 能改名、更新 metadata、上傳新版本 |
| `delete` | 能刪除（硬刪除，見 §4） |
| `move` | 能把這個節點搬到別的資料夾 |
| `manage_acl` | 能讀取/修改這個節點的 ACL |

`browse`、`query`、`read` 三者互相獨立：query=true 但 browse=false 是合法狀態（RAG 可以用內容回答，但目錄樹/搜尋結果不顯示檔名——因為 retrieve-api 的回應本來就只給 document_id，不給檔名，天然滿足這個規則）。

### 3.2 動作 × 權限對照表

| 動作 | 端點 | 需要的權限 |
|------|------|-----------|
| 建立資料夾 | `POST /v1/folders` | 對 `parent_id` 有 `upload` |
| 上傳新文件 | `POST /v1/documents/upload`（不帶 document_id） | 對 `parent_folder_id` 有 `upload` |
| 上傳新版本 | 同上（帶既有 `document_id`） | 對該文件有 `update` |
| 列出目錄 | `GET /v1/nodes?parent_id=` | 依 `browse` 自動過濾 |
| 改名 | `PATCH /v1/nodes/{id}` | `update` |
| 搬移 | `POST /v1/nodes/{id}/move` | 來源 `move` + 目標 `upload` |
| 刪除 | `DELETE /v1/nodes/{id}` 或 `DELETE /v1/documents/{id}` | `delete` |
| 查看節點 ACL | `GET /v1/nodes/{id}/acl` | `manage_acl` |
| 修改節點 ACL | `PUT /v1/nodes/{id}/acl` | `manage_acl` |
| 下載原始檔 | `GET /v1/documents/{id}/content` | `read` |
| 列出文件（扁平） | `GET /v1/documents/` | 依 `browse` 自動過濾 |
| 查單一文件 metadata | `GET /v1/documents/{id}` | `browse` |
| 查 ingest 進度 | `GET /v1/documents/job/{id}` | 對應文件的 `browse` |
| 列出部門 | `GET /v1/departments` | 無需權限（純資訊端點） |
| 查部門管理員名單 | `GET /v1/departments/{department}/admins` | 無需權限（通訊錄性質） |
| 改部門管理員名單 | `PUT /v1/departments/{department}/admins` | 全域管理員，或該部門現任管理員（清空名單只有全域管理員可以） |
| 查全域管理員名單 | `GET /v1/global-admins` | 無需權限 |
| 改全域管理員名單 | `PUT /v1/global-admins` | 全域管理員（名單不得清空） |

---

## 4. 文件生命週期與版本語意

- **上傳新文件必須帶 `parent_folder_id`**（要放進哪個資料夾）——呼叫者需要對該資料夾有 `upload` 權限。文件節點建立時預設 `inherit_acl=true`，直接繼承資料夾的 ACL，不需要另外設定就能被同部門成員查詢/瀏覽。
- **帶既有 `document_id` 上傳** → 該文件的新版本：內容有變 → `active_version + 1`；內容完全相同（sha256 一致）→ 版本不變。`parent_folder_id` 在這個情況下會被忽略，文件留在原本的位置。
- **前端必須自己保存 document_id**：這是覆版的唯一鑰匙，系統不會用檔名去猜「這是不是同一份文件」
- 伺服器只保留每份文件**最新 5 個版本**的實體檔案，更舊的自動刪除
- 刪除文件是**硬刪除**：節點 + 所有 chunks + ACL 一起消失（CASCADE），沒有資源回收桶（`DELETE /v1/documents/{id}` 會拒絕收到資料夾 id，避免誤刪整個子樹）

---

## 5. 上傳流程（含進度輪詢）

### 5.1 發起上傳

`POST /v1/documents/upload`，`multipart/form-data`：

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| `file` | File | ✓ | 僅接受 `.pdf` + `application/pdf`，上限 100MB |
| `parent_folder_id` | UUID string | 新文件必填 | 文件要放進哪個資料夾，呼叫者需要對它有 `upload` 權限 |
| `document_id` | UUID string | — | 僅在覆版時帶；帶了就不需要（也會忽略）`parent_folder_id` |
| `title` | string | — | 文件標題，同時作為節點顯示名稱（省略則用原始檔名） |
| `owner_department_id` | string | — | 僅新文件；預設管理部門，省略則沿用 `parent_folder_id` 所在資料夾的 owner |
| `acl` | JSON string | — | 僅新文件；上傳當下順便給的額外 ACL entries（選填，見 §6 格式） |

資料夾的 `node_id` 從 `GET /v1/nodes?parent_id=` 的目錄樹操作中取得（例如先列出根層級找到部門資料夾，再往下鑽）。

**成功回應（200）**：

```json
{
  "job_id": "…uuid…",
  "document_id": "…uuid…",
  "filename": "test.pdf",
  "status": "submitted",
  "ingest_worker_response": { "job_id": "…", "status": "pending" }
}
```

注意：200 只代表「已受理」，文件此時**還不存在**（`documents` 表還沒有這一列），要等 pipeline 跑完。但對應的 node 已經建立，會以 `status: "processing"` 出現在目錄樹裡。

### 5.2 輪詢進度

`GET /v1/documents/job/{job_id}`，建議每 3–5 秒一次：

```json
{ "job_id": "…", "status": "running", "stage": "marker", "detail": "stage: marker" }
```

`status`：`pending` → `running` → `done` / `failed`
`stage` 順序：`marker`（PDF 轉檔，最耗時，大文件可能數分鐘到 30 分鐘）→ `build_chunks` → `ingest` → `finalize_storage` → `done`

前端建議顯示 stage 對應的進度提示。`failed` 時 `detail` 內含錯誤原因（技術性 traceback，適合摘要顯示 + 提供展開）；失敗後，剛剛建立的 placeholder node 會被後端自動清掉，目錄樹裡不會留下卡在「processing」的空節點。

### 5.3 完成後

`status=done` 後呼叫 `GET /v1/documents/{document_id}` 取得完整 metadata（title、version、file_size、時間戳等），此時文件開始可被搜尋、也可以用 `GET /v1/documents/{document_id}/content` 下載。

### 5.4 上傳自動產生的權限

新文件節點預設 `inherit_acl=true`，**直接繼承 `parent_folder_id` 資料夾的 ACL**——不需要額外呼叫任何 API，同資料夾的部門成員自動可以 browse/query/read。想在上傳當下順便給額外的分享規則，可以帶 `acl` 欄位（見下方格式）；要事後調整則呼叫 `PUT /v1/nodes/{document_id}/acl`（見 §6）。

---

## 6. 節點 ACL 檢視與分享

### 6.1 模型

`effect` 只有兩種：`allow` / `deny`（舊版 detail/summary/deny 三級分級已經拿掉——有權限就是看得到全文，沒有就完全看不到，沒有「只能看摘要」這種中間狀態）。

- **分享** = 給某個 subject `browse`/`query`/`read` 的 allow
- **共同管理** = 額外給 `manage_acl` action 的 allow——這才等同舊版「detail = 有管理權」的概念，兩者現在是分開表達的

優先序：同節點內 user 規則 > department 規則；deny 優先於 allow。找規則的順序是「先看節點自己，沒有再往上層資料夾找」，找到第一個有相符規則的節點就停。

### 6.2 查詢：`GET /v1/nodes/{document_id}/acl`

回傳該節點**自己**的規則（不含繼承來的），適合做「分享設定」面板的初始資料：

```json
{
  "node_id": "…uuid…",
  "owner_department_id": "A",
  "inherit_acl": true,
  "permission_revision": 3,
  "entries": [
    { "subject_type": "department", "subject_id": "B", "effect": "allow", "inherit_to_children": true, "actions": ["browse", "query", "read"] }
  ]
}
```

### 6.3 分享/修改：`PUT /v1/nodes/{document_id}/acl`

**整批取代**語意：這次呼叫帶的 `entries` 會完全取代節點目前的規則，不是逐條增刪。

```json
{
  "entries": [
    { "subject_type": "department", "subject_id": "B", "actions": ["browse", "query", "read"], "effect": "allow" },
    { "subject_type": "user", "subject_id": "…uuid…", "actions": ["browse", "query", "read"], "effect": "deny" }
  ]
}
```

- `subject_type` 只接受 `user` / `department`
- 想避免併發覆蓋（兩個人同時改同一份文件的分享設定），可以帶 `If-Match: "<permission_revision>"`（從 §6.2 的回應拿），版號不符會回 409，前端應該重新讀取最新設定再讓使用者重試
- UI 建議：一般「分享」對話框只出現 `browse`/`query`/`read` 的勾選；`manage_acl`（共同管理）建議獨立於進階設定或需要更高權限的操作路徑，不要跟一般分享混在一起

### 6.4 移除規則

沒有獨立的「刪除」端點——移除某條規則 = 重新 `PUT` 一份不含它的 `entries` 清單。前端可以：讀 §6.2 拿目前規則 → 使用者在 UI 上取消勾選某個 subject → 把剩下的規則整批 `PUT` 回去。

---

## 7. 目錄樹操作

### 7.1 瀏覽

`GET /v1/nodes?parent_id={folder_id}`（省略 `parent_id` = 列出根層級，也就是各部門的根資料夾 + `Public`）：

```json
{
  "parent_id": "…uuid…",
  "items": [
    {
      "node_id": "…uuid…",
      "node_type": "folder",
      "name": "RD",
      "owner_department_id": "A",
      "permissions": { "browse": true, "query": true, "read": true, "upload": true, "update": true, "delete": true, "move": true, "manage_acl": true },
      "updated_at": "2026-07-14T09:00:00Z"
    },
    {
      "node_id": "…uuid…",
      "node_type": "document",
      "name": "研發規格.pdf",
      "document_id": "…uuid…",
      "status": "ready",
      "active_version": 1,
      "file_size": 245678,
      "owner_department_id": "A",
      "permissions": { "browse": true, "query": true, "read": true, "upload": false, "update": false, "delete": false, "move": false, "manage_acl": false },
      "updated_at": "2026-07-14T09:00:00Z"
    }
  ]
}
```

`permissions` 方便前端決定要不要顯示某個按鈕（上傳、改名、刪除…），但後端在每個實際操作時都會重新檢查一次，前端不能只靠這個 map 做安全控制。

### 7.2 建資料夾 / 改名 / 搬移 / 刪除

- `POST /v1/folders`：`{ "parent_id", "name", "owner_department_id"?, "inherit_acl"?, "acl"? }`
- `PATCH /v1/nodes/{id}`：`{ "name" }`
- `POST /v1/nodes/{id}/move`：`{ "new_parent_id" }`（會拒絕搬進自己的子樹）
- `DELETE /v1/nodes/{id}`：硬刪除整個子樹（資料夾）或單一文件

同一資料夾下節點名稱需唯一，衝突回 409。根資料夾（部門根、`Public`）不能被改名/搬移/刪除。

---

## 8. 部門下拉選單與管理員名單

`GET /v1/departments` 回傳 Keycloak 的**所有頂層群組**（`Public` 除外）——FB-6 起
每個頂層群組都是部門，Keycloak 群組樹與 HR 連動、可能有多層，但子單位（處/課）在
v1 一律忽略：

```json
[
  { "id": "a48e314a-…", "name": "A" },
  { "id": "ab8984ef-…", "name": "B" },
  { "id": "ce48af0b-…", "name": "C" }
]
```

- `Public` 被明確排除，**不會**出現在這份清單裡——它是一個所有部門都能 browse/query/read 的共用根資料夾，會出現在 `GET /v1/nodes`（根層級）的目錄樹裡，但不是「部門」，不會出現在部門下拉選單
- ACL 相關 API 用的部門值是 `name`（不是 `id`）
- 這是純資訊端點，回傳內容不代表使用者有權限上傳到這些部門（選了自己無權的部門，送出時會 403）

管理員名單（FB-6 新增，「權限管理」頁面用）：

- `GET /v1/departments/{department}/admins` — 部門管理員名單，任何登入者可讀（想申請權限時知道要找誰）
- `PUT /v1/departments/{department}/admins`（body `{ "user_ids": [...] }`，整批取代）— 全域管理員或該部門現任管理員可改；部門管理員可以交棒但不能清空名單（400）
- `GET /v1/global-admins`、`PUT /v1/global-admins` — 全域管理員名單；只有全域管理員可改，且不得清空

---

## 9. 錯誤碼與 UI 對應建議

| 代碼 | 情境 | UI 建議 |
|------|------|---------|
| 401 | 沒帶 token / token 過期或無效 | 靜默 refresh 重試，失敗則導向登入 |
| 403 | 節點存在但不具所需 action 權限 | 顯示「權限不足」，不要重試 |
| 404 | 節點/文件不存在，**或存在但沒有 `browse` 權限**（後端刻意不區分，避免洩漏節點是否存在） | 顯示找不到 |
| 409 | 節點名稱在同資料夾下衝突；或 ACL 的 `If-Match` 版號過期 | 名稱衝突→提示改名；ACL 版號過期→重新讀取最新設定再讓使用者重試 |
| 413 | 檔案超過 100MB | **前端應在送出前先檢查檔案大小**，這個錯誤當保底 |
| 415 | 非 PDF | **前端應在檔案選擇器限制 `.pdf`**，這個錯誤當保底 |
| 422 | 缺必填欄位（如 `parent_folder_id`） | 表單驗證問題，開發期錯誤 |
| 500 | 伺服器錯誤（含 LLM timeout） | 顯示一般性錯誤，提供重試 |
| 502 | Keycloak 連不上（departments 端點） | 顯示「暫時無法取得部門清單」 |

錯誤 body 統一為 `{ "detail": "訊息" }`（422 為 FastAPI 驗證錯誤陣列格式）。

---

## 10. 已知過渡狀態

前端設計時要知道、但尚未定案/尚未完成的事：

1. **前端 Keycloak client 未建立**——串 OIDC 登入前的必要前置（§2.1）
2. **`ALLOWED_ORIGINS` 未設定**——前端網域定案後才會開 CORS（§1）
3. **`X-User-Id` fallback 將移除**——僅供過渡期測試（§2.2）
4. **`node_effective_permissions` 快取尚未實作**——`GET /v1/documents/` 這類扁平列表是「先分頁、再依權限過濾」，回應筆數可能少於 `limit`；`GET /v1/nodes`（樹狀瀏覽）不受影響，因為天然按資料夾分批
5. **大檔案上傳/斷點續傳（upload session + presigned URL）尚未實作**——目前 100MB 以內走一般 multipart 上傳
6. **`node_effective_permissions` 之外，資料夾搬移後的子樹重算是即時的**（沒有背景延遲），量大時可能需要之後優化
7. ~~dev 環境部門命名兩套並存~~——已於 2026-07-15 解決：dev DB 全部改用 Keycloak 頂層群組原始名稱（`A`/`B`/`C`），與 `GET /v1/departments` 回傳值、webhook 同步寫入值一致。部門值在整個系統只有一套：Keycloak 群組名
