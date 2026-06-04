#!/usr/bin/env bash
# 建立 imagePullSecret 並 apply 所有 k8s manifests
# Usage: ./deployments/k8s-setup.sh

set -euo pipefail

NAMESPACE=docblock
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
K8S_DIR="${SCRIPT_DIR}/k8s"

echo "================================================"
echo "  Docblock K8s Setup"
echo "  Namespace : ${NAMESPACE}"
echo "================================================"

# ── 1. 確認 GITHUB_PAT 已設定 ────────────────────────────────
if [[ -z "${GITHUB_PAT:-}" ]]; then
  if [[ -f "${HOME}/.secrets" ]]; then
    source "${HOME}/.secrets"
  fi
fi

if [[ -z "${GITHUB_PAT:-}" ]]; then
  echo "❌ GITHUB_PAT 未設定。請執行："
  echo "   source ~/.secrets"
  exit 1
fi

# ── 2. 確保 Namespace 存在 ────────────────────────────────────
echo ""
echo ">>> 建立 Namespace ..."
kubectl apply -f "${K8S_DIR}/00-namespace.yaml"
echo "✅ Namespace 完成"

# ── 3. 建立 imagePullSecret ───────────────────────────────────
echo ""
echo ">>> 建立 imagePullSecret ..."
kubectl create secret docker-registry pigbenjamin-ghcr-secret \
  --docker-server=ghcr.io \
  --docker-username=pigbenjamin \
  --docker-password="${GITHUB_PAT}" \
  --namespace="${NAMESPACE}" \
  --dry-run=client -o yaml | kubectl apply -f -
echo "✅ imagePullSecret 完成"

# ── 3. Apply manifests（照順序）──────────────────────────────
MANIFESTS=(
  01-secrets.yaml
  02-configmap.yaml
  03-pv-pvc.yaml
  03b-postgres.yaml
  04-litellm-proxy.yaml
  06-ingest-worker.yaml
  07-admin-api.yaml
  08-webhook-service.yaml
  09-retrieve-api.yaml
  10-nostr-proxy.yaml
  11-nostr-consumer.yaml
)

echo ""
echo ">>> Applying manifests ..."
for MANIFEST in "${MANIFESTS[@]}"; do
  FILE="${K8S_DIR}/${MANIFEST}"
  if [[ -f "${FILE}" ]]; then
    kubectl apply -f "${FILE}"
    echo "✅ ${MANIFEST}"
  else
    echo "⚠️  ${MANIFEST} not found, skipping"
  fi
done

# ── 4. 等待並顯示狀態 ─────────────────────────────────────────
echo ""
echo ">>> Pod 狀態（等待啟動中...）"
kubectl rollout status deployment/litellm-proxy  -n "${NAMESPACE}" --timeout=120s 2>/dev/null || true
kubectl rollout status deployment/nostr-proxy    -n "${NAMESPACE}" --timeout=120s 2>/dev/null || true
kubectl rollout status deployment/nostr-consumer -n "${NAMESPACE}" --timeout=120s 2>/dev/null || true
kubectl rollout status deployment/retrieve-api   -n "${NAMESPACE}" --timeout=120s 2>/dev/null || true

echo ""
kubectl get pods -n "${NAMESPACE}"

echo ""
echo "================================================"
echo "  Setup 完成"
echo ""
echo "  NodePort 對外 port："
echo "    retrieve-api   → http://10.90.20.55:31761"
echo "    admin-api      → http://10.90.20.55:31765"
echo "    webhook-service → http://10.90.20.55:31763"
echo "    nostr-proxy    → http://10.90.20.55:31800"
echo ""
echo "  查看 pod 狀態："
echo "    kubectl get pods -n ${NAMESPACE}"
echo "================================================"
