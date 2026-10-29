"""Тесты replay-движка бэктеста (мок-клиент ClickHouse, без живого CH)."""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from mdgrabber.backtest import (  # noqa: E402
    BACKTEST_TABLE,
    persist_backtest,
    replay_signals,
)
from mdgrabber.signals import (  # noqa: E402
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    VOLUME_SPIKE_RATIO,
)

UID = "uid-1"
TICKER = "SBER"
START = date(2026, 1, 1)


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


def _bar(i: int, **overrides) -> dict:
    """Строка витрины на бар i (0-indexed) с индикаторами по умолчанию."""
    base = dict(close=100.0, volume_ratio=1.0, rsi14=55.0, ma20=100.0, ma50=95.0)
    base.update(overrides)
    return dict(instrument_uid=UID, bar_date=START + timedelta(days=i), **base)


def _series(overrides_by_index: dict[int, dict] | None = None, n: int = 30) -> list[dict]:
    overrides_by_index = overrides_by_index or {}
    return [
        _bar(i, **overrides_by_index.get(i, {})) for i in range(n)
    ]


def _replay(bars: list[dict]) -> list[dict]:
    client = FakeClient(bars)
    return replay_signals(client, {UID: TICKER})


# ---------- ma_cross_up на запрограммированном баре ----------

def test_ma_cross_up_exactly_on_bar_20():
    # бар 19: close ниже MA20; бар 20: close >= MA20 -> ровно один ma_cross_up;
    # бары 21+ держат close выше MA20, чтобы не словить обратный кросс
    bars = _series({
        19: dict(close=99.0),
        20: dict(close=100.0),
        **{i: dict(close=102.0) for i in range(21, 30)},
    })
    signals = _replay(bars)

    assert len(signals) == 1
    s = signals[0]
    assert s["signal_type"] == "ma_cross_up"
    assert s["bar_date"] == str(START + timedelta(days=20))
    assert s["value"] == 0.0
    assert s["ticker"] == TICKER


def test_forward_returns_on_trading_bars():
    # fwd по торговым барам: close(T+1)/close(T)-1, close(T+5)/close(T)-1
    bars = _series({
        19: dict(close=99.0),
        20: dict(close=100.0),
        **{i: dict(close=102.0) for i in range(21, 30)},
    })
    signals = _replay(bars)
    s = signals[0]

    assert s["fwd_ret_1d"] == 102.0 / 100.0 - 1.0        # бар 21 (close=102)
    assert s["fwd_ret_5d"] == 102.0 / 100.0 - 1.0        # бар 25 (close=102)
    assert s["fwd_ret_20d"] is None                      # бар 40 за концом истории (30 баров)


# ---------- lookahead-гвард ----------

def test_signal_on_last_bar_all_fwd_none():
    n = 10
    bars = _series({n - 1: dict(rsi14=75.0)}, n=n)
    signals = _replay(bars)

    assert len(signals) == 1
    s = signals[0]
    assert s["signal_type"] == "rsi_overbought"
    assert s["bar_date"] == str(START + timedelta(days=n - 1))
    assert s["fwd_ret_1d"] is None
    assert s["fwd_ret_5d"] is None
    assert s["fwd_ret_20d"] is None


def test_fwd_null_only_beyond_history_edge():
    # сигнал на баре n-20 (0-indexed: 28 из 30): fwd_20d -> бар 48 > края -> None,
    # fwd_1d/5d считаются
    bars = _series({28: dict(rsi14=RSI_OVERBOUGHT + 1)})
    signals = _replay(bars)
    s = [x for x in signals if x["signal_type"] == "rsi_overbought"][0]

    assert s["bar_date"] == str(START + timedelta(days=28))
    assert s["fwd_ret_1d"] == 0.0   # бар 29: close=100, как и на баре 28
    assert s["fwd_ret_5d"] is None  # бар 33 за концом истории (30 баров)
    assert s["fwd_ret_20d"] is None


# ---------- NULL rsi/ma пропущены ----------

def test_bars_with_null_indicators_skipped():
    bars = [
        _bar(0),                                      # валидный нейтральный
        _bar(1, rsi14=None, close=101.0),             # NULL rsi: кросс-условия есть, бар пропущен
        _bar(2, ma20=None),                           # NULL ma20: пропущен
        _bar(3),                                      # валидный нейтральный
    ]
    assert _replay(bars) == []


