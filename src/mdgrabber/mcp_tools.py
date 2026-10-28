"""Инструменты MCP-сервера: 5 read-only функций над gold-витринами.

Каждый вызов:
  1. verify_key по ключу из contextvar (кладёт туда mcp_asgi) —
     невалидный/отсутствующий ключ -> PermissionError("invalid api key");
  2. SELECT через отдельный клиент под read-only юзером mdg_ro;
  3. audit_call (одна запись на вызов: rows/duration_ms/ok).

Ключи нигде не логируются: mcp_auth пишет в логи только маску SHA256-хэша.
"""
from __future__ import annotations

import contextvars
import json
import logging
import time
from typing import Optional

from .config import REPO_ROOT, Settings
from .mcp_auth import audit_call, verify_key

logger = logging.getLogger(__name__)

api_key_var: contextvars.ContextVar[str] = contextvars.ContextVar("api_key")

GOLD = "market_data_gold.mart_daily_metrics"
EVENTS = "market_data.events"
FUNDAMENTALS = "market_data.fundamentals_snapshots"


def _load_instruments() -> dict[str, dict]:
    with open(REPO_ROOT / "config" / "instruments.json") as f:
        return json.load(f)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _uid_expr(ticker: str) -> str:
    """multiIf тикер->instrument_uid (как в Grafana-дашборде).

    Неизвестный тикер -> пустая строка (запрос не вернёт строк).
    """
    instruments = _load_instruments()
    parts: list[str] = []
    for t, cfg in instruments.items():
        uid = cfg.get("uid")
        if uid:
            parts.append(f"'{_escape(ticker)}' = '{_escape(t)}'")
            parts.append(f"'{uid}'")
    default = instruments.get(ticker, {}).get("uid", "")
    return "multiIf(" + ", ".join(parts) + f", '{_escape(default)}')"


def _ticker_expr() -> str:
    """multiIf instrument_uid->тикер (для вывода событий)."""
    instruments = _load_instruments()
    parts: list[str] = []
    for t, cfg in instruments.items():
        uid = cfg.get("uid")
        if uid:
            parts.append(f"instrument_uid = '{uid}', '{_escape(t)}'")
    return "multiIf(" + ", ".join(parts) + ", '')"


def _asset_uid_expr(ticker: str) -> str:
    """multiIf тикер->asset_uid (фундаментал есть только у акций)."""
    instruments = _load_instruments()
    parts: list[str] = []
    for t, cfg in instruments.items():
        asset_uid = cfg.get("asset_uid")
        if asset_uid:
            parts.append(f"'{_escape(ticker)}' = '{_escape(t)}'")
            parts.append(f"'{asset_uid}'")
    default = instruments.get(ticker, {}).get("asset_uid", "")
    return "multiIf(" + ", ".join(parts) + f", '{_escape(default)}')"


def get_ro_client():
    """Отдельный клиент под read-only mdg_ro (не админский из .env)."""
    from .ch import get_client

    settings = Settings.load()
    return get_client(
        settings,
        user="mdg_ro",
        password=settings.mcp_ro_password,
    )


def _authorize(client) -> str:
    """Проверка ключа из contextvar; возвращает key_hash для аудита."""
    key = api_key_var.get("")
    info = verify_key(key, client=client)
    if info is None:
        raise PermissionError("invalid api key")
    return info.key_hash


def _run_tool(client, tool: str, args: str, fn):
    """Обёртка аудита: одна запись mcp_audit на вызов инструмента."""
    key_hash = _authorize(client)
    started = time.monotonic()
    ok = True
    rows = 0
    try:
        result = fn()
        rows = len(result) if isinstance(result, (list, dict)) else 1
        return result
    except Exception:
        ok = False
        raise
    finally:
        duration_ms = int((time.monotonic() - started) * 1000)
        audit_call(key_hash, tool, args, rows, duration_ms, ok, client=client)


# ---------------------------------------------------------------------------
# Инструменты (тестируются напрямую, без подъёма FastMCP)
# ---------------------------------------------------------------------------

def list_instruments(client) -> list[dict]:
    """Список инструментов юниверса: тикер, uid, figi, asset_uid."""
    def _q() -> list[dict]:
        out = []
        for ticker, cfg in _load_instruments().items():
            out.append(
                {
                    "ticker": ticker,
                    "uid": cfg.get("uid", ""),
                    "figi": cfg.get("figi", ""),
                    "asset_uid": cfg.get("asset_uid", ""),
                }
            )
        return out

    return _run_tool(client, "list_instruments", "{}", _q)


def get_daily_metrics(client, ticker: str, days: int = 30) -> list[dict]:
    """Последние N строк март-витрины по тикеру."""
    days = max(1, min(int(days), 400))

    def _q() -> list[dict]:
        sql = (
            f"SELECT bar_date, close, ret_1d, volume, volume_ratio, rsi14, atr14 "
            f"FROM {GOLD} FINAL "
            f"WHERE instrument_uid = ({_uid_expr(ticker)}) "
            f"ORDER BY bar_date DESC LIMIT {int(days)}"
        )
        result = client.query(sql)
        return [
            {
                "bar_date": r["bar_date"].isoformat(),
                "close": float(r["close"]),
                "ret_1d": None if r["ret_1d"] is None else float(r["ret_1d"]),
                "volume": int(r["volume"]),
                "volume_ratio": None if r["volume_ratio"] is None else float(r["volume_ratio"]),
                "rsi14": None if r["rsi14"] is None else float(r["rsi14"]),
                "atr14": None if r["atr14"] is None else float(r["atr14"]),
            }
            for r in result.named_results()
        ]

    return _run_tool(
        client, "get_daily_metrics", json.dumps({"ticker": ticker, "days": days}), _q
    )


