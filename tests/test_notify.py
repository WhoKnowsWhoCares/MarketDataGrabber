import json

import httpx
import pytest

from mdgrabber.config import Settings
from mdgrabber.notify import send_telegram


def make_settings(monkeypatch, tmp_path) -> Settings:
    monkeypatch.setattr("mdgrabber.config.REPO_ROOT", tmp_path)
    monkeypatch.setenv("T_INVEST_READ_TOKEN", "tok123456789")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ADMIN_CHAT_ID", "111")
    monkeypatch.setenv("CLICKHOUSE_USER", "u")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "p")
    return Settings.load()


def test_ops_always_admin(monkeypatch, tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    orig_client = httpx.Client
    monkeypatch.setattr(
        "httpx.Client",
        lambda **kw: orig_client(transport=transport, **{k: v for k, v in kw.items() if k not in ("transport", "proxy")}),
    )

    s = make_settings(monkeypatch, tmp_path)
    send_telegram(s, "hello", category="ops", chat_id="999")
    assert seen["body"]["chat_id"] == "111"


def test_market_custom_chat(monkeypatch, tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    orig_client = httpx.Client
    monkeypatch.setattr(
        "httpx.Client",
        lambda **kw: orig_client(transport=transport, **{k: v for k, v in kw.items() if k not in ("transport", "proxy")}),
    )

    s = make_settings(monkeypatch, tmp_path)
    send_telegram(s, "signal", category="market", chat_id="222")
    assert seen["body"]["chat_id"] == "222"


def test_market_no_chat_uses_admin(monkeypatch, tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    orig_client = httpx.Client
    monkeypatch.setattr(
        "httpx.Client",
        lambda **kw: orig_client(transport=transport, **{k: v for k, v in kw.items() if k not in ("transport", "proxy")}),
    )

    s = make_settings(monkeypatch, tmp_path)
    send_telegram(s, "signal", category="market")
    assert seen["body"]["chat_id"] == "111"


def test_bad_category(monkeypatch, tmp_path):
    s = make_settings(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        send_telegram(s, "x", category="other")
