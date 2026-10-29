import json

import httpx
import pytest

from mdgrabber.config import Settings
from mdgrabber.trade import SANDBOX_REST, TradeClient, TradeError


def make_settings(monkeypatch, tmp_path, **overrides) -> Settings:
    monkeypatch.setattr("mdgrabber.config.REPO_ROOT", tmp_path)
    monkeypatch.setenv("T_INVEST_READ_TOKEN", "tok123456789")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ADMIN_CHAT_ID", "1")
    monkeypatch.setenv("CLICKHOUSE_USER", "u")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "p")
    monkeypatch.setenv("TLS_VERIFY", "false")
    monkeypatch.setenv("T_INVEST_SANDBOX_TOKEN", "sbx-token")
    monkeypatch.setenv("T_INVEST_SANDBOX_ACCOUNT_ID", "acc-1")
    monkeypatch.setenv("TRADE_MODE", "sandbox")
    for k, v in overrides.items():
        monkeypatch.setenv(k, v)
    return Settings.load()


def client_with(handler, monkeypatch, tmp_path, **overrides) -> TradeClient:
    s = make_settings(monkeypatch, tmp_path, **overrides)
    return TradeClient(s, transport=httpx.MockTransport(handler))


def test_default_trade_settings(monkeypatch, tmp_path):
    s = make_settings(monkeypatch, tmp_path)
    assert s.trade_base_url == SANDBOX_REST
    assert s.trade_mode == "sandbox"
    assert s.trade_max_lots == 1
    assert s.t_invest_sandbox_account_id == "acc-1"


def test_guard_live_mode_raises(monkeypatch, tmp_path):
    s = make_settings(monkeypatch, tmp_path, TRADE_MODE="live")
    with pytest.raises(RuntimeError, match="live trading not enabled"):
        TradeClient(s)


def test_market_order_json(monkeypatch, tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            text=json.dumps(
                {"orderId": "o-1", "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL"}
            ),
        )

    c = client_with(handler, monkeypatch, tmp_path)
    resp = c.place_market_order("BBG004730N88", 1, "buy")
    assert resp["orderId"] == "o-1"
    assert seen["path"].endswith(".SandboxService/PostSandboxOrder")
    assert seen["auth"] == "Bearer sbx-token"
    assert seen["body"] == {
        "instrumentId": "BBG004730N88",
        "quantity": "1",
        "direction": "ORDER_DIRECTION_BUY",
        "accountId": "acc-1",
        "orderType": "ORDER_TYPE_MARKET",
    }


def test_limit_order_price_units_string(monkeypatch, tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            text=json.dumps(
                {"orderId": "o-2", "executionReportStatus": "EXECUTION_REPORT_STATUS_NEW"}
            ),
        )

    c = client_with(handler, monkeypatch, tmp_path)
    resp = c.place_limit_order("BBG004730N88", 1, 295, 500000000, "sell")
    assert resp["executionReportStatus"] == "EXECUTION_REPORT_STATUS_NEW"
    assert seen["body"]["quantity"] == "1"
    assert seen["body"]["orderType"] == "ORDER_TYPE_LIMIT"
    # КРИТИЧНО: units — СТРОКА, числовые дают code 3 на живом API
    assert seen["body"]["price"] == {"units": "295", "nano": 500000000}
    assert seen["body"]["direction"] == "ORDER_DIRECTION_SELL"


def test_trade_error_on_code(monkeypatch, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, text=json.dumps({"code": 3, "message": "30099 The price is outside the limits"})
        )

    c = client_with(handler, monkeypatch, tmp_path)
    with pytest.raises(TradeError) as ei:
        c.place_limit_order("X", 1, 1, 0, "buy")
    assert ei.value.code == 3
    assert "outside the limits" in ei.value.message


def test_get_orders_and_cancel(monkeypatch, tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append((path.rsplit(".", 1)[-1], json.loads(request.content)))
        if path.endswith("GetSandboxOrders"):
            return httpx.Response(200, text=json.dumps({"orders": [{"orderId": "o-1"}]}))
        return httpx.Response(200, text=json.dumps({"time": "2026-10-29T10:00:00Z"}))

    c = client_with(handler, monkeypatch, tmp_path)
    orders = c.get_orders()
    assert orders == {"orders": [{"orderId": "o-1"}]}
    cancelled = c.cancel_order("o-1")
    assert "time" in cancelled
    assert seen[0] == ("SandboxService/GetSandboxOrders", {"accountId": "acc-1"})
    assert seen[1] == (
        "SandboxService/CancelSandboxOrder",
        {"accountId": "acc-1", "orderId": "o-1"},
    )


def test_pay_in_units_string(monkeypatch, tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text=json.dumps({"balance": "100000"}))

    c = client_with(handler, monkeypatch, tmp_path)
    resp = c.pay_in(100000)
    assert resp == {"balance": "100000"}
    assert seen["body"] == {
        "accountId": "acc-1",
        "amount": {"currency": "rub", "units": "100000", "nano": 0},
    }


def test_get_accounts_parses(monkeypatch, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(".SandboxService/GetSandboxAccounts")
        return httpx.Response(
            200,
            text=json.dumps(
                {"accounts": [{"id": "acc-1", "status": "CLOSED", "accessLevel": "ACCOUNT_ACCESS_LEVEL_FULL"}]}
            ),
        )

    c = client_with(handler, monkeypatch, tmp_path)
    accs = c.get_accounts()["accounts"]
    assert accs[0]["id"] == "acc-1"


def test_ensure_account_existing(monkeypatch, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(".SandboxService/GetSandboxAccounts")
        return httpx.Response(200, text=json.dumps({"accounts": [{"id": "acc-9"}]}))

    c = client_with(handler, monkeypatch, tmp_path)
    assert c.ensure_account() == "acc-9"


def test_ensure_account_opens_when_empty(monkeypatch, tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path.rsplit(".", 1)[-1])
        if len(seen) == 1:
            return httpx.Response(200, text=json.dumps({"accounts": []}))
        return httpx.Response(200, text=json.dumps({"accountId": "acc-new"}))

    c = client_with(handler, monkeypatch, tmp_path)
    assert c.ensure_account() == "acc-new"
    assert seen[1] == "SandboxService/OpenSandboxAccount"


def test_get_portfolio(monkeypatch, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(".OperationsService/GetPortfolio")
        assert json.loads(request.content) == {"accountId": "acc-1"}
        return httpx.Response(
            200,
            text=json.dumps(
                {
                    "positions": [
                        {"figi": "BBG004730N88", "quantity": {"units": "1", "nano": 0}}
                    ]
                }
            ),
        )

    c = client_with(handler, monkeypatch, tmp_path)
    pf = c.get_portfolio()
    assert pf["positions"][0]["figi"] == "BBG004730N88"


def test_bad_direction_raises(monkeypatch, tmp_path):
    c = client_with(lambda r: httpx.Response(200), monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="buy/sell"):
        c.place_market_order("X", 1, "up")
