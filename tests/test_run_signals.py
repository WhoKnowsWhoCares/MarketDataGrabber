"""Тесты для scripts/run_signals.py (моки, без живых CH/Telegram/Grafana)."""
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import scripts.run_signals as rs  # noqa: E402


def make_signal(ticker: str, signal_type: str = "rsi_overbought", bar_date="2026-10-28"):
    return {
        "instrument_uid": f"uid-{ticker}",
        "ticker": ticker,
        "signal_type": signal_type,
        "bar_date": bar_date,
        "value": 75.0,
        "message": f"{ticker}: test signal",
    }


SBER_SIG = make_signal("SBER")
GAZP_SIG = make_signal("GAZP", "rsi_oversold")


# ---------- фильтрация по подпискам ----------

def test_sber_wildcard_type_gets_only_sber():
    subs = [{"instrument": "SBER", "signal_type": "*", "min_importance": 0}]
    assert rs.filter_for_subscriber(subs, [SBER_SIG, GAZP_SIG]) == [SBER_SIG]


def test_full_wildcard_gets_all():
    subs = [{"instrument": "*", "signal_type": "*", "min_importance": 0}]
    assert rs.filter_for_subscriber(subs, [SBER_SIG, GAZP_SIG]) == [SBER_SIG, GAZP_SIG]


def test_gazp_subscription_does_not_get_sber():
    subs = [{"instrument": "GAZP", "signal_type": "*", "min_importance": 0}]
    assert rs.filter_for_subscriber(subs, [SBER_SIG]) == []


def test_subscription_by_signal_type_exact():
    subs = [{"instrument": "*", "signal_type": "rsi_overbought", "min_importance": 0}]
    assert rs.filter_for_subscriber(subs, [SBER_SIG, GAZP_SIG]) == [SBER_SIG]


# ---------- рассылка ----------

def test_no_subscribers_signal_goes_to_admin(monkeypatch):
    settings = SimpleNamespace(admin_chat_id="111")
    monkeypatch.setattr(rs, "list_subscribers", lambda: [])
    sent = MagicMock()
    monkeypatch.setattr(rs, "send_telegram", sent)

    plan = rs.broadcast(settings, [SBER_SIG], dry_run=False)

    assert plan == [("111", [SBER_SIG])]
    sent.assert_called_once()
    kwargs = sent.call_args.kwargs
    assert kwargs["category"] == "market"
    assert kwargs["parse_mode"] == "HTML"
    assert "(нет подписчиков)" in kwargs["text"]


def test_each_subscriber_gets_own_filtered_message(monkeypatch):
    settings = SimpleNamespace(admin_chat_id="111")
    monkeypatch.setattr(rs, "list_subscribers", lambda: ["222", "333"])
    monkeypatch.setattr(
        rs, "list_subscriptions",
        lambda chat_id: [{"instrument": "SBER", "signal_type": "*", "min_importance": 0}]
        if chat_id == "222" else [{"instrument": "*", "signal_type": "*", "min_importance": 0}],
    )
    sent = MagicMock()
    monkeypatch.setattr(rs, "send_telegram", sent)

    plan = rs.broadcast(settings, [SBER_SIG, GAZP_SIG], dry_run=False)

    assert plan == [("222", [SBER_SIG]), ("333", [SBER_SIG, GAZP_SIG])]
    assert sent.call_count == 2
    first_kwargs = sent.call_args_list[0].kwargs
    assert first_kwargs["chat_id"] == "222"
    assert "GAZP" not in first_kwargs["text"]
    assert "<b>SBER</b>" in first_kwargs["text"]


def test_dry_run_calls_no_telegram_no_annotations(monkeypatch):
    settings = SimpleNamespace(admin_chat_id="111")
    monkeypatch.setattr(rs, "list_subscribers", lambda: [])
    sent = MagicMock()
    annotate = MagicMock()
    monkeypatch.setattr(rs, "send_telegram", sent)
    monkeypatch.setattr(rs, "post_annotation", annotate)

    client = MagicMock()
    signals = [SBER_SIG]
    rs.broadcast(settings, signals, dry_run=True)

    sent.assert_not_called()
    annotate.assert_not_called()


# ---------- аннотации ----------

def test_annotation_payload_bar_date_ms_and_tags():
    sig = make_signal("SBER", "rsi_overbought", "2026-10-28")
    payload = rs.annotation_payload(sig)

    expected_ms = int(
        datetime(2026, 10, 28, tzinfo=timezone.utc).timestamp() * 1000
    )
    assert payload["time"] == expected_ms
    assert payload["tags"] == ["mdg", "signal", "rsi_overbought", "SBER"]
    assert payload["text"] == sig["message"]


def test_annotation_payload_accepts_date_object():
    import datetime as dt_mod

    sig = make_signal("SBER", bar_date=dt_mod.date(2026, 10, 28))
    payload = rs.annotation_payload(sig)
    assert payload["time"] == int(
        datetime(2026, 10, 28, tzinfo=timezone.utc).timestamp() * 1000
    )


