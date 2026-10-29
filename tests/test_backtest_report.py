"""Тесты для scripts/run_backtest.py (моки, без живых CH/Telegram)."""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import scripts.run_backtest as rb  # noqa: E402

ALL_TYPES = [
    "rsi_overbought", "rsi_oversold", "volume_spike",
    "ma_cross_up", "ma_cross_down", "golden_cross", "death_cross",
]


def make_row(ticker="SBER", signal_type="rsi_overbought", bar_date="2026-01-05",
             fwd1=0.01, fwd5=0.02, fwd20=0.03):
    return {
        "ticker": ticker,
        "signal_type": signal_type,
        "bar_date": bar_date,
        "fwd_ret_1d": fwd1,
        "fwd_ret_5d": fwd5,
        "fwd_ret_20d": fwd20,
    }


def mock_client(rows):
    """Клиент с query() -> named_results(rows) и command() как MagicMock."""
    client = MagicMock()
    client.query.return_value = SimpleNamespace(named_results=lambda: iter(rows))
    return client


# ---------- статистика: точные числа на 5 строках ----------

def test_stats_exact_numbers():
    rows = [
        make_row(fwd1=0.01, fwd5=0.02, fwd20=0.05),
        make_row(fwd1=0.02, fwd5=0.02, fwd20=0.05),
        make_row(fwd1=0.03, fwd5=0.02, fwd20=0.05),
        make_row(fwd1=0.04, fwd5=-0.01, fwd20=-0.05),
        make_row(fwd1=0.05, fwd5=0.07, fwd20=0.10),
    ]
    s = rb.group_stats(rows)
    assert s["count"] == 5
    assert s["win_rate_5d"] == pytest.approx(4 / 5)
    assert s["mean_1d"] == pytest.approx(3.0)      # mean(1..5)% = 3%
    assert s["median_1d"] == pytest.approx(3.0)
    assert s["mean_5d"] == pytest.approx(2.4)      # (2+2+2-1+7)/5
    assert s["median_5d"] == pytest.approx(2.0)
    assert s["mean_20d"] == pytest.approx(4.0)     # (5+5+5-5+10)/5
    assert s["median_20d"] == pytest.approx(5.0)


def test_quantiles_p10_p90():
    rows = [make_row(fwd5=v / 100.0) for v in range(10, 91, 10)]  # 10..90%, шаг 10
    s = rb.group_stats(rows)
    p10, p90 = s["p10_p90_5d"]
    assert p10 == pytest.approx(10.0, abs=1e-6)
    assert p90 == pytest.approx(80.0, abs=1e-6)  # 9 точек: срезы quantiles(n=10) на данных


def test_quantiles_needs_two_points():
    s = rb.group_stats([make_row(fwd5=0.05)])
    assert s["p10_p90_5d"] is None


# ---------- NULL-обработка ----------

def test_null_fwd_excluded_from_mean_but_counted():
    rows = [
        make_row(fwd1=0.01, fwd5=0.02),
        make_row(fwd1=0.03, fwd5=0.04),
        make_row(fwd1=None, fwd5=None, fwd20=None),
        make_row(fwd1=None, fwd5=None, fwd20=None),
        make_row(fwd1=0.02, fwd5=None, fwd20=0.06),
    ]
    s = rb.group_stats(rows)
    assert s["count"] == 5
    assert s["missing"] == 3                       # «нет данных» по NULL fwd_5d
    assert s["mean_1d"] == pytest.approx(2.0)      # только 3 не-NULL: (1+3+2)/3
    assert s["median_5d"] == pytest.approx(3.0)    # только 2 не-NULL: (2+4)/2
    assert s["mean_20d"] == pytest.approx(4.0)     # только 3 не-NULL: (3+3+6)/3
    assert s["win_rate_5d"] == 1.0                 # оба не-NULL > 0


def test_compute_stats_groups_by_type():
    rows = [
        make_row(signal_type="rsi_overbought", fwd5=0.01),
        make_row(signal_type="rsi_oversold", fwd5=-0.01),
        make_row(signal_type="rsi_overbought", fwd5=0.03),
    ]
    stats = rb.compute_stats(rows)
    assert set(stats) == {"rsi_overbought", "rsi_oversold"}
    assert stats["rsi_overbought"]["count"] == 2
    assert stats["rsi_overbought"]["win_rate_5d"] == 1.0
    assert stats["rsi_oversold"]["win_rate_5d"] == 0.0


# ---------- формат сообщения ----------

