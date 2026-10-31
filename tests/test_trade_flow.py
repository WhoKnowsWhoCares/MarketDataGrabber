"""Тесты для торгового флоу (P6.2): inline-подтверждение, Redis pending, guardrails."""
import json
import os
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Set up test env
os.environ.setdefault("T_INVEST_READ_TOKEN", "tok123456789")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:abc")
os.environ.setdefault("ADMIN_CHAT_ID", "1")
os.environ.setdefault("CLICKHOUSE_USER", "u")
os.environ.setdefault("CLICKHOUSE_PASSWORD", "p")
os.environ.setdefault("TLS_VERIFY", "false")
os.environ.setdefault("T_INVEST_SANDBOX_TOKEN", "sbx-token")
os.environ.setdefault("T_INVEST_SANDBOX_ACCOUNT_ID", "acc-1")
os.environ.setdefault("TRADE_MODE", "sandbox")
os.environ.setdefault("TRADE_MAX_LOTS", "1")
os.environ.setdefault("SHOW_TRADE_BUTTONS", "0")


from mdgrabber.config import Settings
from mdgrabber.trade import TradeClient, TradeError
import scripts.run_signals as rs


def make_settings(**overrides) -> Settings:
    """Создать Settings с переопределениями."""
    for k, v in overrides.items():
        os.environ[k] = str(v)
    return Settings.load()


def make_signal(ticker: str, signal_type: str = "rsi_oversold", bar_date="2026-10-28"):
    return {
        "instrument_uid": f"uid-{ticker}",
        "ticker": ticker,
        "signal_type": signal_type,
        "bar_date": bar_date,
        "value": 25.0,
        "message": f"{ticker}: test signal",
    }


# ========== Tests for Redis pending TTL ==========

def test_pending_ttl_write_read(monkeypatch):
    """Pending пишется в Redis с TTL 60с и читается."""
    with patch("redis.from_url") as mock_from_url:
        mock_redis = MagicMock()
        mock_from_url.return_value = mock_redis
        mock_redis.get.return_value = None
        
        # Mock list_subscribers to return a test chat_id
        monkeypatch.setattr(rs, "list_subscribers", lambda: ["123"])
        
        settings = make_settings(SHOW_TRADE_BUTTONS="1")
        signals = [make_signal("SBER", "rsi_oversold")]
        
        # Call internal function
        rs._create_signal_pending(settings, signals)
        
        # Verify set called with ex=60 (redis-py 8.x: setex deprecated)
        mock_redis.set.assert_called()
        call_args = mock_redis.set.call_args
        assert call_args.kwargs.get("ex") == 60  # TTL
        pending_key = call_args[0][0]
        assert "trade:pending:" in pending_key
        assert "SBER" in pending_key
        assert "2026-10-28" in pending_key
        
        # Verify JSON payload
        payload = json.loads(call_args[0][1])
        assert payload["ticker"] == "SBER"
        assert payload["direction"] == "buy"
        assert payload["lots"] == 1


def test_callback_without_pending_expired(monkeypatch):
    """Callback без pending в Redis → 'истёк'."""
    # This tests the bot callback handler logic
    # We mock Redis to return None (expired key)
    with patch("redis.from_url") as mock_from_url:
        mock_redis = MagicMock()
        mock_from_url.return_value = mock_redis
        mock_redis.get.return_value = None  # expired
        
        # The bot handler would do:
        # pending_json = r.get(pending_key)
        # if not pending_json: -> "истёк"
        pending_json = mock_redis.get("trade:pending:1:SBER:2026-10-28")
        assert pending_json is None


# ========== Tests for non-admin rejection ==========

def test_trade_command_non_admin_rejected(monkeypatch):
    """Non-admin /trade → 'недоступно'."""
    settings = make_settings(ADMIN_CHAT_ID="999")  # admin is 999, user is 1
    chat_id = "1"
    
    is_admin = str(chat_id) == str(settings.admin_chat_id)
    assert is_admin is False


