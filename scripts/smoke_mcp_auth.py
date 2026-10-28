"""Smoke-тест MCP-аутентификации на живом CH.

Генерирует тестовый ключ (owner=_smoke), проверяет verify_key -> ok,
revoke -> None. DELETE под mdg-юзером недоступен — запись остаётся
отозванной. Запускать вручную:
  set -a; source .env; set +a && PYTHONPATH=src .venv/bin/python scripts/smoke_mcp_auth.py
"""
from __future__ import annotations

import secrets
import sys

from mdgrabber.ch import get_client
from mdgrabber.config import Settings
from mdgrabber.mcp_auth import audit_call, hash_key, mask_hash, verify_key

KEYS_TABLE = "market_data_aux.mcp_keys"


def main() -> int:
    client = get_client(Settings.load())
    key = f"mdg_{secrets.token_hex(16)}"
    key_hash = hash_key(key)

    client.command(
        f"INSERT INTO {KEYS_TABLE} (key_hash, owner, public_only) VALUES "
        f"('{key_hash}', '_smoke', 1)"
    )
    print(f"1. generated test key, hash {mask_hash(key_hash)}")

    info = verify_key(key, client=client)
    if info is None or info.owner != "_smoke":
        print("FAIL: verify_key did not accept fresh key")
        return 1
    print(f"2. verify_key -> OK (owner={info.owner}, public_only={info.public_only})")

    audit_call(key_hash, tool="_smoke_check", args="", rows=0, duration_ms=0, ok=True, client=client)
    print("3. audit_call -> OK")

    # Отзыв: INSERT строки с revoked=1 (ReplacingMergeTree), DELETE недоступен.
    client.command(
        f"INSERT INTO {KEYS_TABLE} (key_hash, owner, public_only, revoked) VALUES "
        f"('{key_hash}', '_smoke', 1, 1)"
    )
    after = verify_key(key, client=client)
    if after is not None:
        print("FAIL: verify_key accepted revoked key (FINAL not merged yet? run OPTIMIZE)")
        return 1
    print("4. revoked key -> verify_key None")

    print("SMOKE OK: ключ остаётся в mcp_keys как revoked (нет прав на DELETE)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
