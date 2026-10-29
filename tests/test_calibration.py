"""Тесты калибровки сигналов (P4.3): детекция 7 типов сохранена, доставляются только 4.

Моки, без живых CH/Telegram/Grafana.
"""
import os
import sys
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
    sys.path.insert(0, os.path.join(ROOT, "src"))

from mdgrabber.signals import (  # noqa: E402
    SIGNAL_CALIBRATION,
    calibration,
    detect_signals,
)
import scripts.run_signals as rs  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp_users_db(tmp_path, monkeypatch):
    """Изоляция: init_db в main() пишет в tmp, а не в живой data/bot.sqlite."""
    from mdgrabber import users as users_mod

    monkeypatch.setattr(users_mod, "DB_PATH", tmp_path / "test-users.sqlite")


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def named_results(self):
        return iter(self._rows)


class FakeClient:
    def __init__(self, rows=None):
        self._rows = rows or []

    def query(self, sql):
        return FakeResult(self._rows)

    def command(self, sql):
        pass


class EmptyClient:
    """persist_new/fetch_unsent видят пусто: force-режим доставляет то, что детектилось."""

    def query(self, sql):
        return FakeResult([])

    def command(self, sql):
        pass


UID = "uid-1"
TICKER = "SBER"

BASE_Y = dict(close=100.0, volume_ratio=1.0, rsi14=55.0, ma20=100.0, ma50=95.0)
BASE_T = dict(close=100.0, volume_ratio=1.0, rsi14=55.0, ma20=100.0, ma50=95.0)


def _bars(today: dict, yesterday: dict) -> list[dict]:
    y, t = dict(yesterday), dict(today)
    y.update(instrument_uid=UID, bar_date=date(2026, 10, 28))
    t.update(instrument_uid=UID, bar_date=date(2026, 10, 29))
    return [t, y]


# Паттерн баров для каждого из 7 типов (детекция не менялась — все 7 детектятся)
PATTERNS = {
    "rsi_overbought":  ({**BASE_T, "rsi14": 74.9}, BASE_Y),
    "rsi_oversold":    ({**BASE_T, "rsi14": 28.1}, BASE_Y),
    "volume_spike":    ({**BASE_T, "volume_ratio": 2.3}, BASE_Y),
    "ma_cross_up":     ({**BASE_T, "close": 101.0}, {**BASE_Y, "close": 99.0}),
    "ma_cross_down":   ({**BASE_T, "close": 99.0}, {**BASE_Y, "close": 101.0}),
    "golden_cross":    ({**BASE_T, "close": 105.0, "ma20": 101.0, "ma50": 100.0}, {**BASE_Y, "close": 104.0, "ma20": 99.0, "ma50": 100.0}),
    "death_cross":     ({**BASE_T, "close": 104.0, "ma20": 99.0, "ma50": 100.0}, {**BASE_Y, "close": 104.0, "ma20": 101.0, "ma50": 100.0}),
}


# ---------- детекция: все 7 типов по-прежнему детектятся ----------

@pytest.mark.parametrize("signal_type", list(PATTERNS))
def test_detection_still_returns_all_7_types(signal_type):
    today, yesterday = PATTERNS[signal_type]
    signals = detect_signals(FakeClient(_bars(today, yesterday)), {UID: TICKER})
    assert [s["signal_type"] for s in signals] == [signal_type]


def test_calibration_covers_all_7_types():
    assert set(SIGNAL_CALIBRATION) == set(PATTERNS)


# ---------- importance в сигнал-дикт ----------

def test_death_cross_importance_3():
    today, yesterday = PATTERNS["death_cross"]
    signals = detect_signals(FakeClient(_bars(today, yesterday)), {UID: TICKER})
    assert signals[0]["importance"] == 3
    assert calibration("death_cross")["deliver"] is True


def test_ma_cross_up_importance_2():
    today, yesterday = PATTERNS["ma_cross_up"]
    signals = detect_signals(FakeClient(_bars(today, yesterday)), {UID: TICKER})
    assert signals[0]["importance"] == 2


def test_calibration_unknown_type_defaults_to_no_delivery():
    assert calibration("nonexistent_type") == {"deliver": False, "importance": 0}


def test_exactly_4_types_deliverable():
    delivered = {t for t, c in SIGNAL_CALIBRATION.items() if c["deliver"]}
    assert delivered == {"ma_cross_up", "death_cross", "ma_cross_down", "rsi_oversold"}


# ---------- фильтр калибровки ----------

def make_signal(signal_type: str, ticker: str = "SBER", bar_date="2026-10-29"):
    return {
        "instrument_uid": f"uid-{ticker}",
        "ticker": ticker,
        "signal_type": signal_type,
        "bar_date": bar_date,
        "value": 1.0,
        "message": f"{ticker}: test",
    }