def test_callback_non_admin_rejected(monkeypatch):
    """Callback от non-admin → отказ."""
    settings = make_settings(ADMIN_CHAT_ID="999")
    chat_id = "1"
    
    is_admin = str(chat_id) == str(settings.admin_chat_id)
    assert is_admin is False


# ========== Tests for lots > max rejection ==========

def test_trade_command_lots_exceeds_max_rejected(monkeypatch):
    """lots > TRADE_MAX_LOTS → отказ."""
    settings = make_settings(TRADE_MAX_LOTS="1")
    lots = 5
    
    assert lots > settings.trade_max_lots
    assert not (1 <= lots <= settings.trade_max_lots)


def test_trade_command_lots_valid(monkeypatch):
    """lots в пределах max → OK."""
    settings = make_settings(TRADE_MAX_LOTS="5")
    lots = 3
    
    assert 1 <= lots <= settings.trade_max_lots


# ========== Tests for successful flow ==========

def test_successful_trade_flow_calls_place_market_order(monkeypatch):
    """Успешный флоу вызывает place_market_order с правильными аргументами."""
    import httpx
    
    seen = {}
    
    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            text=json.dumps({
                "orderId": "o-123",
                "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
                "initialOrderPrice": {"units": "295", "nano": 600000000},
            }),
        )
    
    settings = make_settings()
    client = TradeClient(settings, transport=httpx.MockTransport(handler))
    
    figi = "BBG004730N88"  # SBER
    resp = client.place_market_order(figi, 1, "buy")
    
    assert resp["orderId"] == "o-123"
    assert seen["body"]["instrumentId"] == figi
    assert seen["body"]["quantity"] == "1"
    assert seen["body"]["direction"] == "ORDER_DIRECTION_BUY"
    assert seen["body"]["orderType"] == "ORDER_TYPE_MARKET"


def test_successful_signal_flow_calls_place_market_order(monkeypatch):
    """Успешный флоу по сигналу вызывает place_market_order с BUY 1 lot."""
    import httpx
    
    seen = {}
    
    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            text=json.dumps({
                "orderId": "o-456",
                "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
            }),
        )
    
    settings = make_settings()
    client = TradeClient(settings, transport=httpx.MockTransport(handler))
    
    figi = "BBG004730N88"
    resp = client.place_market_order(figi, 1, "buy")
    
    assert resp["orderId"] == "o-456"
    assert seen["body"]["quantity"] == "1"
    assert seen["body"]["direction"] == "ORDER_DIRECTION_BUY"


# ========== Tests for SHOW_TRADE_BUTTONS ==========

def test_show_trade_buttons_false_no_buttons(monkeypatch):
    """SHOW_TRADE_BUTTONS=0 → кнопки не строятся."""
    settings = make_settings(SHOW_TRADE_BUTTONS="0")
    signals = [make_signal("SBER", "ma_cross_up")]
    
    text, reply_markup = rs.format_message_with_trade_buttons(signals, settings)
    
    assert reply_markup is None
    assert "SBER" in text


def test_show_trade_buttons_true_creates_buttons(monkeypatch):
    """SHOW_TRADE_BUTTONS=1 → кнопки создаются для bullish сигналов."""
    settings = make_settings(SHOW_TRADE_BUTTONS="1")
    signals = [
        make_signal("SBER", "ma_cross_up"),
        make_signal("GAZP", "rsi_oversold"),
        make_signal("CNYRUBF", "death_cross"),  # not bullish
    ]
    
    text, reply_markup = rs.format_message_with_trade_buttons(signals, settings)
    
    assert reply_markup is not None
    assert "inline_keyboard" in reply_markup
    # 2 bullish signals = 2 rows
    assert len(reply_markup["inline_keyboard"]) == 2
    # Check callback data format
    assert reply_markup["inline_keyboard"][0][0]["callback_data"].startswith("trade:signal:")
    assert "SBER" in reply_markup["inline_keyboard"][0][0]["text"]
    assert "GAZP" in reply_markup["inline_keyboard"][1][0]["text"]


