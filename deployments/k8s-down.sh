#!/usr/bin/env bash
# 關閉 docblock k8s 所有資源
# Usage:
#   ./deployments/k8s-down.sh          # 只停服務，保留資料（PV/PVC/Secrets 保留）
#   ./deployments/k8s-down.sh --all    # 完全清除，包含資料和 namespace

set -euo pipefail

NAMESPACE=docblock
PURGE=false
[[ "${1:-}" == "--all" ]] && PURGE=true

echo "================================================"
echo "  Docblock K8s Shutdown"
echo "  Namespace : ${NAMESPACE}"
echo "  Mode      : $(${PURGE} && echo '完全清除（--all）' || echo '只停服務')"
echo "================================================"

if ${PURGE}; then
  echo ""
  read -p "⚠️  這將刪除 namespace 及所有資料，確認繼續？[y/N] " -n 1 -r
  echo
  if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "已取消。"
    exit 0
  fi
fi

# ── 停止所有 Deployment ──────────────────────────────────────
echo ""
echo ">>> 停止 Deployments ..."
DEPLOYMENTS=(
  retrieve-api
  webhook-service
  document-api
  ingest-worker
)

for DEPLOY in "${DEPLOYMENTS[@]}"; do
  if kubectl get deployment "${DEPLOY}" -n "${NAMESPACE}" &>/dev/null; then
    kubectl delete deployment "${DEPLOY}" -n "${NAMESPACE}" --ignore-not-found
    echo "✅ ${DEPLOY} 已停止"
  fi
done

# ── 刪除 Services ─────────────────────────────────────────────
echo ""
echo ">>> 刪除 Services ..."
kubectl delete service --all -n "${NAMESPACE}" --ignore-not-found 2>/dev/null || true

if ${PURGE}; then
  # ── 完全清除模式 ─────────────────────────────────────────────
  echo ""
  echo ">>> 刪除 PVC / PV ..."
  kubectl delete pvc --all -n "${NAMESPACE}" --ignore-not-found
  kubectl delete pv docblock-ingest-pv --ignore-not-found

  echo ""
  echo ">>> 刪除 Secrets / ConfigMap ..."
  kubectl delete secret --all -n "${NAMESPACE}" --ignore-not-found
  kubectl delete configmap --all -n "${NAMESPACE}" --ignore-not-found

  echo ""
  echo ">>> 刪除 Namespace ..."
  kubectl delete namespace "${NAMESPACE}" --ignore-not-found

  echo ""
  echo "================================================"
  echo "  完全清除完成"
  echo "  ⚠️  hostPath 目錄資料仍保留於："
  echo "    /home/ai-x/data/docblock/ingest"
  echo "  若需清除請手動執行："
  echo "    rm -rf /home/ai-x/data/docblock"
  echo "================================================"
else
  echo ""
  echo "================================================"
  echo "  服務已停止（資料保留）"
  echo ""
  echo "  保留的資源："
  echo "    PV/PVC、Secrets、ConfigMap、Namespace"
  echo ""
  echo "  重新啟動："
  echo "    ./deployments/k8s-setup.sh"
  echo "================================================"
fi