def test_filter_calibrated_keeps_only_deliverable_and_counts():
    mixed = [
        make_signal("ma_cross_up"),
        make_signal("death_cross", "GAZP"),
        make_signal("golden_cross"),
        make_signal("rsi_overbought", "GAZP"),
        make_signal("volume_spike", "LKOH"),
    ]
    deliverable, filtered = rs.filter_calibrated(mixed)
    assert [s["signal_type"] for s in deliverable] == ["ma_cross_up", "death_cross"]
    assert filtered == 3


# ---------- рантайм ----------

def _patch_runtime(monkeypatch, settings, signals, subscribers=("222",)):
    monkeypatch.setattr(rs, "Settings", SimpleNamespace(load=lambda: settings))
    monkeypatch.setattr(rs, "get_client", lambda s: EmptyClient())
    monkeypatch.setattr(rs, "detect_signals", lambda client, instruments: signals)
    monkeypatch.setattr(rs, "list_subscribers", lambda: list(subscribers))
    monkeypatch.setattr(
        rs, "list_subscriptions",
        lambda chat_id: [{"instrument": "*", "signal_type": "*", "min_importance": 0}],
    )


def test_nondeliverable_types_not_broadcast(monkeypatch):
    """golden_cross / rsi_overbought / volume_spike: мок broadcast не получает их."""
    settings = SimpleNamespace(admin_chat_id="111")
    signals = [
        make_signal("golden_cross"),
        make_signal("rsi_overbought", "GAZP"),
        make_signal("volume_spike", "LKOH"),
    ]
    sent = MagicMock()
    _patch_runtime(monkeypatch, settings, signals)
    monkeypatch.setattr(rs, "send_telegram", sent)

    rc = rs.main(["--force"])

    assert rc == 0
    sent.assert_not_called()


def test_deliverable_reaches_broadcast(monkeypatch):
    """death_cross и ma_cross_up (калиброванные) доходят до рассылки."""
    settings = SimpleNamespace(admin_chat_id="111")
    signals = [make_signal("death_cross"), make_signal("ma_cross_up", "GAZP")]
    sent = MagicMock()
    _patch_runtime(monkeypatch, settings, signals)
    monkeypatch.setattr(rs, "send_telegram", sent)

    rc = rs.main(["--force"])

    assert rc == 0
    sent.assert_called_once()
    text = sent.call_args.kwargs["text"]
    assert "SBER" in text and "GAZP" in text
    assert "death_cross" in text and "ma_cross_up" in text


def test_calibration_filter_does_not_break_subscription_filters(monkeypatch):
    """SBER-подписчик получает calibrated SBER-сигнал; чужой тикер — нет."""
    settings = SimpleNamespace(admin_chat_id="111")
    signals = [make_signal("death_cross"), make_signal("death_cross", "GAZP")]
    sent = MagicMock()
    monkeypatch.setattr(rs, "Settings", SimpleNamespace(load=lambda: settings))
    monkeypatch.setattr(rs, "get_client", lambda s: EmptyClient())
    monkeypatch.setattr(rs, "detect_signals", lambda client, instruments: signals)
    monkeypatch.setattr(rs, "list_subscribers", lambda: ["222"])
    monkeypatch.setattr(
        rs, "list_subscriptions",
        lambda chat_id: [{"instrument": "SBER", "signal_type": "*", "min_importance": 0}],
    )
    monkeypatch.setattr(rs, "send_telegram", sent)

    rc = rs.main(["--force"])

    assert rc == 0
    text = sent.call_args.kwargs["text"]
    assert "SBER" in text
    assert "GAZP" not in text


def test_annotations_and_mark_sent_only_for_deliverable(monkeypatch):
    """Grafana-аннотации и mark_sent — только доставляемые типы."""
    settings = SimpleNamespace(admin_chat_id="111")
    signals = [
        make_signal("ma_cross_up"),
        make_signal("golden_cross"),
        make_signal("volume_spike", "GAZP"),
    ]
    annotate = MagicMock()
    mark_sent = MagicMock()
    _patch_runtime(monkeypatch, settings, signals)
    monkeypatch.setattr(rs, "send_telegram", MagicMock())
    monkeypatch.setattr(rs, "post_annotation", annotate)
    monkeypatch.setattr(rs, "mark_sent", mark_sent)

    rc = rs.main(["--force"])

    assert rc == 0
    annotated_types = {c.args[0]["signal_type"] for c in annotate.call_args_list}
    assert annotated_types == {"ma_cross_up"}
    marked = mark_sent.call_args.args[1]
    assert [s["signal_type"] for s in marked] == ["ma_cross_up"]
