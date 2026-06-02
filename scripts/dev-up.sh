#!/usr/bin/env bash
# Start the full local dev stack via docker-compose.
# Usage: ./scripts/dev-up.sh

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_DIR="$ROOT/deployments/compose"

if [[ ! -f "$COMPOSE_DIR/.env" ]]; then
    echo "⚠  .env not found — copying .env.example"
    cp "$COMPOSE_DIR/.env.example" "$COMPOSE_DIR/.env"
    echo "   Edit $COMPOSE_DIR/.env before re-running."
    exit 1
fi

docker compose -f "$COMPOSE_DIR/docker-compose.yml" --env-file "$COMPOSE_DIR/.env" up --build "$@"
