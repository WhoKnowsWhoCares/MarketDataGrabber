#!/usr/bin/env bash
# Применяет db/ro_user.sql к живому ClickHouse под админским юзером.
# Пароль mdg_ro берётся из .env (MCP_RO_PASSWORD) или генерируется и дописывается в .env.
# Пароли в stdout НЕ печатаются. Идемпотентно (CREATE USER IF NOT EXISTS / GRANT).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "ERROR: .env not found in repo root" >&2
  exit 1
fi

# shellcheck disable=SC1091
set -a; source .env; set +a

CLICKHOUSE_USER="${CLICKHOUSE_USER:?CLICKHOUSE_USER not set in .env}"
CLICKHOUSE_PASSWORD="${CLICKHOUSE_PASSWORD:-}"
CH_URL="http://${CLICKHOUSE_HOST:-172.18.0.1}:8123"

# 1. Пароль mdg_ro: из .env или сгенерировать и дописать.
PW_SOURCE="env"
if [ -z "${MCP_RO_PASSWORD:-}" ]; then
  PW_SOURCE="generated+appended"
  MCP_RO_PASSWORD="$(openssl rand -hex 24)"
  if ! printf '\nMCP_RO_PASSWORD=%s\n' "$MCP_RO_PASSWORD" >> .env; then
    echo "ERROR: cannot append MCP_RO_PASSWORD to .env" >&2
    exit 1
  fi
fi
echo "ro password source: ${PW_SOURCE}"

# 2. Применить SQL, подставив пароль вместо плейсхолдера.
SQL="$(sed "s|__MCP_RO_PASSWORD__|${MCP_RO_PASSWORD}|g" db/ro_user.sql)"
APPLY_OUT="$(curl -fsS "${CH_URL}/?user=${CLICKHOUSE_USER}&password=${CLICKHOUSE_PASSWORD}" \
  --data-binary "$SQL")" || { echo "ERROR: apply failed: ${APPLY_OUT:-curl error}" >&2; exit 1; }
if [ -n "$APPLY_OUT" ]; then
  echo "$APPLY_OUT"
fi

# 3. Проверка: SELECT 1 под mdg_ro работает.
if SEL="$(curl -fsS "${CH_URL}/" --user "mdg_ro:${MCP_RO_PASSWORD}" --data-binary "SELECT 1")" \
   && [ "$SEL" = "1" ]; then
  echo "CHECK OK: mdg_ro SELECT 1 -> 1"
else
  echo "CHECK FAIL: mdg_ro SELECT 1" >&2
  exit 1
fi

# 4. Проверка: CREATE TABLE под mdg_ro отклоняется (нет прав; код 164/517).
CREATE_ERR="$(curl -sS "${CH_URL}/" --user "mdg_ro:${MCP_RO_PASSWORD}" \
  --data-binary "CREATE TABLE market_data_aux.mcp_ro_denied_check (x UInt8) ENGINE=MergeTree ORDER BY x" 2>&1 || true)"
CREATE_CODE="$(curl -sS -o /dev/null -w '%{http_code}' "${CH_URL}/" --user "mdg_ro:${MCP_RO_PASSWORD}" \
  --data-binary "CREATE TABLE market_data_aux.mcp_ro_denied_check (x UInt8) ENGINE=MergeTree ORDER BY x")"
if echo "$CREATE_ERR" | grep -qE 'ACCESS_DENIED|NOT_ENOUGH_PRIVILEGES|code: *(164|517)'; then
  echo "CHECK OK: mdg_ro CREATE TABLE denied (expected)"
else
  echo "CHECK FAIL: unexpected result for CREATE TABLE under mdg_ro (http ${CREATE_CODE}): ${CREATE_ERR}" >&2
  exit 1
fi

echo "OK: mdg_ro applied and verified"
