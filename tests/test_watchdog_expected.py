"""Тесты expected_trading_day (watchdog) — граничные случаи недели.

MSK = UTC+3. Ожидаемый торговый день:
- до 00:45 МСК ночной сбор ещё не прошёл → день назад;
- воскресенье не торговое → день назад;
- праздник в календаре (payload.is_open=false) → откат до торгового дня;
- строки в календаре нет / CH недоступен → чистая эвристика.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

import scripts.watchdog as wd


def _set_now(dt_utc: datetime, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wd, "_utcnow", lambda: dt_utc)


class FakeCH:
    """Mock CH-клиента: query по календарю отдаёт строку is_open или пусто."""

    def __init__(
        self,
        is_open_by_date: dict[date, bool] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.is_open_by_date = is_open_by_date or {}
        self.error = error
        self.queries: list[tuple[str, dict]] = []

    def query(self, sql: str, parameters: dict | None = None):
        self.queries.append((sql, parameters or {}))
        if self.error is not None:
            raise self.error
        d = (parameters or {}).get("d")
        if isinstance(d, date) and d in self.is_open_by_date:
            return SimpleNamespace(result_rows=[(self.is_open_by_date[d],)])
        return SimpleNamespace(result_rows=[])


@pytest.fixture()
def no_calendar_rows() -> FakeCH:
    """Календарь пуст («строки нет») — не блокирует, чистая эвристика."""
    return FakeCH()


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
    now_utc: datetime,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
    no_calendar_rows: FakeCH,
) -> None:
    _set_now(now_utc, monkeypatch)
    got = wd.expected_trading_day(no_calendar_rows)
    assert got.isoformat() == expected
    # каждая проверка дня уходила в CH одним запросом по expected_date
    assert no_calendar_rows.queries, "календарь должен опрашиваться"
    assert all("trading_day" in sql for sql, _ in no_calendar_rows.queries)


def test_holiday_shifts_expected_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Чт 5.11.2026 утро: 4.11 (среда) праздник → expected=3.11."""
    _set_now(datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc), monkeypatch)
    ch = FakeCH({date(2026, 11, 4): False})
    got = wd.expected_trading_day(ch)
    assert got.isoformat() == "2026-11-03"
    # строка по 3.11 отсутствует в календаре → откат останавливается
    assert any(p.get("d") == date(2026, 11, 3) for _, p in ch.queries)


def test_holiday_chain_shifts_to_trading_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """Серия праздников: 4-5.11 закрыто → откат до 3.11 (торговый в календаре)."""
    _set_now(datetime(2026, 11, 6, 6, 0, tzinfo=timezone.utc), monkeypatch)
    ch = FakeCH({date(2026, 11, 5): False, date(2026, 11, 4): False})
    got = wd.expected_trading_day(ch)
    assert got.isoformat() == "2026-11-03"


def test_calendar_confirms_trading_day_no_extra_shift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Строка есть и is_open=true → эвристический день остаётся."""
    _set_now(datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc), monkeypatch)
    ch = FakeCH({date(2026, 11, 4): True})
    assert wd.expected_trading_day(ch).isoformat() == "2026-11-04"


def test_ch_error_falls_back_to_heuristic(monkeypatch: pytest.MonkeyPatch) -> None:
    """CH недоступен при проверке календаря → чистая эвристика, без падения."""
    _set_now(datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc), monkeypatch)
    ch = FakeCH(error=ConnectionError("clickhouse down"))
    assert wd.expected_trading_day(ch).isoformat() == "2026-11-04"


def test_none_client_pure_heuristic(monkeypatch: pytest.MonkeyPatch) -> None:
    """ch=None (обратная совместимость) → чистая эвристика."""
    _set_now(datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc), monkeypatch)
    assert wd.expected_trading_day().isoformat() == "2026-11-04"