def test_null_rsi_bar_not_used_as_cross_prev():
    # бар 1 с NULL rsi между двумя валидными: кросс не должен считаться
    # от бар 0 напрямую к бар 2 (prev — последний ВАЛИДНЫЙ бар)
    bars = [
        _bar(0, close=99.0, ma20=100.0),
        _bar(1, close=100.0, rsi14=None),             # невалидный, close уже >= ma20
        _bar(2, close=101.0),                         # от prev=бар0 кросса нет (prev close уже >=? нет)
    ]
    signals = _replay(bars)
    # prev для бара 2 = бар 0 (close 99 < ma20 100) и бар 2 close 101 >= 100 ->
    # ma_cross_up на баре 2, но никак не на баре 1
    assert all(s["bar_date"] != str(START + timedelta(days=1)) for s in signals)
    assert all(s["signal_type"] != "ma_cross_up" or s["bar_date"] == str(START + timedelta(days=2))
               for s in signals)


# ---------- пороги из signals.py ----------

def test_rsi_overbought_threshold_uses_signals_constant():
    bars = _series({5: dict(rsi14=RSI_OVERBOUGHT)})  # ровно порог -> сигнал
    signals = [s for s in _replay(bars) if s["signal_type"] == "rsi_overbought"]
    assert len(signals) == 1
    assert signals[0]["value"] == float(RSI_OVERBOUGHT)
    assert signals[0]["bar_date"] == str(START + timedelta(days=5))


def test_rsi_below_overbought_no_signal():
    bars = _series({5: dict(rsi14=RSI_OVERBOUGHT - 0.1)})
    assert _replay(bars) == []


def test_rsi_oversold_threshold_uses_signals_constant():
    bars = _series({7: dict(rsi14=RSI_OVERSOLD)})
    signals = [s for s in _replay(bars) if s["signal_type"] == "rsi_oversold"]
    assert len(signals) == 1
    assert signals[0]["value"] == float(RSI_OVERSOLD)


def test_rsi_above_oversold_no_signal():
    bars = _series({7: dict(rsi14=RSI_OVERSOLD + 0.1)})
    assert _replay(bars) == []


def test_volume_spike_threshold_uses_signals_constant():
    bars = _series({3: dict(volume_ratio=VOLUME_SPIKE_RATIO)})
    signals = [s for s in _replay(bars) if s["signal_type"] == "volume_spike"]
    assert len(signals) == 1
    assert signals[0]["value"] == float(VOLUME_SPIKE_RATIO)


def test_volume_below_spike_no_signal():
    bars = _series({3: dict(volume_ratio=VOLUME_SPIKE_RATIO - 0.01)})
    assert _replay(bars) == []


def test_null_volume_ratio_no_spike():
    bars = _series({3: dict(volume_ratio=None)})
    assert _replay(bars) == []


# ---------- replay: пустой вход ----------

def test_replay_empty_instruments():
    client = FakeClient([_bar(0)])
    assert replay_signals(client, {}) == []


def test_replay_reads_full_history_ordered():
    # клиент вернёт бары в произвольном порядке — replay сортирует сам
    bars = _series({19: dict(close=99.0), **{i: dict(close=102.0) for i in range(20, 30)}})
    shuffled = [bars[20], bars[0], bars[19]] + bars[1:19] + bars[21:]
    signals = _replay(shuffled)
    assert len(signals) == 1
    assert signals[0]["bar_date"] == str(START + timedelta(days=20))


# ---------- persist_backtest ----------

def _row(**overrides) -> dict:
    base = dict(
        instrument_uid=UID, ticker=TICKER, signal_type="rsi_overbought",
        bar_date="2026-01-06", value=71.5,
        fwd_ret_1d=0.01, fwd_ret_5d=None, fwd_ret_20d=-0.02,
    )
    base.update(overrides)
    return base


def test_persist_builds_correct_insert_sql():
    client = FakeClient()
    persist_backtest(client, [_row()])

    assert len(client.commands) == 1
    sql = client.commands[0]
    assert f"INSERT INTO {BACKTEST_TABLE}" in sql
    assert "(instrument_uid, ticker, signal_type, bar_date, value, " \
           "fwd_ret_1d, fwd_ret_5d, fwd_ret_20d)" in sql
    values_part = sql.split("VALUES", 1)[1]
    assert "'SBER'" in values_part
    assert "'rsi_overbought'" in values_part
    assert "'2026-01-06'" in values_part
    assert "NULL" in values_part          # fwd_ret_5d = None
    assert "0.01" in values_part and "-0.02" in values_part


def test_persist_empty_noop():
    client = FakeClient()
    persist_backtest(client, [])
    assert client.commands == []


def test_persist_batches_rows():
    client = FakeClient()
    persist_backtest(client, [_row(bar_date=f"2026-01-{d:02d}") for d in (6, 7, 8)])
    assert len(client.commands) == 1  # 3 строки < BATCH_SIZE -> одна пачка
    assert client.commands[0].count("INSERT INTO") == 1
    values_part = client.commands[0].split("VALUES", 1)[1]
    assert "'2026-01-06'" in values_part
    assert "'2026-01-07'" in values_part
    assert "'2026-01-08'" in values_part
