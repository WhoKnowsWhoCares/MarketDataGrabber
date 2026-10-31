"""Тесты expected_trading_day (watchdog) — граничные случаи недели.

MSK = UTC+3. Ожидаемый торговый день:
- до 00:45 МСК ночной сбор ещё не прошёл → день назад;
- воскресенье не торговое → день назад.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import scripts.watchdog as wd


def _set_now(dt_utc: datetime, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wd, "_utcnow", lambda: dt_utc)


@pytest.mark.parametrize(
    "now_utc, expected",
    [
        # Сб 10:03 UTC (13:03 МСК): вчера=пт — торги пятницы собраны ночным сбором сб.
        (datetime(2026, 10, 31, 10, 3, tzinfo=timezone.utc), "2026-10-30"),
        # Вс 10:00 UTC: вчера=сб (субботние сессии собираются вс 00:15 МСК).
        (datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc), "2026-10-31"),
        # Пн 10:00 UTC: вчера=вс (не торговый) → суббота.
        (datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc), "2026-10-31"),
        # Пн 21:30 UTC = 00:30 МСК вт: ночной сбор за пн ещё не прошёл → суббота.
        (datetime(2026, 11, 2, 21, 30, tzinfo=timezone.utc), "2026-10-31"),
        # Пн 22:30 UTC = 01:30 МСК вт: сбор 00:35 МСК прошёл → понедельник.
        (datetime(2026, 11, 2, 22, 30, tzinfo=timezone.utc), "2026-11-02"),
        # Ср 12:00 UTC: обычный будний день → вт.
        (datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc), "2026-11-03"),
    ],
)
def test_expected_trading_day(
    now_utc: datetime, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_now(now_utc, monkeypatch)
    assert wd.expected_trading_day().isoformat() == expected