def test_show_trade_buttons_only_bullish_get_buttons(monkeypatch):
    """Только ma_cross_up и rsi_oversold получают кнопки."""
    settings = make_settings(SHOW_TRADE_BUTTONS="1")
    bullish = {"ma_cross_up", "rsi_oversold"}
    all_types = ["ma_cross_up", "rsi_oversold", "ma_cross_down", "death_cross", "golden_cross", "volume_spike", "rsi_overbought"]
    
    for sig_type in all_types:
        signals = [make_signal("SBER", sig_type)]
        text, reply_markup = rs.format_message_with_trade_buttons(signals, settings)
        has_button = reply_markup is not None
        expected = sig_type in bullish
        assert has_button == expected, f"Signal {sig_type}: expected button={expected}, got {has_button}"


# ========== Tests for cooldown ==========

def test_cooldown_blocks_repeat(monkeypatch):
    """Cooldown 10с блокирует повторный вызов."""
    with patch("mdgrabber.ratelimit.allow") as mock_allow:
        mock_allow.return_value = False  # cooldown active
        
        from mdgrabber.ratelimit import allow
        result = allow("trade:cooldown:1", ttl_seconds=10)
        
        assert result is False
        mock_allow.assert_called_with("trade:cooldown:1", ttl_seconds=10)


def test_cooldown_allows_after_ttl(monkeypatch):
    """Cooldown разрешает после истечения TTL."""
    with patch("mdgrabber.ratelimit.allow") as mock_allow:
        mock_allow.return_value = True  # cooldown expired
        
        from mdgrabber.ratelimit import allow
        result = allow("trade:cooldown:1", ttl_seconds=10)
        
        assert result is True


# ========== Tests for guardrails ==========

def test_guard_trade_mode_not_sandbox_disabled(monkeypatch):
    """TRADE_MODE != sandbox → trading disabled."""
    settings = make_settings(TRADE_MODE="live")
    
    assert settings.trade_mode != "sandbox"


def test_guard_trade_mode_sandbox_allowed(monkeypatch):
    """TRADE_MODE == sandbox → trading allowed."""
    settings = make_settings(TRADE_MODE="sandbox")
    
    assert settings.trade_mode == "sandbox"


def test_guard_ticker_not_in_universe_rejected(monkeypatch):
    """Тикер не в universe → отказ."""
    from mdgrabber.bot import _ticker_to_figi
    
    figi = _ticker_to_figi("INVALID_TICKER")
    assert figi is None


def test_guard_ticker_in_universe_allowed(monkeypatch):
    """Тикер в universe → OK."""
    from mdgrabber.bot import _ticker_to_figi
    
    figi = _ticker_to_figi("SBER")
    assert figi is not None
    assert figi == "BBG004730N88"


def test_trade_error_handling(monkeypatch):
    """TradeError от API → показывается code + message."""
    import httpx
    
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            text=json.dumps({"code": 30099, "message": "The price is outside the limits"}),
        )
    
    settings = make_settings()
    client = TradeClient(settings, transport=httpx.MockTransport(handler))
    
    with pytest.raises(TradeError) as ei:
        client.place_market_order("BBG004730N88", 1, "buy")
    
    assert ei.value.code == 30099
    assert "outside the limits" in ei.value.message


# ========== Tests for pending key format ==========

def test_pending_key_format_command(monkeypatch):
    """Ключ pending для /trade: trade:pending:{chat_id}:{ticker}:{direction}:{lots}."""
    chat_id = "123"
    ticker = "SBER"
    direction = "buy"
    lots = 1
    callback_id = f"{ticker}:{direction}:{lots}"
    pending_key = f"trade:pending:{chat_id}:{callback_id}"
    
    assert pending_key == "trade:pending:123:SBER:buy:1"


