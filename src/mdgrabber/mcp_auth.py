"""Аутентификация MCP-ключей и аудит вызовов (market_data_aux.mcp_keys / mcp_audit).

Сам ключ нигде не хранится и не логируется: в БД и логах — только SHA256-хэш,
замаскированный до «…<последние 6>».
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_KEYS_TABLE = "market_data_aux.mcp_keys"
_AUDIT_TABLE = "market_data_aux.mcp_audit"


@dataclass(frozen=True)
class AuthInfo:
    key_hash: str
    owner: str
    public_only: bool


def hash_key(key: str) -> str:
    """SHA256-хэш ключа — единственная форма, попадающая в БД и логи."""
    return hashlib.sha256(key.encode()).hexdigest()


def mask_hash(key_hash: str) -> str:
    """Маска хэша для логов: …abc123."""
    return f"…{key_hash[-6:]}"


def _escape(value: str) -> str:
    """Экранирование строкового литерала для ClickHouse SQL."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def verify_key(key: str, client=None) -> AuthInfo | None:
    """Lookup по SHA256-хэшу, только не отозванные ключи.

    CH недоступен → исключение наружу (MCP-сервер падает честно).
    """
    if client is None:
        from .ch import get_client
        from .config import Settings

        client = get_client(Settings.load())

    key_hash = hash_key(key)
    result = client.query(
        f"SELECT owner, public_only FROM {_KEYS_TABLE} "
        f"WHERE key_hash = '{key_hash}' AND revoked = 0 LIMIT 1"
    )
    rows = result.result_rows
    if not rows:
        logger.info("verify_key: unknown/revoked key %s", mask_hash(key_hash))
        return None
    owner, public_only = rows[0]
    logger.info("verify_key: key %s accepted (owner=%s)", mask_hash(key_hash), owner)
    return AuthInfo(key_hash=key_hash, owner=owner, public_only=bool(public_only))


def audit_call(
    key_hash: str,
    tool: str,
    args: str,
    rows: int,
    duration_ms: int,
    ok: bool,
    client=None,
) -> None:
    """Вставка записи аудита. CH недоступен → исключение наружу."""
    if client is None:
        from .ch import get_client
        from .config import Settings

        client = get_client(Settings.load())

    client.command(
        f"INSERT INTO {_AUDIT_TABLE} (key_hash, tool, args, rows, duration_ms, ok) VALUES ("
        f"'{_escape(key_hash)}', '{_escape(tool)}', '{_escape(args)}', "
        f"{int(rows)}, {int(duration_ms)}, {1 if ok else 0})"
    )
    logger.info(
        "audit_call: tool=%s key=%s rows=%s duration_ms=%s ok=%s",
        tool,
        mask_hash(key_hash),
        rows,
        duration_ms,
        ok,
    )
