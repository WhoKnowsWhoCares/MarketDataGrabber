"""Тесты mcp_auth (мок-клиент, живой CH не нужен)."""
import hashlib
from unittest.mock import MagicMock

from mdgrabber.mcp_auth import (
    AuthInfo,
    audit_call,
    hash_key,
    mask_hash,
    verify_key,
)


def _client_with_rows(rows: list) -> MagicMock:
    client = MagicMock()
    client.query.return_value.result_rows = rows
    return client


def test_hash_key_deterministic():
    """SHA256-хэш детерминирован и совпадает с hashlib."""
    key = "mdg_abc123"
    expected = hashlib.sha256(key.encode()).hexdigest()
    assert hash_key(key) == expected
    assert hash_key(key) == hash_key(key)
    assert len(expected) == 64


def test_verify_key_ok():
    """Найденный неотозванный ключ -> AuthInfo."""
    key = "mdg_abc123"
    key_hash = hash_key(key)
    client = _client_with_rows([("tester", 1)])

    info = verify_key(key, client=client)

    assert info == AuthInfo(key_hash=key_hash, owner="tester", public_only=True)
    assert key_hash in client.query.call_args[0][0]
    assert "revoked = 0" in client.query.call_args[0][0]


def test_verify_key_revoked_returns_none():
    """Revoked-ключ (строк в выборке нет) -> None."""
    client = _client_with_rows([])
    assert verify_key("mdg_revoked", client=client) is None


def test_verify_key_public_only_false():
    client = _client_with_rows([("svc", 0)])
    info = verify_key("mdg_x", client=client)
    assert info is not None
    assert info.public_only is False


def test_audit_call_builds_insert():
    """audit_call формирует корректный INSERT в mcp_audit."""
    client = MagicMock()
    key_hash = "a" * 64

    audit_call(
        key_hash,
        tool="get_candles",
        args="{'ticker': 'SBER'}",
        rows=42,
        duration_ms=120,
        ok=True,
        client=client,
    )

    sql = client.command.call_args[0][0]
    assert "INSERT INTO market_data_aux.mcp_audit" in sql
    assert f"'{key_hash}'" in sql
    assert "'get_candles'" in sql
    assert "42" in sql
    assert "120" in sql
    assert ", 1)" in sql


def test_audit_call_ok_false():
    client = MagicMock()
    audit_call("b" * 64, tool="t", args="", rows=0, duration_ms=1, ok=False, client=client)
    sql = client.command.call_args[0][0]
    assert ", 0)" in sql


def test_audit_call_escapes_quotes_in_args():
    """Одинарные кавычки в args экранируются."""
    client = MagicMock()
    audit_call("c" * 64, tool="t", args="it's", rows=0, duration_ms=0, ok=True, client=client)
    sql = client.command.call_args[0][0]
    assert "it\\'s" in sql


def test_mask_hash():
    """Маска хэша: …<последние 6>."""
    h = "0123456789" * 6 + "abc123"
    assert mask_hash(h) == "…abc123"


def test_mask_never_reveals_full_hash_in_logs():
    """Лог-строки verify_key/audit_call не содержат полный хэш и ключ."""
    import logging

    key = "mdg_secret"
    key_hash = hash_key(key)
    client = _client_with_rows([("tester", 1)])
    audit_client = MagicMock()

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    logger = logging.getLogger("mdgrabber.mcp_auth")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        verify_key(key, client=client)
        audit_call(key_hash, tool="t", args="", rows=1, duration_ms=1, ok=True, client=audit_client)
    finally:
        logger.removeHandler(handler)

    for rec in records:
        text = rec.getMessage()
        assert key not in text
        assert key_hash not in text
        assert mask_hash(key_hash) in text