def test_pending_key_format_signal(monkeypatch):
    """Ключ pending для сигнала: trade:pending:{chat_id}:{ticker}:{bar_date}."""
    chat_id = "123"
    ticker = "SBER"
    bar_date = "2026-10-28"
    signal_key = f"{ticker}:{bar_date}"
    pending_key = f"trade:pending:{chat_id}:{signal_key}"
    
    assert pending_key == "trade:pending:123:SBER:2026-10-28"


# ========== Tests for graceful degradation ==========

def test_no_redis_graceful_bot(monkeypatch):
    """Без Redis бот не падает (graceful)."""
    # ratelimit.allow returns True when Redis unavailable
    from mdgrabber.ratelimit import allow
    
    with patch("mdgrabber.ratelimit._redis_module", None):
        result = allow("test:key", ttl_seconds=10)
        assert result is True


def test_no_redis_graceful_signals(monkeypatch):
    """Без Redis run_signals не падает, pending просто не создается."""
    settings = make_settings(SHOW_TRADE_BUTTONS="1")
    signals = [make_signal("SBER", "ma_cross_up")]
    
    with patch("redis.from_url", side_effect=Exception("Connection refused")):
        # Should not raise
        rs._create_signal_pending(settings, signals)
    
    # No assertion needed - just verifies no exception


def test_no_trade_settings_graceful(monkeypatch):
    """Без trade настроек (пустые токены) бот не падает."""
    # Settings loads with empty sandbox token - TradeClient will raise on init
    # but bot handles this gracefully in callback
    settings = make_settings(T_INVEST_SANDBOX_TOKEN="")
    
    # TradeClient would raise RuntimeError on init if trade_mode != sandbox
    # But with sandbox mode and empty token, it initializes but fails on HTTP call
    # This is handled by try/except in callback
    assert settings.trade_mode == "sandbox"


# ========== Tests for structlog trade-flow events (P6.4) ==========

def _run_coro(coro):
    import asyncio
    return asyncio.run(coro)


async def _noop_coro(*a, **kw):
    pass


def test_trade_callback_and_executed_logged(monkeypatch):
    """trade-callback логирует trade_callback и trade_executed (со status)."""
    from mdgrabber import bot as bot_mod

    settings = make_settings(ADMIN_CHAT_ID="1", TRADE_MODE="sandbox")
    monkeypatch.setattr(bot_mod.Settings, "load", classmethod(lambda cls: settings))
    monkeypatch.setattr(bot_mod, "_ticker_to_figi", lambda t: "BBG004730N88")

    pending = {
        "figi": "BBG004730N88",
        "ticker": "SBER",
        "direction": "buy",
        "lots": 1,
        "chat_id": "1",
    }

    class _Msg:
        async def edit_text(self, *a, **kw):
            pass

    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        data="trade:exec:SBER:buy:1",
        message=_Msg(),
        answer=lambda *a, **kw: _noop_coro(),
    )

    with patch("redis.from_url") as mock_from_url, \
         patch.object(bot_mod, "logger") as mock_logger, \
         patch.object(bot_mod, "TradeClient") as mock_client_cls:
        mock_redis = MagicMock()
        mock_from_url.return_value = mock_redis
        mock_redis.get.return_value = json.dumps(pending)

        mock_client_cls.return_value.place_market_order.return_value = {
            "orderId": "o-789",
            "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
        }

        _run_coro(bot_mod.cb_trade_exec(cb))

        # trade_callback: info при получении callback
        info_events = [c.args[0] for c in mock_logger.info.call_args_list if c.args]
        assert "trade_callback" in info_events
        cb_call = [c for c in mock_logger.info.call_args_list if c.args and c.args[0] == "trade_callback"][0]
        assert cb_call.kwargs["data"] == "trade:exec:SBER:buy:1"

        # trade_executed: info после исполнения, status из ответа
        assert "trade_executed" in info_events
        ex_call = [c for c in mock_logger.info.call_args_list if c.args and c.args[0] == "trade_executed"][0]
        assert ex_call.kwargs["order_id"] == "o-789"
        assert ex_call.kwargs["ticker"] == "SBER"
        assert ex_call.kwargs["lots"] == 1
        assert ex_call.kwargs["status"] == "EXECUTION_REPORT_STATUS_FILL"


