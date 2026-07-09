# 部署指南

## 前置需求

| 項目 | 最低版本 | 說明 |
|------|----------|------|
| Docker Engine | 24+ | |
| Docker Compose | v2.20+ | `docker compose` 指令（非 `docker-compose`） |
| Python | 3.x | 僅執行測試腳本需要 |
| LiteLLM（外部） | — | **外部服務**，提供 marker / embedding / rerank / chat 模型（由 firdi-litellm 平台承載） |

---

## 一、前置準備

### 1.1 確認外部服務可達

```bash
# LiteLLM（外部）
curl http://your-litellm-host:port/v1/models \
  -H "Authorization: Bearer your-api-key"
```

---

## 二、環境設定

### 2.1 建立 `.env`

```bash
cd deployments/compose
cp .env.example .env   # 若有範本，或直接編輯 .env
```

> **正式環境以 k8s 為主**，設定維護在 `deployments/k8s/02-configmap.yaml`（非機密，進 git）
> 與 `deployments/k8s/01-secrets.yaml`（機密，已加入 `.gitignore`，不進 git）。
> 本節的 `.env` 僅供本地 Docker Compose 開發/測試使用，與 k8s 設定各自獨立維護，
> 兩邊數值需手動保持一致（無自動同步）。

完整 `.env` 欄位說明：

```dotenv
# ── PostgreSQL ──────────────────────────────────────────────────
POSTGRES_USER=ai-x
POSTGRES_PASSWORD=changeme
POSTGRES_DB=acl_FIRDI
POSTGRES_PORT=5437

# ── docblock-core 共用 ──────────────────────────────────────────
PG_DSN=postgresql://ai-x:changeme@postgres:5432/acl_FIRDI
TENANT_ID=firdi

# ── LLM 路由（所有服務直連外部 LiteLLM，OpenAI-compatible）────
LITELLM_BASE_URL=http://your-litellm-host:port
LITELLM_API_KEY=your-api-key

# 模型名稱（需與外部 LiteLLM 設定一致，見 firdi-litellm README「可用模型」）
EMBED_MODEL=embeddinggemma-300m
SEG_MODEL=gemma-4-26B-A4B-it
SUMMARY_MODEL=gemma-4-26B-A4B-it
CHAT_MODEL=gemma-4-26B-A4B-it
CAPABILITIES_MODEL=gemma-4-26B-A4B-it
# rerank 服務已自 LiteLLM 移除；留空時 rerank 失敗自動 fallback 原始排序
RERANK_MODEL=

# ── Ingest worker / Marker ──────────────────────────────────────
LITELLM_PROXY_URL=http://your-litellm-host:port   # marker/pdf-to-md 路由

# ── Document API ──────────────────────────────────────────────
ACL_ADMIN_SECRET=dev-secret-change-me
INGEST_WORKER_URL=http://ingest-worker:8762

# ── Webhook Service ─────────────────────────────────────────────
WEBHOOK_SECRET=dev-webhook-secret
# 容器內連宿主機的 Keycloak 請用 host.docker.internal；realm 名稱區分大小寫
KEYCLOAK_URL=https://your-keycloak-host:8446
KEYCLOAK_REALM=your-realm
KEYCLOAK_CLIENT_ID=user-sync-service
KEYCLOAK_CLIENT_SECRET=your-client-secret
# true / false / CA bundle 路徑（自簽憑證環境用）
KEYCLOAK_VERIFY_SSL=true

# ── HuggingFace 離線模式 ─────────────────────────────────────────
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1

# ── Logging ─────────────────────────────────────────────────────
LOG_DIR=logs
```

> **生產環境請務必更換** `POSTGRES_PASSWORD`、`ACL_ADMIN_SECRET`、`WEBHOOK_SECRET`、`KEYCLOAK_CLIENT_SECRET`。

---

## 三、Build Image

### 3.1 一次 Build 所有 image

```bash
cd deployments/compose
docker compose build
```

### 3.2 單獨 Build 指定 service

```bash
docker compose build retrieve-api
docker compose build document-api
docker compose build ingest-worker
docker compose build webhook-service
```

### 3.3 強制重新 Build

