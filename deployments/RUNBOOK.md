# Image 管理操作手冊

## 目錄

1. [快速使用（Scripts）](#0-快速使用scripts)
2. [Build Image](#1-build-image)
3. [Push Image 到 GHCR](#2-push-image-到-ghcr)
4. [K8s 使用 Image](#3-k8s-使用-image)

---

## 0. 快速使用（Scripts）

最常用的操作已封裝成 script，位於 `deployments/`：

| Script | 功能 |
|--------|------|
| `build-all.sh` | Build 全部 7 個 service image |
| `push-all.sh` | Push 全部 image 到 GHCR |
| `k8s-setup.sh` | 建立 k8s Secret + apply 全部 manifests |
| `k8s-down.sh` | 停止所有服務（保留資料） |
| `k8s-down.sh --all` | 完全清除（含 PV/PVC/Secret/Namespace） |

> 登入與憑證設定詳見 [GHCR-CREDENTIALS.md](GHCR-CREDENTIALS.md)

### Build 全部 service

```bash
# 一般 build（使用 layer cache）
./deployments/build-all.sh

# 強制重 build（不用 cache）
./deployments/build-all.sh --no-cache
```

輸出範例：
```
================================================
  Build All Services
  IMAGE_PREFIX : ghcr.io/myorg/docblock-rag-platform
  SHA          : a1b2c3d
  NO_CACHE     : off
================================================

>>> Building retrieve-api ...
✅ retrieve-api done
...
================================================
  All services built successfully ✓
  Tags: :latest  :sha-a1b2c3d
================================================
```

### Push 全部 image 到 GHCR

```bash
# push-all.sh 自動使用 ~/.docker-km（獨立 config，不影響其他人）
source ~/.secrets
./deployments/push-all.sh
```

完整一次 build + push 流程：

```bash
source ~/.secrets
./deployments/build-all.sh
./deployments/push-all.sh
```

### 只 rebuild 單一 service 並 push

```bash
export OWNER=your-github-username
SHA=$(git rev-parse --short HEAD)
SERVICE=retrieve-api   # 改成目標 service

docker build -f services/${SERVICE}/Dockerfile \
  -t ghcr.io/${OWNER}/docblock-rag-platform/${SERVICE}:latest \
  -t ghcr.io/${OWNER}/docblock-rag-platform/${SERVICE}:sha-${SHA} \
  . && \
docker push ghcr.io/${OWNER}/docblock-rag-platform/${SERVICE}:latest && \
docker push ghcr.io/${OWNER}/docblock-rag-platform/${SERVICE}:sha-${SHA}
```

---

## 前置設定

```bash
# GHCR registry
REGISTRY=ghcr.io
OWNER=<your-github-username-or-org>
REPO=docblock-rag-platform

# 完整 image prefix
IMAGE_PREFIX=${REGISTRY}/${OWNER}/${REPO}
```

---

## 1. Build Image

所有 Dockerfile 的 build context 都是**專案根目錄**（因為需要複製 `libs/docblock-core`）。

### 1.1 Build 單一 service

```bash
cd /path/to/docblock-rag-platform

# retrieve-api
docker build -f services/retrieve-api/Dockerfile -t ${IMAGE_PREFIX}/retrieve-api:latest .

# document-api
docker build -f services/document-api/Dockerfile -t ${IMAGE_PREFIX}/document-api:latest .

# ingest-worker
docker build -f services/ingest-worker/Dockerfile -t ${IMAGE_PREFIX}/ingest-worker:latest .

# webhook-service
docker build -f services/webhook-service/Dockerfile -t ${IMAGE_PREFIX}/webhook-service:latest .
```

### 1.2 Build 全部 services（一次執行）

```bash
cd /path/to/docblock-rag-platform

for SERVICE in retrieve-api document-api ingest-worker webhook-service; do
  echo "=== Building ${SERVICE} ==="
  docker build -f services/${SERVICE}/Dockerfile \
    -t ${IMAGE_PREFIX}/${SERVICE}:latest \
    .
done
```

### 1.3 Build 時加上 git SHA tag（推薦）

```bash
SHA=$(git rev-parse --short HEAD)

docker build -f services/retrieve-api/Dockerfile \
  -t ${IMAGE_PREFIX}/retrieve-api:latest \
  -t ${IMAGE_PREFIX}/retrieve-api:sha-${SHA} \
  .
```

### 1.4 只 build docblock-core 相依的 services

修改 `libs/docblock-core` 後，需要重 build 以下服務：

```bash
for SERVICE in retrieve-api document-api ingest-worker webhook-service; do
  docker build -f services/${SERVICE}/Dockerfile \
    -t ${IMAGE_PREFIX}/${SERVICE}:latest \
    .
done
```

---

## 2. Push Image 到 GHCR

### 2.1 登入 GHCR

```bash
# 方法一：使用 GitHub Personal Access Token（需要 write:packages 權限）
echo $GITHUB_PAT | docker login ghcr.io -u <your-github-username> --password-stdin

# 方法二：使用 GitHub CLI（已安裝 gh 的環境）
gh auth token | docker login ghcr.io -u <your-github-username> --password-stdin
```

### 2.2 Push 單一 service

```bash
docker push ${IMAGE_PREFIX}/retrieve-api:latest
docker push ${IMAGE_PREFIX}/retrieve-api:sha-${SHA}
```

### 2.3 Build 完立即 Push（推薦一次完成）

```bash
SHA=$(git rev-parse --short HEAD)
SERVICE=retrieve-api   # 改成目標 service

docker build -f services/${SERVICE}/Dockerfile \
  -t ${IMAGE_PREFIX}/${SERVICE}:latest \
  -t ${IMAGE_PREFIX}/${SERVICE}:sha-${SHA} \
  . && \
docker push ${IMAGE_PREFIX}/${SERVICE}:latest && \
docker push ${IMAGE_PREFIX}/${SERVICE}:sha-${SHA}
```

### 2.4 Push 全部 services

```bash
SHA=$(git rev-parse --short HEAD)

for SERVICE in retrieve-api document-api ingest-worker webhook-service; do
  echo "=== Pushing ${SERVICE} ==="
  docker push ${IMAGE_PREFIX}/${SERVICE}:latest
  docker push ${IMAGE_PREFIX}/${SERVICE}:sha-${SHA}
done
```

### 2.5 透過 GitHub Actions 手動觸發（自動 build + push）

```bash
# 需要安裝 gh CLI
gh workflow run build-push.yml

# 強制 rebuild 全部 services
gh workflow run build-push.yml -f force_all=true

# 查看執行狀態
gh run list --workflow=build-push.yml
```

或在 GitHub 網頁：**Actions → Build & Push to GHCR → Run workflow**

---

## 3. K8s 部署與操作

### 3.1 部署（第一次 / 重新部署）

```bash
cd /home/ai-x/km/repo/docblock-rag-platform
source ~/.secrets
./deployments/k8s-setup.sh
```

`k8s-setup.sh` 會自動：
1. 建立 `pigbenjamin-ghcr-secret`（imagePullSecret）
2. Apply 所有 manifests（00～11）
3. 等待主要服務啟動並顯示 pod 狀態

### 3.2 停止服務

| 指令 | 停止項目 | 保留項目 | 用途 |
|------|---------|---------|------|
| `./deployments/k8s-down.sh` | Deployments, Services | PV/PVC, Secrets, ConfigMap, Namespace | 暫停服務，快速重啟 |
| `./deployments/k8s-down.sh --all` | 所有 k8s 資源 | 宿主機資料目錄 | 完全重來 |

```bash
# 暫停服務（保留設定與資料）
./deployments/k8s-down.sh

# 重新啟動
./deployments/k8s-setup.sh

# 完全清除（需重新設定）
./deployments/k8s-down.sh --all
```

> ⚠️ 兩種模式都**不會**刪除宿主機上的實際資料（`/home/ai-x/data/docblock/`）。

### 3.3 NodePort 對外 port

| 服務 | URL |
|------|-----|
| retrieve-api | `http://10.90.20.55:31761` |
| document-api | `http://10.90.20.55:31765` |
| webhook-service | `http://10.90.20.55:31763` |

### 3.4 部署

```bash
# 01-secrets.yaml（機密，不進 git）與 02-configmap.yaml（進 git）需先手動維護好
kubectl apply -f deployments/k8s/

# 確認狀態
kubectl get pods -n docblock
kubectl logs -f deployment/retrieve-api -n docblock
```

### 3.5 更新 image（程式碼有變動後）

```bash
# 方法一：指定新的 SHA tag（推薦，可回滾）
SHA=$(git rev-parse --short HEAD)
kubectl set image deployment/retrieve-api \
  retrieve-api=ghcr.io/<owner>/docblock-rag-platform/retrieve-api:sha-${SHA} \
  -n docblock

# 方法二：強制重新拉取 latest
kubectl rollout restart deployment/retrieve-api -n docblock

# 查看更新進度
kubectl rollout status deployment/retrieve-api -n docblock
```

### 3.6 回滾

```bash
# 回到上一個版本
kubectl rollout undo deployment/retrieve-api -n docblock

# 回到指定 SHA tag
kubectl set image deployment/retrieve-api \
  retrieve-api=ghcr.io/<owner>/docblock-rag-platform/retrieve-api:sha-<old-sha> \
  -n docblock
```

---

## 常用速查

```bash
# 查看 GHCR 上的 image tags
gh api /user/packages/container/docblock-rag-platform%2Fretrieve-api/versions \
  --jq '.[].metadata.container.tags'

# 查看本機所有 docblock image
docker images | grep docblock-rag-platform

# 清理本機舊 image
docker image prune -f
```