def test_annotation_failure_does_not_crash_main(monkeypatch, capsys):
    """Ошибка POST аннотации — warn, рантайм продолжается (mark_sent/exit 0)."""
    settings = SimpleNamespace(admin_chat_id="111")

    class MockClient:
        def __init__(self):
            self.commands = []

        def command(self, sql):
            self.commands.append(sql)

        def query(self, sql):
            return SimpleNamespace(named_results=lambda: iter([]))

    monkeypatch.setattr(rs, "Settings", SimpleNamespace(load=lambda: settings))
    monkeypatch.setattr(rs, "get_client", lambda s: MockClient())
    monkeypatch.setattr(rs, "detect_signals", lambda client, instruments: [SBER_SIG])
    monkeypatch.setattr(rs, "list_subscribers", lambda: [])
    monkeypatch.setattr(rs, "send_telegram", MagicMock())
    monkeypatch.setattr(rs, "post_annotation", MagicMock(side_effect=RuntimeError("grafana down")))

    rc = rs.main([])

    assert rc == 0


def test_main_dry_run_skips_send_and_annotations(monkeypatch):
    settings = SimpleNamespace(admin_chat_id="111")

    class MockClient:
        def command(self, sql):
            pass

        def query(self, sql):
            return SimpleNamespace(named_results=lambda: iter([]))

    monkeypatch.setattr(rs, "Settings", SimpleNamespace(load=lambda: settings))
    monkeypatch.setattr(rs, "get_client", lambda s: MockClient())
    monkeypatch.setattr(rs, "detect_signals", lambda client, instruments: [SBER_SIG])
    monkeypatch.setattr(rs, "list_subscribers", lambda: ["222"])
    monkeypatch.setattr(
        rs, "list_subscriptions",
        lambda chat_id: [{"instrument": "*", "signal_type": "*", "min_importance": 0}],
    )
    sent = MagicMock()
    annotate = MagicMock()
    mark_sent = MagicMock()
    monkeypatch.setattr(rs, "send_telegram", sent)
    monkeypatch.setattr(rs, "post_annotation", annotate)
    monkeypatch.setattr(rs, "mark_sent", mark_sent)

    rc = rs.main(["--dry-run"])

    assert rc == 0
    sent.assert_not_called()
    annotate.assert_not_called()
    mark_sent.assert_not_called()


def test_main_no_new_signals_is_silent_success(monkeypatch):
    """Идемпотентность: повторный запуск — 'no new signals', exit 0."""
    settings = SimpleNamespace(admin_chat_id="111")

    class MockClient:
        def command(self, sql):
            pass

        def query(self, sql):
            # persist_new видит существующий сигнал → дедуп
            def rows():
                yield {"instrument_uid": "uid-SBER", "signal_type": "rsi_overbought",
                       "bar_date": datetime(2026, 10, 28)}
            return SimpleNamespace(named_results=rows)

    monkeypatch.setattr(rs, "Settings", SimpleNamespace(load=lambda: settings))
    monkeypatch.setattr(rs, "get_client", lambda s: MockClient())
    monkeypatch.setattr(rs, "detect_signals", lambda client, instruments: [SBER_SIG])
    monkeypatch.setattr(rs, "list_subscribers", lambda: ["222"])
    monkeypatch.setattr(
        rs, "list_subscriptions",
        lambda chat_id: [{"instrument": "*", "signal_type": "*", "min_importance": 0}],
    )
    sent = MagicMock()
    monkeypatch.setattr(rs, "send_telegram", sent)

    rc = rs.main([])

    assert rc == 0
    sent.assert_not_called()


def test_main_force_broadcasts_despite_dedup(monkeypatch):
    settings = SimpleNamespace(admin_chat_id="111")

    class MockClient:
        def command(self, sql):
            pass

        def query(self, sql):
            def rows():
                yield {"instrument_uid": "uid-SBER", "signal_type": "rsi_overbought",
                       "bar_date": datetime(2026, 10, 28)}
            return SimpleNamespace(named_results=rows)

    monkeypatch.setattr(rs, "Settings", SimpleNamespace(load=lambda: settings))
    monkeypatch.setattr(rs, "get_client", lambda s: MockClient())
    monkeypatch.setattr(rs, "detect_signals", lambda client, instruments: [SBER_SIG])
    monkeypatch.setattr(rs, "list_subscribers", lambda: ["222"])
    monkeypatch.setattr(
        rs, "list_subscriptions",
        lambda chat_id: [{"instrument": "*", "signal_type": "*", "min_importance": 0}],
    )
    sent = MagicMock()
    monkeypatch.setattr(rs, "send_telegram", sent)
    monkeypatch.setattr(rs, "post_annotation", MagicMock())

    rc = rs.main(["--force"])

    assert rc == 0
    sent.assert_called_once()


def test_format_message_contains_ticker_in_bold():
    text = rs.format_message([SBER_SIG])
    assert "<b>SBER</b>" in text
    assert "rsi_overbought" in text


@pytest.mark.parametrize(
    "sub,signal,expected",
    [
        ({"instrument": "sber", "signal_type": "*"}, make_signal("SBER"), True),
        ({"instrument": "*", "signal_type": "volume_spike"}, make_signal("SBER", "volume_spike"), True),
        ({"instrument": "*", "signal_type": "volume_spike"}, make_signal("SBER"), False),
    ],
)
def test_subscription_matches_matrix(sub, signal, expected):
    assert rs.subscription_matches(sub, signal) is expected
