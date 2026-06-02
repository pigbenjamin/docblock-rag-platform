#!/usr/bin/env bash
# Build all service Docker images from the project root.
# Usage: ./scripts/build-all.sh [--push]

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PUSH=${1:-}

services=(webhook-service retrieve-api ingest-worker admin-api)

for svc in "${services[@]}"; do
    echo "▶ Building $svc ..."
    docker build \
        -f "$ROOT/services/$svc/Dockerfile" \
        -t "docblock-$svc:latest" \
        "$ROOT"
    if [[ "$PUSH" == "--push" ]]; then
        docker push "docblock-$svc:latest"
    fi
done

echo "✓ All images built."
