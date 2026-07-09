# 待辦事項

Compose 本機測試完成後的後續工作清單。

---

## 生產化補強

- [ ] **ingest-worker job store 換成 Redis / DB**
  - 目前用 in-memory dict，container 重啟後所有 job 狀態消失
  - 建議改用 Redis（`hset job:{job_id} status running`）或寫入 DB jobs table

- [ ] **ingest_data 共用 volume 改為 S3 / MinIO**
  - document-api 上傳 PDF → 寫 S3；ingest-worker 從 S3 拉取 PDF 執行 pipeline
  - 解除 `ReadWriteMany` PVC 依賴，讓兩個 service 可以獨立部署在不同節點

- [ ] **密鑰管理遷移**
  - 將 `.env` 中的 `POSTGRES_PASSWORD`、`ACL_ADMIN_SECRET`、`WEBHOOK_SECRET` 等遷移至 K8s Secret 或 Vault
  - CI/CD pipeline 不應直接持有明文密鑰

---

## CI/CD

- [ ] **建立 Container Registry + image tag 策略**
  - 選擇 registry（ECR / GCR / Harbor / GHCR）
  - 決定 tag 策略：`git SHA`、`semver`、或 `branch-YYYYMMDD`

- [ ] **建立 CI pipeline**
  - push code → build image → push registry → deploy
  - 觸發條件：PR merge to main
  - 需包含：自動跑 `tests/` 下的測試腳本

---

## K8s 遷移

- [ ] **撰寫 Kubernetes manifests 或 Helm chart**
  - 資源：Deployment、Service、ConfigMap、Secret、PVC、Ingress
  - postgres：block storage PVC（ReadWriteOnce）
  - ingest_data：若改 S3 則不需要 RWX PVC

- [ ] **設定 HPA 與 GPU 節點排程**
  - retrieve-api：CPU/memory 觸發 HPA，可水平擴展
  - ingest-worker：需要 GPU node，設定 `nodeSelector` 或 `tolerations`；GPU 資源不適合直接 HPA，考慮 KEDA 依 queue 長度觸發

---

## 完成紀錄

- [x] **marker-service 獨立為獨立服務**（2026-05-22，已於 2026-07-08 全數移除，見下）
  - 從 `ingest-worker` 抽出 marker（PDF → Markdown）為獨立 `marker-service`（port 8766，GPU）
  - 新增 `litellm-proxy`（port 4000）作為路由層，`ingest-worker` 透過 litellm 呼叫 marker

- [x] **移除 Nostr 通訊層，改直連外部 LiteLLM**（2026-07-07）
  - 刪除 `nostr-proxy`、`nostr-consumer`；docblock-core 全面改用 OpenAI-compatible 直連

- [x] **marker-service 完全移除，改由 firdi-litellm 承載**（2026-07-08）
  - firdi-litellm 平台已自行實作並上線 `marker/pdf-to-md` 模型路由，與本 repo 的 marker-service 完全重複
  - 移除本 repo 的 `services/marker-service/`、`docblock_core/marker_runner.py`、本地 `litellm-proxy` relay（`04-litellm-proxy.yaml`、`litellm_config.yaml`）
  - `ingest-worker` 透過 `LITELLM_PROXY_URL` 直連外部 LiteLLM 呼叫 `marker/pdf-to-md`，不再需要任何本地 marker 服務或路由層
