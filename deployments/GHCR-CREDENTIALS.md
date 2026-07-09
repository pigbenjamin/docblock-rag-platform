# GHCR 憑證管理說明

本機為多人共用環境，各自使用獨立的 Docker config 目錄，避免登入狀態互相覆蓋。

---

## 帳號資訊

| 項目 | 值 |
|------|-----|
| GitHub Username | `pigbenjamin` |
| GHCR Registry | `ghcr.io` |
| Image 路徑 | `ghcr.io/pigbenjamin/docblock-rag-platform/<service>:latest` |
| Docker config 目錄 | `~/.docker-km` |

---

## 一次性設定（首次使用）

### 1. 產生 GitHub Personal Access Token (PAT)

```
GitHub → Settings → Developer settings → Personal access tokens (classic)
→ Generate new token → 勾選：
  ✅ write:packages
  ✅ read:packages
  ✅ delete:packages（選用）
→ 設定到期日 → Generate token → 複製
```

### 2. 儲存 PAT

```bash
echo 'export GITHUB_PAT=ghp_你的token' >> ~/.secrets
source ~/.secrets
```

### 3. 登入 GHCR（使用獨立 config）

```bash
mkdir -p ~/.docker-km
DOCKER_CONFIG=~/.docker-km \
  docker login ghcr.io -u pigbenjamin --password-stdin <<< $GITHUB_PAT
```

登入成功後 `~/.docker-km/config.json` 會儲存憑證，之後不需要重複登入。

---

## 日常 Build & Push

```bash
cd /home/ai-x/km/repo/docblock-rag-platform

# Build 全部（自動使用 OWNER=pigbenjamin）
./deployments/build-all.sh

# Push 全部（自動使用 ~/.docker-km）
./deployments/push-all.sh
```

### 只 Build + Push 單一 service

```bash
source ~/.secrets
SHA=$(git rev-parse --short HEAD)
SERVICE=retrieve-api   # 改成目標 service

docker build -f services/${SERVICE}/Dockerfile \
  -t ghcr.io/pigbenjamin/docblock-rag-platform/${SERVICE}:latest \
  -t ghcr.io/pigbenjamin/docblock-rag-platform/${SERVICE}:sha-${SHA} \
  . && \
DOCKER_CONFIG=~/.docker-km docker push ghcr.io/pigbenjamin/docblock-rag-platform/${SERVICE}:latest && \
DOCKER_CONFIG=~/.docker-km docker push ghcr.io/pigbenjamin/docblock-rag-platform/${SERVICE}:sha-${SHA}
```

---

## K8s imagePullSecret

k8s 使用獨立的 secret（`pigbenjamin-ghcr-secret`），與其他人的 secret 完全分開。

### 建立（只需一次）

```bash
source ~/.secrets
kubectl create secret docker-registry pigbenjamin-ghcr-secret \
  --docker-server=ghcr.io \
  --docker-username=pigbenjamin \
  --docker-password=$GITHUB_PAT \
  --namespace=docblock
```

### PAT 過期後更新

```bash
source ~/.secrets   # 確認 GITHUB_PAT 已更新

# 更新 docker 登入
DOCKER_CONFIG=~/.docker-km \
  docker login ghcr.io -u pigbenjamin --password-stdin <<< $GITHUB_PAT

# 更新 k8s secret
kubectl create secret docker-registry pigbenjamin-ghcr-secret \
  --docker-server=ghcr.io \
  --docker-username=pigbenjamin \
  --docker-password=$GITHUB_PAT \
  --namespace=docblock \
  --dry-run=client -o yaml | kubectl apply -f -
```

---

## 確認 Image 是否已推上 GHCR

```bash
for SVC in retrieve-api document-api ingest-worker webhook-service; do
  if DOCKER_CONFIG=~/.docker-km docker manifest inspect \
    ghcr.io/pigbenjamin/docblock-rag-platform/${SVC}:latest > /dev/null 2>&1; then
    echo "✅ ${SVC}"
  else
    echo "❌ ${SVC}"
  fi
done
```

---

## 注意事項

- `~/.secrets` 和 `~/.docker-km/config.json` 均含有敏感資訊，請勿 commit 至 git
- PAT 建議設定到期日，到期前到 GitHub Settings 重新生成並更新 `~/.secrets`
- 其他使用者的 `~/.docker/config.json` 不受影響
