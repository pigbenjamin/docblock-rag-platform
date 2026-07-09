#!/usr/bin/env bash
# Build all service images
# Usage: ./deployments/build-all.sh [--no-cache]
#
# Environment variables:
#   REGISTRY  (default: ghcr.io)
#   OWNER     GitHub username or org
#   REPO      GitHub repo name (default: docblock-rag-platform)

set -euo pipefail

REGISTRY=${REGISTRY:-ghcr.io}
OWNER=${OWNER:-pigbenjamin}
REPO=${REPO:-docblock-rag-platform}
IMAGE_PREFIX="${REGISTRY}/${OWNER}/${REPO}"
SHA=$(git rev-parse --short HEAD)
NO_CACHE=""
[[ "${1:-}" == "--no-cache" ]] && NO_CACHE="--no-cache"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

ALL_SERVICES=(
  retrieve-api
  document-api
  ingest-worker
  webhook-service
)

echo "================================================"
echo "  Build All Services"
echo "  IMAGE_PREFIX : ${IMAGE_PREFIX}"
echo "  SHA          : ${SHA}"
echo "  NO_CACHE     : ${NO_CACHE:-off}"
echo "================================================"

FAILED=()

for SERVICE in "${ALL_SERVICES[@]}"; do
  echo ""
  echo ">>> Building ${SERVICE} ..."
  if docker build ${NO_CACHE} \
    -f "${REPO_ROOT}/services/${SERVICE}/Dockerfile" \
    -t "${IMAGE_PREFIX}/${SERVICE}:latest" \
    -t "${IMAGE_PREFIX}/${SERVICE}:sha-${SHA}" \
    "${REPO_ROOT}"; then
    echo "✅ ${SERVICE} done"
  else
    echo "❌ ${SERVICE} FAILED"
    FAILED+=("${SERVICE}")
  fi
done

echo ""
echo "================================================"
if [[ ${#FAILED[@]} -eq 0 ]]; then
  echo "  All services built successfully ✓"
  echo "  Tags: :latest  :sha-${SHA}"
else
  echo "  FAILED services: ${FAILED[*]}"
  exit 1
fi
echo "================================================"
