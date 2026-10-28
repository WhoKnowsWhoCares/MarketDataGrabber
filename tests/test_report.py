"""Тесты build_digest: ин-мемори данные, get_client замокан через monkeypatch."""
from __future__ import annotations

from datetime import date

import mdgrabber.report as report
from mdgrabber.config import Settings
from mdgrabber.report import MAX_LEN, build_digest

TODAY = date(2026, 10, 27)


def make_settings(monkeypatch, tmp_path) -> Settings:
    monkeypatch.setattr("mdgrabber.config.REPO_ROOT", tmp_path)
    monkeypatch.setenv("T_INVEST_READ_TOKEN", "tok123456789")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ADMIN_CHAT_ID", "111")
    monkeypatch.setenv("CLICKHOUSE_USER", "u")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "p")
    return Settings.load()


def fake_client(today=TODAY, n_instruments=5):
    """Фейковый ClickHouse-клиент: query() отвечает по тексту запроса."""

    class FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def named_results(self):
            yield from self._rows

    instruments = report._load_instruments()
    tickers = list(instruments)[:n_instruments]
    uid_of = lambda i: instruments[tickers[i]]["uid"]  # noqa: E731
    # asset_uid есть только у акций — берём из конфига.
    asset_of_uid = {
        uid_of(i): a
        for i in range(n_instruments)
        if (a := instruments[tickers[i]].get("asset_uid"))
    }

    def query(sql, parameters=None):
        if "max(bar_date)" in sql:
            return FakeResult([{"d": today}])
        if "mart_daily_metrics" in sql:
            rows = []
            for i in range(n_instruments):
                rows.append(
                    {
                        "instrument_uid": uid_of(i),
                        "close": 100.0 + i,
                        "ret_1d": 1.5 + i,
                        "ret_5d": -2.0 + i,
                        "ret_20d": 10.0,
                        "volume_ratio": 3.0 if i == 0 else 1.0,
                        "rsi14": 75.0 if i == 0 else (25.0 if i == 1 else 50.0),
                        "gap_pct": 0.4,
                    }
                )
            return FakeResult(rows)
        if "fundamentals_snapshots" in sql:
            return FakeResult(
                [{"asset_uid": a, "pe": 3.45} for a in set(asset_of_uid.values())]
            )
        if "market_data.events" in sql:
            return FakeResult(
                [
                    {
                        "instrument_uid": uid_of(0),
                        "expected_date": date(2026, 12, 21),
                        "event_type": "expiry",
                    },
                    {
                        "instrument_uid": uid_of(1),
                        "expected_date": date(2026, 10, 28),
                        "event_type": "report",
                    },
                ]
            )
        raise AssertionError(f"неожиданный запрос: {sql[:80]}")

    class FakeClient:
        def __init__(self):
            self.queries = []

        def query(self, sql, parameters=None):
            self.queries.append(sql)
            return query(sql, parameters)

    return FakeClient()


def test_digest_contains_all_tickers(monkeypatch, tmp_path):
    s = make_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(report, "get_client", lambda settings: fake_client())
    text = build_digest(s)
    for t in report._load_instruments():
        assert t in text, f"тикер {t} отсутствует"


def test_digest_percent_signs_and_metrics(monkeypatch, tmp_path):
    s = make_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(report, "get_client", lambda settings: fake_client())
    text = build_digest(s)
    assert "+1.50%" in text   # ret_1d с плюсом
    assert "-2.00%" in text   # ret_5d с минусом
    assert "RSI14" in text
    assert "перекуплен" in text  # rsi 75
    assert "перепродан" in text  # rsi 25
    assert "×3.00" in text and "⚠" in text  # volume_ratio > 2
    assert "P/E TTM 3.45" in text


def test_digest_header_date_format(monkeypatch, tmp_path):
    s = make_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(report, "get_client", lambda settings: fake_client())
    text = build_digest(s)
    assert f"данные за {TODAY.isoformat()}" in text


def test_digest_event_format(monkeypatch, tmp_path):
    s = make_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(report, "get_client", lambda settings: fake_client())
    text = build_digest(s)
    assert "📅 2026-12-21 expiry SBER" in text
    assert "📅 2026-10-28 report GAZP" in text


def test_digest_len_limit(monkeypatch, tmp_path):
    s = make_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(report, "get_client", lambda settings: fake_client())
    text = build_digest(s)
    assert len(text) <= MAX_LEN


def test_digest_truncates_events_on_overflow(monkeypatch, tmp_path):
    """При переполнении список событий режется, но лимит соблюдается."""
    s = make_settings(monkeypatch, tmp_path)
    client = fake_client()

    orig_query = client.query

    def query(sql, parameters=None):
        if "market_data.events" not in sql:
            return orig_query(sql, parameters)
        # Много событий → блоки распухают за MAX_LEN.
        rows = [
            {
                "instrument_uid": f"uid{i % 5}",
                "expected_date": date(2026, 11, 1 + i // 5),
                "event_type": "dividend",
            }
            for i in range(100)
        ]

        class FakeResult:
            def named_results(self):
                yield from rows

        return FakeResult()

    client.query = query
    monkeypatch.setattr(report, "get_client", lambda settings: client)
    text = build_digest(s)
    assert len(text) <= MAX_LEN
    # События срезаны — их не может быть 100 штук.
    assert text.count("📅") < 100


def test_digest_rsi_neutral(monkeypatch, tmp_path):
    s = make_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(report, "get_client", lambda settings: fake_client())
    text = build_digest(s)
    assert "нейтрально" in text  # rsi 50 у третьего инструмента