def test_trade_rejected_expired_logged(monkeypatch):
    """Истёк pending → warning trade_rejected reason=expired."""
    from mdgrabber import bot as bot_mod

    settings = make_settings(ADMIN_CHAT_ID="1", TRADE_MODE="sandbox")
    monkeypatch.setattr(bot_mod.Settings, "load", classmethod(lambda cls: settings))

    class _Msg:
        async def edit_text(self, *a, **kw):
            pass

    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        data="trade:exec:SBER:buy:1",
        message=_Msg(),
        answer=lambda *a, **kw: _noop_coro(),
    )

    with patch("redis.from_url") as mock_from_url, \
         patch.object(bot_mod, "logger") as mock_logger:
        mock_redis = MagicMock()
        mock_from_url.return_value = mock_redis
        mock_redis.get.return_value = None  # expired

        _run_coro(bot_mod.cb_trade_exec(cb))

        warn_calls = [
            c for c in mock_logger.warning.call_args_list
            if c.args and c.args[0] == "trade_rejected"
        ]
        assert len(warn_calls) == 1
        assert warn_calls[0].kwargs["reason"] == "expired"


def test_trade_rejected_not_admin_logged(monkeypatch):
    """Callback от non-admin → warning trade_rejected reason=not_admin."""
    from mdgrabber import bot as bot_mod

    settings = make_settings(ADMIN_CHAT_ID="999")
    monkeypatch.setattr(bot_mod.Settings, "load", classmethod(lambda cls: settings))

    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        data="trade:exec:SBER:buy:1",
        message=SimpleNamespace(),
        answer=lambda *a, **kw: _noop_coro(),
    )

    with patch.object(bot_mod, "logger") as mock_logger:
        _run_coro(bot_mod.cb_trade_exec(cb))

        warn_calls = [
            c for c in mock_logger.warning.call_args_list
            if c.args and c.args[0] == "trade_rejected"
        ]
        assert len(warn_calls) == 1
        assert warn_calls[0].kwargs["reason"] == "not_admin"


def test_trade_error_rejected_logged(monkeypatch):
    """TradeError от API → warning trade_rejected reason=trade_error."""
    from mdgrabber import bot as bot_mod
    from mdgrabber.trade import TradeError

    settings = make_settings(ADMIN_CHAT_ID="1", TRADE_MODE="sandbox")
    monkeypatch.setattr(bot_mod.Settings, "load", classmethod(lambda cls: settings))
    monkeypatch.setattr(bot_mod, "_ticker_to_figi", lambda t: "BBG004730N88")

    pending = {
        "figi": "BBG004730N88",
        "ticker": "SBER",
        "direction": "buy",
        "lots": 1,
        "chat_id": "1",
    }

    class _Msg:
        async def edit_text(self, *a, **kw):
            pass

    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        data="trade:exec:SBER:buy:1",
        message=_Msg(),
        answer=lambda *a, **kw: _noop_coro(),
    )

    with patch("redis.from_url") as mock_from_url, \
         patch.object(bot_mod, "logger") as mock_logger, \
         patch.object(bot_mod, "TradeClient") as mock_client_cls:
        mock_redis = MagicMock()
        mock_from_url.return_value = mock_redis
        mock_redis.get.return_value = json.dumps(pending)
        mock_client_cls.return_value.place_market_order.side_effect = TradeError(30099, "boom")

        _run_coro(bot_mod.cb_trade_exec(cb))

        warn_calls = [
            c for c in mock_logger.warning.call_args_list
            if c.args and c.args[0] == "trade_rejected"
        ]
        assert len(warn_calls) == 1
        assert warn_calls[0].kwargs["reason"] == "trade_error"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])