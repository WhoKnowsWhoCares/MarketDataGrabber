#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export $(grep -E "^CLICKHOUSE_(USER|PASSWORD)=" .env | xargs)
docker compose exec -T clickhouse clickhouse-client \
  --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" \
  --multiquery < db/schema.sql
echo "OK: schema applied"
