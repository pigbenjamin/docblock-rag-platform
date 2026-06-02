# 待辦事項

Compose 本機測試完成後的後續工作清單。

---

## 生產化補強

- [ ] **ingest-worker job store 換成 Redis / DB**
  - 目前用 in-memory dict，container 重啟後所有 job 狀態消失
  - 建議改用 Redis（`hset job:{job_id} status running`）或寫入 DB jobs table

- [ ] **ingest_data 共用 volume 改為 S3 / MinIO**
  - admin-api 上傳 PDF → 寫 S3；ingest-worker 從 S3 拉取 PDF 執行 pipeline
  - 解除 `ReadWriteMany` PVC 依賴，讓兩個 service 可以獨立部署在不同節點

- [ ] **K8s 上 Marker 模型快取分發策略**
  - Marker 已獨立為 `marker-service`（port 8766），GPU 需求集中於此
  - 選項 A：init container 在 pod 啟動時從 S3 拉取模型到 emptyDir
  - 選項 B：把模型打進 marker-service image（image 會很大，~3.3 GB）
  - 選項 C：hostPath（僅單節點適用）

- [ ] **litellm-proxy 高可用與設定管理**
  - 目前 `litellm_config.yaml` 以 bind mount 方式載入，更新需 `--force-recreate`
  - K8s 上改為 ConfigMap 掛載，更新後 rolling restart 即可生效
  - 評估是否需要 litellm-proxy HA（多副本）

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

- [x] **marker-service 獨立為獨立服務**（2026-05-22）
  - 從 `ingest-worker` 抽出 marker（PDF → Markdown）為獨立 `marker-service`（port 8766，GPU）
  - 新增 `litellm-proxy`（port 4000）作為路由層，`ingest-worker` 透過 litellm 呼叫 marker
  - 相關測試：`services/marker-service/tests/`（unit）、`tests/13_marker_service.py`（integration）
