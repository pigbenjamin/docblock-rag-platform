# 部署指南

## 前置需求

| 項目 | 最低版本 | 說明 |
|------|----------|------|
| Docker Engine | 24+ | GPU passthrough 需要 nvidia-container-toolkit |
| Docker Compose | v2.20+ | `docker compose` 指令（非 `docker-compose`） |
| NVIDIA GPU + Driver | — | marker-service 執行 Surya OCR |
| Python | 3.x | 僅執行測試腳本需要 |
| Nostr Relay | — | **外部服務**，consumer 和 proxy 共用同一個 relay |
| LiteLLM（外部） | — | **外部服務**，提供 embedding / rerank / chat 模型 |

---

## 一、前置準備

### 1.1 下載 Marker / Surya 模型（首次部署）

Marker 的 OCR 模型（Surya）需要約 **3.3 GB** 快取：

```bash
pip install marker-pdf
python3 -c "from marker.models import load_all_models; load_all_models()"
```

模型存放於 `~/.cache/datalab/models/`，Compose 以唯讀方式掛載進 marker-service。

### 1.2 確認外部服務可達

```bash
# Nostr Relay
wscat -c wss://your-relay-host:9443   # 應能連線

# LiteLLM（外部）
curl http://your-litellm-host:port/v1/models \
  -H "Authorization: Bearer your-api-key"
```

### 1.3 準備 Nostr 金鑰對

nostr-proxy 需要一組 Schnorr 金鑰對（用於簽名 Nostr 事件），nostr-consumer 需要另一組（用於簽名回覆事件）。

```bash
# 可使用 nostr-tool 或任何相容的金鑰生成工具
# proxy pubkey 必須加入 nostr-consumer 的 allowlist.json
```

確認 `services/nostr-consumer/allowlist.json` 包含 proxy 的 pubkey：
```json
{
  "allowed_pubkeys": [
    "your-proxy-pubkey-hex"
  ]
}
```

---

## 二、環境設定

### 2.1 建立 `.env`

```bash
cd deployments/compose
cp .env.example .env   # 若有範本，或直接編輯 .env
```

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

# ── LLM 路由（retrieve-api / docblock-core 使用）──────────────
# 指向 nostr-proxy；nostr-proxy 再透過 Nostr 路由至外部 LiteLLM
LITELLM_BASE_URL=http://nostr-proxy:8800
OLLAMA_BASE_URL=http://nostr-proxy:8800   # 向後相容保留

# 模型名稱（需與外部 LiteLLM 設定一致）
EMBED_MODEL=Qwen3-Embedding-8B
RERANK_MODEL=Qwen3-Reranker-8B
SEG_MODEL=qwen3:8b
SUMMARY_MODEL=qwen3:8b
CHAT_MODEL=qwen3:8b

# ── Nostr Proxy ─────────────────────────────────────────────────
RELAY_URL=wss://your-relay-host:9443/
NOSTR_PRIV_KEY=<64-char hex 私鑰>
NOSTR_PUB_KEY=<64-char hex 公鑰>
OLLAMA_DIRECT_URL=http://host.docker.internal:11434  # Nostr 停用時的 fallback

# Nostr routing 開關（預設全部 true）
EMBED_VIA_NOSTR=true
RERANK_VIA_NOSTR=true
CHAT_VIA_NOSTR=true

# ── Nostr Consumer ──────────────────────────────────────────────
# 注意：docker-compose 中 nostr-consumer 的 LITELLM_BASE_URL
#       會被 service 層 override 為外部 LiteLLM 地址，不使用此值
BOT_PRIVATE_KEY=<64-char hex 私鑰（與 proxy 不同）>
BOT_PUBKEY=<64-char hex 公鑰>

# ── Ingest worker / Marker ──────────────────────────────────────
MARKER_SERVICE_URL=http://marker-service:8766
LITELLM_PROXY_URL=http://litellm-proxy:4000
LITELLM_API_KEY=sk-litellm-internal

# ── Admin API ───────────────────────────────────────────────────
ACL_ADMIN_SECRET=dev-secret-change-me
INGEST_WORKER_URL=http://ingest-worker:8762

# ── Webhook Service ─────────────────────────────────────────────
WEBHOOK_SECRET=dev-webhook-secret
KEYCLOAK_URL=http://your-keycloak-host
KEYCLOAK_REALM=your-realm
KEYCLOAK_CLIENT_ID=docblock
KEYCLOAK_CLIENT_SECRET=your-client-secret

# ── HuggingFace 離線模式 ─────────────────────────────────────────
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1