def test_message_contains_all_seven_types():
    rows = [make_row(signal_type=st, bar_date="2026-01-05") for st in ALL_TYPES]
    rows.append(make_row(signal_type="rsi_overbought", bar_date="2026-03-10"))
    msg = rb.build_message(rows)
    assert "📊" in msg
    assert "история: 2026-01-05..2026-03-10" in msg
    for st in ALL_TYPES:
        assert st in msg
    assert "<pre>" in msg
    # топ-3 типа по count получают блок по тикерам
    assert msg.count("по тикерам") == 3


def test_message_subset_of_types():
    rows = [make_row(signal_type="volume_spike"), make_row(signal_type="volume_spike")]
    msg = rb.build_message(rows)
    assert "volume_spike" in msg
    assert "rsi_overbought" not in msg


def test_message_percent_format():
    rows = [make_row(signal_type="golden_cross", fwd1=0.12345, fwd5=0.05, fwd20=-0.02)]
    msg = rb.build_message(rows)
    assert "12.35" in msg           # mean_1d в %
    assert "5.00" in msg
    assert "win5d" in msg and "p10_5d" in msg and "нетДанных" in msg


# ---------- dry-режим и main ----------

@pytest.fixture
def _patched(monkeypatch):
    settings = SimpleNamespace(admin_chat_id="111")
    monkeypatch.setattr(rb, "Settings", SimpleNamespace(load=lambda: settings))
    return settings


def test_no_send_does_not_call_send_telegram(monkeypatch, _patched):
    sent = MagicMock()
    monkeypatch.setattr(rb, "send_telegram", sent)
    client = mock_client([make_row(signal_type="volume_spike")])
    monkeypatch.setattr(rb, "get_client", lambda settings: client)

    rc = rb.main(["--report", "--no-send"])

    assert rc == 0
    sent.assert_not_called()
    client.command.assert_called()  # запись в etl_runs выполнена


def test_report_calls_send_telegram_with_ops_html(monkeypatch, _patched):
    sent = MagicMock()
    monkeypatch.setattr(rb, "send_telegram", sent)
    client = mock_client([make_row(signal_type="volume_spike")])
    monkeypatch.setattr(rb, "get_client", lambda settings: client)

    rb.main(["--report"])

    sent.assert_called_once()
    kwargs = sent.call_args.kwargs
    assert kwargs["category"] == "ops"
    assert kwargs["parse_mode"] == "HTML"


def test_default_runs_both_phases(monkeypatch, _patched):
    monkeypatch.setattr(rb, "send_telegram", MagicMock())
    replayed = MagicMock(return_value=[make_row()])
    monkeypatch.setattr(rb, "replay_signals", replayed)
    monkeypatch.setattr(rb, "persist_backtest", MagicMock())
    monkeypatch.setattr(rb, "load_instruments", lambda path: {"uid-1": "SBER"})
    client = mock_client([make_row()])
    monkeypatch.setattr(rb, "get_client", lambda settings: client)

    rc = rb.main([])

    assert rc == 0
    replayed.assert_called_once()
    rb.persist_backtest.assert_called_once()
    rb.send_telegram.assert_called_once()          # отчёт отправлен
    assert client.command.call_count >= 1          # запись в etl_runs


def test_replay_only_skips_report(monkeypatch, _patched):
    monkeypatch.setattr(rb, "send_telegram", MagicMock())
    monkeypatch.setattr(rb, "replay_signals", MagicMock(return_value=[]))
    monkeypatch.setattr(rb, "persist_backtest", MagicMock())
    monkeypatch.setattr(rb, "load_instruments", lambda path: {})
    client = mock_client([])
    monkeypatch.setattr(rb, "get_client", lambda settings: client)

    rb.main(["--replay"])

    rb.send_telegram.assert_not_called()
    client.query.assert_not_called()


def test_empty_report_sends_nothing(monkeypatch, _patched):
    sent = MagicMock()
    monkeypatch.setattr(rb, "send_telegram", sent)
    client = mock_client([])
    monkeypatch.setattr(rb, "get_client", lambda settings: client)

    rb.main(["--report", "--no-send"])
    sent.assert_not_called()


def test_record_run_writes_backtest_job():
    client = MagicMock()
    started = __import__("datetime").datetime(2026, 10, 29, 9, 0, 0)
    rb.record_run(client, "ok", 42, started)
    sql = client.command.call_args.args[0]
    assert "'backtest'" in sql
    assert "42" in sql
    assert "'ok'" in sql
