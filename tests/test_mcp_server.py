"""Тесты MCP-инструментов (мок-клиент CH + мок verify_key, сервер не поднимается)."""
import re
from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

from mdgrabber import mcp_tools

UIDS = {
    "SBER": "e6123145-9665-43e0-8413-cd61b8aa9b13",
    "GAZP": "962e2a95-02a9-4171-abd7-aa198dbe643a",
    "CNYRUBF": "c300543d-aa18-4249-b110-615409dde036",
    "IMOEXF": "5bcff194-f10d-4314-b9ee-56b7fdb344fd",
    "IBZ6": "14d97146-1f46-435b-984f-3d4594a5183c",
}
VALID_KEY = "mdg_test_key_12345"


def _ch_client(rows: list[dict]) -> MagicMock:
    """Мок-клиент: query() -> named_results() отдаёт готовые dict-строки."""
    client = MagicMock()
    result = MagicMock()
    result.named_results.return_value = iter(rows)
    client.query.return_value = result
    return client


@pytest.fixture(autouse=True)
def valid_key(monkeypatch):
    """Валидный ключ в contextvar + мок verify_key/audit_call на каждый тест."""
    monkeypatch.setattr(mcp_tools, "verify_key", lambda key, client=None: (
        None if key != VALID_KEY else MagicMock(key_hash="a" * 64)
    ))
    calls: list[tuple] = []
    monkeypatch.setattr(
        mcp_tools, "audit_call",
        lambda key_hash, tool, args, rows, duration_ms, ok, client=None: calls.append(
            (key_hash, tool, rows, ok)
        ),
    )
    mcp_tools.api_key_var.set(VALID_KEY)
    yield calls
    mcp_tools.api_key_var.set("")


# --- (а) структура ответа каждого инструмента -------------------------------

def test_list_instruments_structure():
    rows = mcp_tools.list_instruments(MagicMock())
    assert len(rows) == 5
    for r in rows:
        assert set(r) == {"ticker", "uid", "figi", "asset_uid"}
        assert isinstance(r["ticker"], str) and r["ticker"]
        assert isinstance(r["uid"], str)
    tickers = {r["ticker"] for r in rows}
    assert tickers == set(UIDS)


def test_get_daily_metrics_structure():
    client = _ch_client(
        [
            {
                "bar_date": date(2026, 10, 27),
                "close": 300.5,
                "ret_1d": 1.2,
                "volume": 10_000_000,
                "volume_ratio": 1.4,
                "rsi14": 55.0,
                "atr14": 5.5,
            }
        ]
    )
    rows = mcp_tools.get_daily_metrics(client, "SBER", 30)
    assert len(rows) == 1
    r = rows[0]
    assert r["bar_date"] == "2026-10-27"
    assert isinstance(r["close"], float)
    assert isinstance(r["volume"], int)
    assert r["volume_ratio"] is None or isinstance(r["volume_ratio"], float)
    assert "LIMIT 30" in client.query.call_args[0][0]
    assert UIDS["SBER"] in client.query.call_args[0][0]


def test_get_latest_status_structure():
    client = _ch_client(
        [
            {
                "bar_date": date(2026, 10, 27),
                "close": 300.5,
                "ret_1d": 1.2,
                "ret_5d": 3.0,
                "ret_20d": -2.0,
                "rsi14": 55.0,
                "volume_ratio": 1.4,
                "gap_pct": 0.5,
                "ma20": 295.0,
                "ma50": 290.0,
                "above_ma20": 1,
                "above_ma50": 1,
            }
        ]
    )
    status = mcp_tools.get_latest_status(client, "SBER")
    assert status["ticker"] == "SBER"
    assert status["bar_date"] == "2026-10-27"
    assert isinstance(status["close"], float)
    assert isinstance(status["above_ma20"], bool)
    assert isinstance(status["above_ma50"], bool)
    sql = client.query.call_args[0][0]
    assert "argMax(close, bar_date)" in sql
    assert "close > ma20 AS above_ma20" in sql


def test_get_events_structure_with_and_without_ticker():
    client = _ch_client(
        [
            {
                "ticker": "SBER",
                "event_type": "dividend",
                "expected_date": date(2026, 11, 10),
                "event_time": None,
                "importance": 2,
            }
        ]
    )
    rows = mcp_tools.get_events(client, "SBER")
    assert len(rows) == 1
    r = rows[0]
    assert r["event_type"] == "dividend"
    assert r["expected_date"] == "2026-11-10"
    assert isinstance(r["importance"], int)
    sql = client.query.call_args[0][0]
    assert "event_type != 'trading_day'" in sql
    assert "expected_date >= today()" in sql
    # trading_day только в исключении, не в выборке
    assert sql.count("trading_day") == 1
    # фильтр по тикеру добавляет multiIf-условие
    assert UIDS["SBER"] in sql

    mcp_tools.get_events(client)  # без тикера
    sql2 = client.query.call_args[0][0]
    assert "AND instrument_uid" not in sql2