# ── Logging ─────────────────────────────────────────────────────
LOG_DIR=logs
```

> **生產環境請務必更換** `POSTGRES_PASSWORD`、`ACL_ADMIN_SECRET`、`WEBHOOK_SECRET`、`NOSTR_PRIV_KEY`、`BOT_PRIVATE_KEY`。

---

## 三、Build Image

### 3.1 一次 Build 所有 image

```bash
cd deployments/compose
docker compose build
```

### 3.2 單獨 Build 指定 service

```bash
docker compose build nostr-proxy
docker compose build nostr-consumer
docker compose build marker-service
docker compose build retrieve-api
docker compose build admin-api
docker compose build ingest-worker
docker compose build webhook-service
```

### 3.3 強制重新 Build

```bash
docker compose build --no-cache nostr-proxy nostr-consumer
```

### 3.4 Build 時間參考

| Service | 首次 Build | 有快取 |
|---------|-----------|--------|
| marker-service | ~5 分鐘 | ~30 秒 |
| ingest-worker | ~2 分鐘 | ~15 秒 |
| retrieve-api | ~2 分鐘 | ~15 秒 |
| admin-api | ~2 分鐘 | ~15 秒 |
| nostr-proxy | ~1 分鐘 | ~10 秒 |
| nostr-consumer | ~1 分鐘 | ~10 秒 |
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
2. `marker-service`（GPU 服務，最先啟動）
3. `litellm-proxy`（等待 marker-service 啟動）
4. `webhook-service`、`retrieve-api`（同時啟動）
5. `ingest-worker`（等待 litellm-proxy 啟動）
6. `admin-api`（等待 ingest-worker 啟動）
7. `nostr-proxy`（等待 litellm-proxy 啟動）
8. `nostr-consumer`（等待 nostr-proxy 和 litellm-proxy 啟動）

### 4.2 確認所有服務正常

```bash
docker compose ps
```

預期狀態全為 `Up`（postgres 顯示 `healthy`）：

```
NAME                        STATUS
compose-postgres-1          Up (healthy)
compose-marker-service-1    Up
compose-litellm-proxy-1     Up
compose-ingest-worker-1     Up
compose-retrieve-api-1      Up
compose-admin-api-1         Up
compose-webhook-service-1   Up
compose-nostr-proxy-1       Up
compose-nostr-consumer-1    Up
```

### 4.3 健康檢查

```bash
curl http://localhost:8761/healthz        # retrieve-api
curl http://localhost:8762/healthz        # ingest-worker
curl http://localhost:8763/healthz        # webhook-service
curl http://localhost:8765/healthz        # admin-api
curl http://localhost:8766/healthz        # marker-service
curl http://localhost:8800/health         # nostr-proxy
curl http://localhost:4000/health/liveliness  # litellm-proxy
```

或執行整合測試（含 nostr-proxy health check）：

```bash
python3 tests/01_health_check.py
```

### 4.4 確認 Nostr 路徑通暢

```bash
# 測試 nostr-proxy 三個 endpoint（需 Nostr relay + consumer 全部在線）
NOSTR_PROXY=http://localhost:8800 python3 tests/14_nostr_proxy.py
```

預期全部 PASS：embedding dim=4096、rerank results=3、chat role='assistant'、legacy /api/embeddings。

### 4.5 確認 nostr-consumer 連線狀態

```bash
docker logs compose-nostr-consumer-1 2>&1 | tail -5
```

正常輸出：
```
--- 正在啟動 Nostr Consumer (Kind 2000/2001/2002) ---
RELAY_URL: wss://...
LITELLM_BASE_URL: http://...
✅ 稽核資料庫: ./data/audit.db
>>> 連線成功，訂閱 Kind 2000 / 2001 ...
```

---

## 五、更新單一 Service

### 5.1 更新 nostr-proxy / nostr-consumer

```bash
cd deployments/compose

# 修改程式碼後重新 build
docker compose build nostr-proxy nostr-consumer