def get_latest_status(client, ticker: str) -> dict:
    """Последняя строка (argMax по bar_date): цены, доходности, RSI, объём,
    гэп и положение относительно MA20/MA50 (вычисляется в SQL)."""
    def _q() -> dict:
        sql = f"""
        SELECT
            bar_date,
            close,
            ret_1d, ret_5d, ret_20d,
            rsi14, volume_ratio, gap_pct,
            ma20, ma50,
            close > ma20 AS above_ma20,
            close > ma50 AS above_ma50
        FROM (
            SELECT
                max(bar_date) AS bar_date,
                argMax(close, bar_date) AS close,
                argMax(ret_1d, bar_date) AS ret_1d,
                argMax(ret_5d, bar_date) AS ret_5d,
                argMax(ret_20d, bar_date) AS ret_20d,
                argMax(rsi14, bar_date) AS rsi14,
                argMax(volume_ratio, bar_date) AS volume_ratio,
                argMax(gap_pct, bar_date) AS gap_pct,
                argMax(ma20, bar_date) AS ma20,
                argMax(ma50, bar_date) AS ma50
            FROM {GOLD} FINAL
            WHERE instrument_uid = ({_uid_expr(ticker)})
        )"""
        result = client.query(sql)
        rows = list(result.named_results())
        if not rows:
            return {}
        r = rows[0]
        return {
            "ticker": ticker,
            "bar_date": r["bar_date"].isoformat(),
            "close": float(r["close"]),
            "ret_1d": None if r["ret_1d"] is None else float(r["ret_1d"]),
            "ret_5d": None if r["ret_5d"] is None else float(r["ret_5d"]),
            "ret_20d": None if r["ret_20d"] is None else float(r["ret_20d"]),
            "rsi14": None if r["rsi14"] is None else float(r["rsi14"]),
            "volume_ratio": None if r["volume_ratio"] is None else float(r["volume_ratio"]),
            "gap_pct": None if r["gap_pct"] is None else float(r["gap_pct"]),
            "ma20": None if r["ma20"] is None else float(r["ma20"]),
            "ma50": None if r["ma50"] is None else float(r["ma50"]),
            "above_ma20": bool(r["above_ma20"]),
            "above_ma50": bool(r["above_ma50"]),
        }

    return _run_tool(client, "get_latest_status", json.dumps({"ticker": ticker}), _q)


def get_events(client, ticker: Optional[str] = None) -> list[dict]:
    """События с today() вперёд (без trading_day), тикер через multiIf."""
    def _q() -> list[dict]:
        filter_sql = (
            f"AND instrument_uid = ({_uid_expr(ticker)}) " if ticker else ""
        )
        sql = f"""
        SELECT
            {_ticker_expr()} AS ticker,
            event_type,
            expected_date,
            event_time,
            importance
        FROM {EVENTS} FINAL
        WHERE event_type != 'trading_day'
          AND expected_date >= today()
          {filter_sql}
        ORDER BY expected_date"""
        result = client.query(sql)
        return [
            {
                "ticker": r["ticker"],
                "event_type": r["event_type"],
                "expected_date": (
                    r["expected_date"].isoformat() if r["expected_date"] else None
                ),
                "event_time": (
                    r["event_time"].isoformat() if r["event_time"] is not None else None
                ),
                "importance": int(r["importance"] or 0),
            }
            for r in result.named_results()
        ]

    return _run_tool(client, "get_events", json.dumps({"ticker": ticker}), _q)


def get_fundamentals(client, ticker: str) -> dict:
    """Последний фундаментал-снапшот по asset_uid тикера."""
    def _q() -> dict:
        sql = f"""
        SELECT
            asset_uid,
            JSONExtractFloat(payload, 'peRatioTtm') AS peRatioTtm,
            JSONExtractFloat(payload, 'priceToBookTtm') AS priceToBookTtm,
            JSONExtractFloat(payload, 'forwardAnnualDividendYield') AS forwardAnnualDividendYield
        FROM {FUNDAMENTALS}
        WHERE asset_uid = ({_asset_uid_expr(ticker)})
          AND snapshot_ts = (
              SELECT max(snapshot_ts) FROM {FUNDAMENTALS}
          )"""
        result = client.query(sql)
        rows = list(result.named_results())
        if not rows:
            return {}
        r = rows[0]
        return {
            "ticker": ticker,
            "asset_uid": r["asset_uid"],
            "peRatioTtm": float(r["peRatioTtm"]),
            "priceToBookTtm": float(r["priceToBookTtm"]),
            "forwardAnnualDividendYield": float(r["forwardAnnualDividendYield"]),
        }

    return _run_tool(client, "get_fundamentals", json.dumps({"ticker": ticker}), _q)
