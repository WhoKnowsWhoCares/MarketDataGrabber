"""Управление MCP-ключами: generate / list / revoke (market_data_aux.mcp_keys).

Ключ печатается в stdout ровно один раз при generate — в БД только SHA256-хэш.
Запуск: set -a; source .env; set +a && PYTHONPATH=src .venv/bin/python scripts/mcp_key.py ...
"""
from __future__ import annotations

import argparse
import secrets
import sys

from mdgrabber.ch import get_client
from mdgrabber.config import Settings
from mdgrabber.mcp_auth import hash_key, mask_hash

KEYS_TABLE = "market_data_aux.mcp_keys"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def cmd_generate(client, owner: str, public_only: bool) -> int:
    key = f"mdg_{secrets.token_hex(16)}"
    key_hash = hash_key(key)
    client.command(
        f"INSERT INTO {KEYS_TABLE} (key_hash, owner, public_only) VALUES "
        f"('{key_hash}', '{_escape(owner)}', {1 if public_only else 0})"
    )
    # Ключ выводится один раз; в логи идёт только маска хэша.
    print(key)
    print(f"key stored: {mask_hash(key_hash)} owner={owner}", file=sys.stderr)
    return 0


def cmd_list(client) -> int:
    result = client.query(
        f"SELECT key_hash, owner, public_only, revoked, created_at "
        f"FROM {KEYS_TABLE} FINAL ORDER BY created_at"
    )
    print(f"{'owner':<20} {'public_only':<12} {'revoked':<8} {'created':<20} hash")
    for key_hash, owner, public_only, revoked, created_at in result.result_rows:
        print(
            f"{owner:<20} {int(public_only):<12} {int(revoked):<8} "
            f"{str(created_at):<20} {mask_hash(str(key_hash))}"
        )
    return 0


def cmd_revoke(client, key: str) -> int:
    key_hash = hash_key(key)
    result = client.query(
        f"SELECT owner, public_only FROM {KEYS_TABLE} "
        f"WHERE key_hash = '{key_hash}' LIMIT 1"
    )
    if not result.result_rows:
        print(f"key not found: {mask_hash(key_hash)}", file=sys.stderr)
        return 1
    owner, public_only = result.result_rows[0]
    # ReplacingMergeTree ORDER BY key_hash: новая строка с revoked=1 затирает старую по FINAL.
    client.command(
        f"INSERT INTO {KEYS_TABLE} (key_hash, owner, public_only, revoked) VALUES "
        f"('{key_hash}', '{_escape(owner)}', {int(public_only)}, 1)"
    )
    print(f"revoked: {mask_hash(key_hash)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MCP-ключи: generate / list / revoke")
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="создать ключ (печатается один раз)")
    p_gen.add_argument("--owner", required=True)
    p_gen.add_argument("--public-only", action="store_true", default=True)

    sub.add_parser("list", help="список ключей (хэши маскированы)")

    p_rev = sub.add_parser("revoke", help="отозвать ключ")
    p_rev.add_argument("--key", required=True)

    args = parser.parse_args(argv)
    client = get_client(Settings.load())

    if args.command == "generate":
        return cmd_generate(client, args.owner, args.public_only)
    if args.command == "list":
        return cmd_list(client)
    return cmd_revoke(client, args.key)


if __name__ == "__main__":
    raise SystemExit(main())