```bash
docker compose build --no-cache retrieve-api
```

### 3.4 Build 時間參考

| Service | 首次 Build | 有快取 |
|---------|-----------|--------|
| ingest-worker | ~2 分鐘 | ~15 秒 |
| retrieve-api | ~2 分鐘 | ~15 秒 |
| document-api | ~2 分鐘 | ~15 秒 |
| webhook-service | ~1 分鐘 | ~10 秒 |

---

## 四、啟動服務

### 4.1 完整啟動

```bash
cd deployments/compose
docker compose up -d
```

啟動順序由 `depends_on` 控制：
1. `postgres`（等待 healthcheck 通過）
2. `webhook-service`、`retrieve-api`（同時啟動）
3. `ingest-worker`
4. `document-api`（等待 ingest-worker 啟動）

### 4.2 確認所有服務正常

```bash
docker compose ps
```

預期狀態全為 `Up`（postgres 顯示 `healthy`）：

```
NAME                        STATUS
compose-postgres-1          Up (healthy)
compose-ingest-worker-1     Up
compose-retrieve-api-1      Up
compose-document-api-1      Up
compose-webhook-service-1   Up
```

### 4.3 健康檢查

```bash
curl http://localhost:8761/healthz        # retrieve-api
curl http://localhost:8762/healthz        # ingest-worker
curl http://localhost:8763/healthz        # webhook-service
curl http://localhost:8765/healthz        # document-api
```

或執行整合測試：

```bash
python3 tests/01_health_check.py
```

### 4.4 確認 LiteLLM 直連通暢

```bash
# 應回傳模型清單（含 embedding / rerank / chat）
curl http://your-litellm-host:port/v1/models \
  -H "Authorization: Bearer $LITELLM_API_KEY"
```

---

## 五、更新單一 Service

修改 `libs/docblock-core/` 下的程式碼時，需重建所有依賴該函式庫的 service：

```bash
docker compose build ingest-worker retrieve-api document-api webhook-service
docker compose up -d ingest-worker retrieve-api document-api webhook-service
```

> **注意：** 只修改 `.env` 中的環境變數不需 rebuild：
> ```bash
> docker compose up -d --no-build <service>
> ```

---

## 六、服務 Port 對照

| Service | 對外 Port | 用途 |
|---------|-----------|------|
| postgres | 5437 | PostgreSQL（本機直連 debug 用） |
| retrieve-api | 8761 | 搜尋 / RAG 問答 |
| ingest-worker | 8762 | PDF ingest pipeline |
| webhook-service | 8763 | Keycloak user sync |
| document-api | 8765 | 文件管理 / ACL 設定 |

---

## 七、Volume 說明

| Volume | 掛載路徑 | 說明 |
|--------|----------|------|
| `postgres_data` | postgres:/var/lib/postgresql/data | DB 資料（持久化） |
| `ingest_data` | document-api + ingest-worker:/data | PDF 上傳暫存 + ingest 工作目錄（兩個 service 共用） |

---

## 八、重置資料（開發環境）

**清除所有資料（包含 DB）：**

```bash
cd deployments/compose
docker compose down -v    # -v 會同時刪除所有 volume
docker compose up -d
```

**只清除 ingest 暫存檔（保留 DB）：**

```bash
docker volume rm compose_ingest_data
docker compose up -d
```

---

## 九、停止服務

```bash
# 停止所有服務（SIGTERM → 等待 grace period → SIGKILL）
docker compose stop

# 停止單一服務
docker compose stop retrieve-api
```

---

## 十、常見問題

### LiteLLM 401 錯誤

原因：`LITELLM_API_KEY` 未設定或錯誤。

```bash
# 確認 .env 中有正確設定
grep LITELLM_API_KEY deployments/compose/.env
```

### LiteLLM 400 model 不存在

原因：`LITELLM_BASE_URL` 指向的 LiteLLM 沒有設定對應模型。
解法：確認外部 LiteLLM 已設定 `EMBED_MODEL` / `RERANK_MODEL` / `CHAT_MODEL` 對應的模型路由。

### 首次啟動 postgres 失敗

```bash
docker compose down -v   # 刪除 postgres_data volume
docker compose up -d
```
