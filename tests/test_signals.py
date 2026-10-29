"""Тесты движка сигналов (мок-клиент ClickHouse, без живого CH)."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from mdgrabber.signals import detect_signals, mark_sent, persist_new  # noqa: E402

UID = "uid-1"
TICKER = "SBER"


class FakeResult:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def named_results(self):
        return iter(self._rows)


class FakeClient:
    """Мок: query возвращает заготовленные строки, command копит SQL."""

    def __init__(self, rows: list[dict] | None = None):
        self.rows = rows or []
        self.queries: list[str] = []
        self.commands: list[str] = []

    def query(self, sql: str) -> FakeResult:
        self.queries.append(sql)
        return FakeResult(self.rows)

    def command(self, sql: str) -> None:
        self.commands.append(sql)


def _bars(today: dict, yesterday: dict) -> list[dict]:
    """Строки витрины: сегодня и вчера (клиент вернёт их в любом порядке)."""
    y, t = dict(yesterday), dict(today)
    y.update(instrument_uid=UID, bar_date=date(2026, 10, 28))
    t.update(instrument_uid=UID, bar_date=date(2026, 10, 29))
    return [t, y]


BASE_Y = dict(close=100.0, volume_ratio=1.0, rsi14=55.0, ma20=100.0, ma50=95.0)
BASE_T = dict(close=100.0, volume_ratio=1.0, rsi14=55.0, ma20=100.0, ma50=95.0)


def _detect(rows: list[dict]) -> list[dict]:
    client = FakeClient(rows)
    return detect_signals(client, {UID: TICKER})


def _types(signals: list[dict]) -> set[str]:
    return {s["signal_type"] for s in signals}


# ---------- RSI ----------

def test_rsi_overbought():
    rows = _bars({**BASE_T, "rsi14": 74.9}, BASE_Y)
    signals = _detect(rows)
    assert _types(signals) == {"rsi_overbought"}
    assert signals[0]["value"] == 74.9
    assert signals[0]["bar_date"] == "2026-10-29"


def test_rsi_oversold():
    rows = _bars({**BASE_T, "rsi14": 28.1}, BASE_Y)
    signals = _detect(rows)
    assert _types(signals) == {"rsi_oversold"}
    assert signals[0]["value"] == 28.1


def test_rsi_neutral_no_signal():
    rows = _bars({**BASE_T, "rsi14": 55.0}, BASE_Y)
    assert _detect(rows) == []


# ---------- объём ----------

def test_volume_spike():
    rows = _bars({**BASE_T, "volume_ratio": 2.3}, BASE_Y)
    signals = _detect(rows)
    assert _types(signals) == {"volume_spike"}
    assert signals[0]["value"] == 2.3


def test_volume_normal_no_signal():
    rows = _bars({**BASE_T, "volume_ratio": 1.1}, BASE_Y)
    assert _detect(rows) == []


# ---------- close x MA20 ----------

def test_ma_cross_up():
    rows = _bars({**BASE_T, "close": 101.0, "ma20": 100.0},
                 {**BASE_Y, "close": 99.0, "ma20": 100.0})
    signals = _detect(rows)
    assert _types(signals) == {"ma_cross_up"}
    assert signals[0]["value"] == 0


def test_ma_cross_down():
    rows = _bars({**BASE_T, "close": 99.0, "ma20": 100.0},
                 {**BASE_Y, "close": 101.0, "ma20": 100.0})
    signals = _detect(rows)
    assert _types(signals) == {"ma_cross_down"}


def test_no_ma_cross():
    rows = _bars({**BASE_T, "close": 105.0, "ma20": 100.0},
                 {**BASE_Y, "close": 104.0, "ma20": 100.0})
    assert _detect(rows) == []


# ---------- MA20 x MA50 ----------

def test_golden_cross():
    rows = _bars({**BASE_T, "close": 105.0, "ma20": 101.0, "ma50": 100.0},
                 {**BASE_Y, "close": 104.0, "ma20": 99.0, "ma50": 100.0})
    signals = _detect(rows)
    assert _types(signals) == {"golden_cross"}
    assert signals[0]["value"] == 0


def test_death_cross():
    rows = _bars({**BASE_T, "close": 104.0, "ma20": 99.0, "ma50": 100.0},
                 {**BASE_Y, "close": 104.0, "ma20": 101.0, "ma50": 100.0})
    signals = _detect(rows)
    assert _types(signals) == {"death_cross"}
    assert signals[0]["value"] == 0


def test_no_golden_death_cross():
    rows = _bars({**BASE_T, "ma20": 103.0, "ma50": 100.0},
                 {**BASE_Y, "ma20": 102.0, "ma50": 100.0})
    assert _detect(rows) == []


# ---------- один бар: кроссов нет ----------

def test_single_bar_no_crosses():
    client = FakeClient([dict(instrument_uid=UID, bar_date=date(2026, 10, 29), **BASE_T)])
    signals = detect_signals(client, {UID: TICKER})
    assert signals == []


# ---------- persist_new: дедуп ----------

def _signal(signal_type: str, bar_date: str) -> dict:
    return {
        "instrument_uid": UID,
        "ticker": TICKER,
        "signal_type": signal_type,
        "bar_date": bar_date,
        "value": 1.0,
        "message": f"🔴 {TICKER}: test",
    }


def test_persist_new_skips_existing_and_returns_inserted():
    # 3 сигнала, 2 уже в CH -> вставлен/возвращён 1
    client = FakeClient(rows=[
        dict(instrument_uid=UID, signal_type="rsi_overbought", bar_date=date(2026, 10, 29)),
        dict(instrument_uid=UID, signal_type="volume_spike", bar_date=date(2026, 10, 29)),
    ])
    signals = [
        _signal("rsi_overbought", "2026-10-29"),   # уже есть
        _signal("rsi_oversold", "2026-10-29"),     # новый
        _signal("volume_spike", "2026-10-29"),     # уже есть
    ]
    inserted = persist_new(client, signals)

    assert len(inserted) == 1
    assert inserted[0]["signal_type"] == "rsi_oversold"
    # 1 INSERT: только новый сигнал в VALUES, sent=0
    assert len(client.commands) == 1
    insert_sql = client.commands[0]
    assert "INSERT INTO" in insert_sql
    values_part = insert_sql.split("VALUES", 1)[1]
    assert "rsi_oversold" in values_part
    assert "rsi_overbought" not in values_part
    assert "volume_spike" not in values_part
    assert ", 0)" in values_part


def test_persist_new_empty_input_no_queries():
    client = FakeClient()
    assert persist_new(client, []) == []
    assert client.queries == [] and client.commands == []


# ---------- mark_sent ----------

def test_mark_sent_inserts_same_keys_with_sent_1():
    client = FakeClient()
    mark_sent(client, [_signal("rsi_overbought", "2026-10-29")])
    assert len(client.commands) == 1
    sql = client.commands[0]
    assert "INSERT INTO" in sql
    assert ", 1)" in sql  # sent=1 в конце строки VALUES


def test_mark_sent_empty_noop():
    client = FakeClient()
    mark_sent(client, [])
    assert client.commands == []


# ---------- сообщения ----------

def test_message_contains_ticker_and_value():
    rows = _bars({**BASE_T, "rsi14": 74.9}, BASE_Y)
    signals = _detect(rows)
    msg = signals[0]["message"]
    assert TICKER in msg
    assert "74.9" in msg


def test_messages_all_types_contain_ticker():
    rows = _bars({**BASE_T, "rsi14": 74.9, "volume_ratio": 2.3,
                  "close": 101.0, "ma20": 100.0, "ma50": 99.0},
                 {**BASE_Y, "rsi14": 55.0, "volume_ratio": 1.0,
                  "close": 99.0, "ma20": 100.0, "ma50": 100.0})
    signals = _detect(rows)
    assert len(signals) >= 3
    for s in signals:
        assert TICKER in s["message"]
        assert s["message"][0] != ""  # начинается с эмодзи