def test_get_fundamentals_structure():
    client = _ch_client(
        [
            {
                "asset_uid": "40d89385-a03a-4659-bf4e-d3ecba011782",
                "peRatioTtm": 5.4,
                "priceToBookTtm": 0.8,
                "forwardAnnualDividendYield": 0.1,
            }
        ]
    )
    row = mcp_tools.get_fundamentals(client, "SBER")
    assert row["ticker"] == "SBER"
    assert row["asset_uid"] == "40d89385-a03a-4659-bf4e-d3ecba011782"
    assert isinstance(row["peRatioTtm"], float)
    assert isinstance(row["priceToBookTtm"], float)
    sql = client.query.call_args[0][0]
    assert "peRatioTtm" in sql and "priceToBookTtm" in sql
    assert "forwardAnnualDividendYield" in sql
    assert "max(snapshot_ts)" in sql


# --- (б) невалидный ключ -> PermissionError ----------------------------------

def test_invalid_key_raises_permission_error():
    mcp_tools.api_key_var.set("wrong-key")
    with pytest.raises(PermissionError, match="invalid api key"):
        mcp_tools.list_instruments(MagicMock())


def test_missing_key_raises_permission_error():
    mcp_tools.api_key_var.set("")
    with pytest.raises(PermissionError, match="invalid api key"):
        mcp_tools.get_latest_status(MagicMock(), "SBER")


# --- (в) audit_call на каждый вызов ------------------------------------------

def test_audit_call_invoked_per_tool_call(valid_key):
    calls = valid_key
    empty = _ch_client([])
    mcp_tools.list_instruments(empty)
    mcp_tools.get_daily_metrics(empty, "SBER", 30)
    mcp_tools.get_latest_status(empty, "GAZP")
    mcp_tools.get_events(empty)
    mcp_tools.get_fundamentals(empty, "SBER")
    assert len(calls) == 5
    tools = [c[1] for c in calls]
    assert tools == [
        "list_instruments",
        "get_daily_metrics",
        "get_latest_status",
        "get_events",
        "get_fundamentals",
    ]
    for key_hash, _, rows, ok in calls:
        assert ok is True
        assert key_hash == "a" * 64
        assert rows >= 0


def test_audit_call_ok_false_on_error(valid_key):
    calls = valid_key
    client = MagicMock()
    client.query.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        mcp_tools.get_latest_status(client, "SBER")
    assert len(calls) == 1
    assert calls[0][3] is False


# --- (г) multiIf содержит все 5 uid ------------------------------------------

def test_multiif_contains_all_uids(valid_key):
    client = _ch_client([])
    mcp_tools.get_daily_metrics(client, "SBER", 5)
    sql = client.query.call_args[0][0]
    m = re.search(r"multiIf\([^)]*\)", sql, re.DOTALL)
    assert m, "multiIf-маппинг тикер->uid отсутствует в SQL"
    for uid in UIDS.values():
        assert uid in m.group(0), f"uid {uid} не найден в multiIf"


def test_multiif_unknown_ticker_maps_to_empty(valid_key):
    expr = mcp_tools._uid_expr("UNKNOWN")
    tail = expr.rsplit(",", 1)[-1]
    assert "''" in tail  # default -> пустая строка


# --- ASGI-обёртка: 401 на невалидном ключе, проход с валидным ----------------

def test_asgi_middleware_rejects_invalid_key(monkeypatch):
    import asyncio

    from mdgrabber import mcp_asgi

    monkeypatch.setattr(mcp_asgi, "check_key", lambda key, client=None: False)
    called = []

    async def inner(scope, receive, send):
        called.append(True)

    app = mcp_asgi.ApiKeyAuthMiddleware(inner)
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "headers": [(b"x-api-key", b"bad-key")],
    }
    asyncio.run(app(scope, MagicMock(), send))
    assert not called
    assert sent[0]["status"] == 401
    assert b"invalid api key" in sent[1]["body"]


def test_asgi_middleware_passes_valid_key(monkeypatch):
    import asyncio

    from mdgrabber import mcp_asgi

    monkeypatch.setattr(mcp_asgi, "check_key", lambda key, client=None: True)
    called = []

    async def inner(scope, receive, send):
        called.append(True)

    app = mcp_asgi.ApiKeyAuthMiddleware(inner)
    scope = {"type": "http", "headers": [(b"x-api-key", VALID_KEY.encode())]}
    asyncio.run(app(scope, MagicMock(), MagicMock()))
    assert called
    assert mcp_tools.api_key_var.get() == VALID_KEY
