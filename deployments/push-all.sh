#!/usr/bin/env bash
# Push all service images to GHCR
# Usage: ./deployments/push-all.sh
#
# Environment variables:
#   REGISTRY      (default: ghcr.io)
#   OWNER         GitHub username (default: pigbenjamin)
#   REPO          GitHub repo name (default: docblock-rag-platform)
#   DOCKER_CONFIG 獨立的 docker config 目錄，避免覆蓋其他人的登入
#                 (default: ~/.docker-km)
#
# 登入方式（只需執行一次）：
#   DOCKER_CONFIG=~/.docker-km \
#     docker login ghcr.io -u pigbenjamin --password-stdin <<< $GITHUB_PAT

set -euo pipefail

REGISTRY=${REGISTRY:-ghcr.io}
OWNER=${OWNER:-pigbenjamin}
REPO=${REPO:-docblock-rag-platform}
IMAGE_PREFIX="${REGISTRY}/${OWNER}/${REPO}"
SHA=$(git rev-parse --short HEAD)

# 使用獨立的 docker config，不影響其他人的登入狀態
export DOCKER_CONFIG="${DOCKER_CONFIG:-${HOME}/.docker-km}"

ALL_SERVICES=(
  nostr-proxy
  nostr-consumer
  retrieve-api
  admin-api
  ingest-worker
  marker-service
  webhook-service
)

echo "================================================"
echo "  Push All Services to GHCR"
echo "  IMAGE_PREFIX  : ${IMAGE_PREFIX}"
echo "  SHA           : ${SHA}"
echo "  DOCKER_CONFIG : ${DOCKER_CONFIG}"
echo "================================================"

# 確認已登入
if [[ ! -f "${DOCKER_CONFIG}/config.json" ]]; then
  echo ""
  echo "⚠️  尚未登入。請先執行："
  echo "   mkdir -p ~/.docker-km"
  echo "   DOCKER_CONFIG=~/.docker-km docker login ghcr.io -u ${OWNER} --password-stdin <<< \$GITHUB_PAT"
  echo ""
  exit 1
fi

FAILED=()

for SERVICE in "${ALL_SERVICES[@]}"; do
  echo ""
  echo ">>> Pushing ${SERVICE} ..."

  if ! docker image inspect "${IMAGE_PREFIX}/${SERVICE}:latest" &>/dev/null; then
    echo "⚠️  ${SERVICE}:latest not found locally, skipping"
    echo "   Run ./deployments/build-all.sh first"
    FAILED+=("${SERVICE}(not built)")
    continue
  fi

  if docker push "${IMAGE_PREFIX}/${SERVICE}:latest" && \
     docker push "${IMAGE_PREFIX}/${SERVICE}:sha-${SHA}"; then
    echo "✅ ${SERVICE} pushed"
  else
    echo "❌ ${SERVICE} FAILED"
    FAILED+=("${SERVICE}")
  fi
done

echo ""
echo "================================================"
if [[ ${#FAILED[@]} -eq 0 ]]; then
  echo "  All services pushed successfully ✓"
  echo "  Registry: ${IMAGE_PREFIX}/<service>:latest"
  echo "            ${IMAGE_PREFIX}/<service>:sha-${SHA}"
else
  echo "  FAILED: ${FAILED[*]}"
  exit 1
fi
echo "================================================"