# 重啟（consumer 會等待 SIGTERM 最多 15 秒後才強制停止）
docker compose up -d nostr-proxy nostr-consumer
```

> **注意：** 只修改 `.env` 中的環境變數（不需 rebuild）：
> ```bash
> docker compose up -d --no-build nostr-consumer
> ```

### 5.2 更新 docblock-core 後需 rebuild 的 service

修改 `libs/docblock-core/` 下的程式碼時，需重建所有依賴該函式庫的 service：

```bash
docker compose build ingest-worker retrieve-api admin-api webhook-service
docker compose up -d ingest-worker retrieve-api admin-api webhook-service
```

---

## 六、服務 Port 對照

| Service | 對外 Port | 用途 |
|---------|-----------|------|
| postgres | 5437 | PostgreSQL（本機直連 debug 用） |
| retrieve-api | 8761 | 搜尋 / RAG 問答 |
| ingest-worker | 8762 | PDF ingest pipeline |
| webhook-service | 8763 | Keycloak user sync |
| admin-api | 8765 | 文件管理 / ACL 設定 |
| marker-service | 8766 | PDF → Markdown OCR（GPU） |
| litellm-proxy | 4000 | LLM 路由 proxy（marker 專用） |
| nostr-proxy | 8800 | OpenAI-compatible API → Nostr 路由 |
| nostr-consumer | — | 無 HTTP port（純 Nostr subscriber） |

---

## 七、Volume 說明

| Volume | 掛載路徑 | 說明 |
|--------|----------|------|
| `postgres_data` | postgres:/var/lib/postgresql/data | DB 資料（持久化） |
| `ingest_data` | admin-api + ingest-worker + marker-service:/data | PDF 上傳暫存 + ingest 工作目錄（三個 service 共用） |
| `nostr_consumer_data` | nostr-consumer:/app/data | audit.db 稽核記錄（持久化） |
| `~/.cache/datalab/models` | marker-service:/datalab_cache:ro | Surya OCR 模型快取（唯讀掛載） |

---

## 八、重置資料（開發環境）

**清除所有資料（包含 DB）：**

```bash
cd deployments/compose
docker compose down -v    # -v 會同時刪除所有 volume
docker compose up -d
```

**只清除 nostr-consumer 稽核 DB：**

```bash
docker volume rm compose_nostr_consumer_data
docker compose up -d nostr-consumer
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
docker compose stop nostr-consumer   # 等待最多 15s（stop_grace_period）
docker compose stop nostr-proxy
```

> **nostr-consumer 停止說明：** consumer 已實作 SIGTERM handler，停止時會先呼叫 `ws.close()` 關閉 WebSocket 連線，再正常退出。docker compose 會等待最多 `stop_grace_period: 15s`。

---

## 十、常見問題

### nostr-consumer 沒有 log 輸出

原因：Python stdout 預設是 buffering 狀態。  
解法：docker-compose.yml 中的 `nostr-consumer` 已設定 `PYTHONUNBUFFERED: "1"`，若仍無輸出請確認 image 有重新 build。

```bash
docker compose build --no-cache nostr-consumer
docker compose up -d nostr-consumer
```

### nostr-proxy /v1/chat/completions 超時（500）

原因：consumer 未收到事件、未回覆、或 LiteLLM 呼叫失敗。

排查步驟：
```bash
# 1. 確認 consumer 有收到並處理事件
docker logs compose-nostr-consumer-1 2>&1 | grep -E "💬|Chat|❌" | tail -10

# 2. 確認 LITELLM_BASE_URL 和 LITELLM_API_KEY 正確
docker exec compose-nostr-consumer-1 env | grep LITELLM
```

### nostr-consumer LiteLLM 401 錯誤

原因：`LITELLM_API_KEY` 未設定或錯誤。

```bash
# 確認 .env 中有正確設定
grep LITELLM_API_KEY deployments/compose/.env
```

### nostr-consumer LiteLLM 400 model 不存在

原因：`LITELLM_BASE_URL` 指向的 LiteLLM 沒有設定對應模型。  
解法：確認 consumer 的 `LITELLM_BASE_URL` 指向有 embedding/rerank/chat 模型的外部 LiteLLM，而非只有 marker 路由的本地 `litellm-proxy:4000`。

```yaml
# docker-compose.yml 中 nostr-consumer 的 override
environment:
  LITELLM_BASE_URL: http://外部-litellm-host:port   # 不是 litellm-proxy:4000
```

### marker-service 啟動後 GPU 未被偵測

```bash
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi
```

若無 GPU，移除 `docker-compose.yml` 中 marker-service 的 `deploy.resources` 區塊。

### litellm_config.yaml 更新後路由沒有生效

```bash
docker compose up -d --force-recreate litellm-proxy
```

### Relay 連線失敗（TLS 憑證錯誤）

nostr-proxy 和 nostr-consumer 連接 relay 時預設停用 TLS 驗證（`ssl.CERT_NONE`）。若 relay 使用自簽憑證，此為預期行為。

### 首次啟動 postgres 失敗

```bash
docker compose down -v   # 刪除 postgres_data volume
docker compose up -d
```
