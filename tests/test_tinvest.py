import io
import json
import zipfile

import httpx

from mdgrabber.config import Settings
from mdgrabber.tinvest import TInvestClient, money_to_float


def make_settings(monkeypatch, tmp_path) -> Settings:
    monkeypatch.setattr("mdgrabber.config.REPO_ROOT", tmp_path)
    monkeypatch.setenv("T_INVEST_READ_TOKEN", "tok123456789")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ADMIN_CHAT_ID", "1")
    monkeypatch.setenv("CLICKHOUSE_USER", "u")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "p")
    monkeypatch.setenv("TLS_VERIFY", "false")
    return Settings.load()


def client_with(handler, monkeypatch, tmp_path) -> TInvestClient:
    s = make_settings(monkeypatch, tmp_path)
    return TInvestClient(s, transport=httpx.MockTransport(handler))


def test_money_to_float():
    assert money_to_float({"units": "291", "nano": 100000000}) == 291.1
    assert money_to_float(None) is None


def test_get_candles_parses(monkeypatch, tmp_path):
    body = {"candles": [{
        "time": "2026-10-23T10:00:00Z",
        "open": {"units": "291", "nano": 100000000},
        "high": {"units": "292", "nano": 0},
        "low": {"units": "290", "nano": 500000000},
        "close": {"units": "291", "nano": 500000000},
        "volume": "986",
    }]}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(".MarketDataService/GetCandles")
        assert request.headers["Authorization"] == "Bearer tok123456789"
        return httpx.Response(200, text=json.dumps(body))

    c = client_with(handler, monkeypatch, tmp_path)
    candles = c.get_candles(
        "uid", "CANDLE_INTERVAL_1_MIN", "2026-10-23T10:00:00Z", "2026-10-23T11:00:00Z"
    )
    assert len(candles) == 1
    assert candles[0].open == 291.1 and candles[0].volume == 986


def test_429_retry(monkeypatch, tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, text=json.dumps({"accounts": []}))

    c = client_with(handler, monkeypatch, tmp_path)
    assert c.get_accounts() == {"accounts": []}
    assert calls["n"] == 2


def test_429_gives_up(monkeypatch, tmp_path):
    import pytest

    from mdgrabber.tinvest import TInvestError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "0"})

    c = client_with(handler, monkeypatch, tmp_path)
    with pytest.raises(TInvestError, match="429"):
        c.get_accounts()


def test_history_zip_uses_figi(monkeypatch, tmp_path):
    seen = {}

    class FakeResponse:
        status_code = 200
        content = b"zipbytes"
        text = ""

    class FakeClient:
        def __init__(self, **kwargs):
            seen["kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, params=None, headers=None):
            seen["url"], seen["params"], seen["headers"] = url, params, headers
            return FakeResponse()

    monkeypatch.setattr("mdgrabber.tinvest.httpx.Client", FakeClient)
    c = client_with(lambda r: httpx.Response(200), monkeypatch, tmp_path)
    assert c.history_zip("BBG004730N88", 2024) == b"zipbytes"
    assert seen["url"] == "https://invest-public-api.tinkoff.ru/history-data"
    assert seen["params"] == {"figi": "BBG004730N88", "year": "2024"}
    assert seen["headers"]["Authorization"] == "Bearer tok123456789"
    assert seen["kwargs"]["verify"] is False


def test_parse_history_zip(monkeypatch, tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # реальный формат: ';'-разделитель, без заголовка, хвостовая пустая колонка
        zf.writestr(
            "SBER.csv",
            "X;2024-01-03T09:59:00Z;290;291;292;289;100;\n"
            "X;2024-01-03T10:00:00Z;291;290.5;291.2;290.1;50;\n",
        )
    c = client_with(lambda r: httpx.Response(200), monkeypatch, tmp_path)
    rows = c.parse_history_zip(buf.getvalue())
    assert rows == [
        {"uid": "X", "utc": "2024-01-03T09:59:00Z", "open": 290.0,
         "close": 291.0, "high": 292.0, "low": 289.0, "volume": 100},
        {"uid": "X", "utc": "2024-01-03T10:00:00Z", "open": 291.0,
         "close": 290.5, "high": 291.2, "low": 290.1, "volume": 50},
    ]


def test_parse_history_zip_comma_with_header(monkeypatch, tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "SBER.csv",
            "UID,UTC,open,close,high,low,volume\nX,2024-01-03T09:59:00Z,290,291,292,289,100\n",
        )
    c = client_with(lambda r: httpx.Response(200), monkeypatch, tmp_path)
    rows = c.parse_history_zip(buf.getvalue())
    assert rows == [{"uid": "X", "utc": "2024-01-03T09:59:00Z", "open": 290.0,
                     "close": 291.0, "high": 292.0, "low": 289.0, "volume": 100}]


def test_get_asset_by_parses(monkeypatch, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(".InstrumentsService/GetAssetBy")
        assert b"INSTRUMENT_ID_TYPE_INSTRUMENT_ID" in request.content
        return httpx.Response(200, text=json.dumps(
            {"asset": {"uid": "asset-uid-1", "type": "ASSET_TYPE_SHARE"}}
        ))

    c = client_with(handler, monkeypatch, tmp_path)
    asset = c.get_asset_by("instrument-uid-1")
    assert asset["uid"] == "asset-uid-1"


def test_get_fundamentals_and_consensus(monkeypatch, tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path.rsplit(".", 1)[-1])
        if calls[-1] == "InstrumentsService/GetAssetFundamentals":
            assert json.loads(request.content) == {"assets": ["asset-uid-1"]}
            return httpx.Response(200, text=json.dumps(
                {"fundamentals": [{"asset_uid": "asset-uid-1", "pe_ratio_ttm": 2.5}]}
            ))
        assert calls[-1] == "InstrumentsService/GetConsensusForecasts"
        return httpx.Response(200, text=json.dumps({"items": []}))

    c = client_with(handler, monkeypatch, tmp_path)
    resp = c.get_fundamentals(["asset-uid-1"])
    assert resp["fundamentals"][0]["pe_ratio_ttm"] == 2.5
    assert c.get_consensus_forecasts() == {"items": []}
    assert calls == [
        "InstrumentsService/GetAssetFundamentals",
        "InstrumentsService/GetConsensusForecasts",
    ]
