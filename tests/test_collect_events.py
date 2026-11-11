"""Тесты календаря collect_events.py: приоритет источников trading_day.

Источники: tinvest (TradingSchedules) > candles (прошлое) > isdayoff
(будущее) > weekday (fallback).
"""
import json
import sys
import unittest.mock as mock
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import collect_events as ce  # noqa: E402


def _payload(r) -> dict:
    """row() = [type, uid, event_time, expected, actual, imp, source, payload_str]."""
    return json.loads(r[-1])


class FakeCh:
    """clickhouse-клиент-заглушка: возвращает set открытых дней прошлого."""

    def __init__(self, open_past: set[date]):
        self.open_past = open_past

    def query(self, sql):
        class R:
            result_rows = [(d,) for d in self.open_past]

        return R()


def _mk_api(days_open: set[date], horizon_end: date):
    """Заглушка get_trading_schedules: отдаёт дни в пределах горизонта API."""
    def fake(from_iso=None, to_iso=None):
        days = []
        for d in sorted(days_open):
            if d > horizon_end:
                continue
            days.append({"date": d.isoformat() + "T00:00:00Z", "isTradingDay": True})
        return [{"exchange": "MOEX", "days": days}]

    return fake


# Производственный календарь-заглушка: все будни рабочие, все выходные нет,
# КРОМЕ переданных праздников.
def _mock_prod(holidays: set[date]):
    def fake_fetch(start, end, settings=None):
        out = {}
        cur = date(start.year, 1, 1)
        while cur <= date(end.year, 12, 31):
            out[cur] = cur.weekday() < 5 and cur not in holidays
            cur += timedelta(days=1)
        return out

    return fake_fetch


def test_future_holidays_closed_by_prod_calendar():
    """Праздники за горизонтом API закрываются производственным календарём
    (раньше weekday-эвристика помечала будни-праздники открытыми)."""
    today = date(2026, 11, 11)
    api_open = {today + timedelta(days=d) for d in range(1, 11)}  # horizon +10
    new_year = {date(2027, 1, d) for d in range(1, 9)}  # все в горизонте +60

    with mock.patch.object(ce, "fetch_production_calendar", _mock_prod(new_year)):
        rows = ce.collect_calendar(
            type("TI", (), {"get_trading_schedules": _mk_api(api_open, today + timedelta(days=13))})(),
            FakeCh(set()),
            today,
        )

    by_date = {r[3]: r for r in rows}  # r[3] = expected_date
    jan2 = _payload(by_date[date(2027, 1, 2)])  # суббота
    assert jan2["source"] == "isdayoff"
    assert jan2["is_open"] is False
    jan4 = _payload(by_date[date(2027, 1, 4)])  # будня-праздник — ключевой кейс
    assert jan4["is_open"] is False
    jan11 = _payload(by_date[date(2026, 12, 24)])  # будня, не праздник
    assert jan11["is_open"] is True
    assert jan11["source"] == "isdayoff"


def test_weekday_fallback_when_prod_unavailable():
    """isdayoff недоступен (None) → fallback будни/выходные."""
    today = date(2026, 11, 11)
    with mock.patch.object(ce, "fetch_production_calendar", lambda *a, **k: None):
        rows = ce.collect_calendar(
            type("TI", (), {"get_trading_schedules": _mk_api(set(), today)})(),
            FakeCh(set()),
            today,
        )
    by_date = {r[3]: r for r in rows}
    dec7 = _payload(by_date[date(2026, 12, 7)])  # понедельник
    assert dec7["source"] == "weekday"
    assert dec7["is_open"] is True
    dec5 = _payload(by_date[date(2026, 12, 5)])  # суббота
    assert dec5["is_open"] is False


def test_tinvest_days_take_priority():
    """Дни из TradingSchedules (включая субботнюю сессию) — source=tinvest,
    не перекрываются candles/isdayoff."""
    today = date(2026, 11, 11)
    sat = date(2026, 11, 14)  # суббота в горизонте API, сессия открыта
    with mock.patch.object(ce, "fetch_production_calendar", _mock_prod(set())):
        rows = ce.collect_calendar(
            type("TI", (), {"get_trading_schedules": _mk_api({sat}, today + timedelta(days=13))})(),
            FakeCh(set()),
            today,
        )
    by_date = {r[3]: r for r in rows}
    sat_row = _payload(by_date[sat])
    assert sat_row["source"] == "tinvest"
    assert sat_row["is_open"] is True
    assert sat_row["derived"] is False


def test_past_from_candles_beats_prod_calendar():
    """Прошлые дни берутся из фактических свечей, не из производственного
    календаря (субботние сессии в прошлом — открыты)."""
    today = date(2026, 11, 11)
    last_sat = date(2026, 11, 7)  # суббота с торгами
    with mock.patch.object(ce, "fetch_production_calendar", _mock_prod(set())):
        rows = ce.collect_calendar(
            type("TI", (), {"get_trading_schedules": _mk_api(set(), today)})(),
            FakeCh({last_sat}),
            today,
        )
    by_date = {r[3]: r for r in rows}
    sat_row = _payload(by_date[last_sat])
    assert sat_row["source"] == "candles"
    assert sat_row["is_open"] is True


def test_fetch_production_calendar_parses_codes():
    """Парсинг кодов isdayoff: 0/2/4 — открыт, 1/3 — закрыт."""
    resp = mock.MagicMock()
    resp.text = "0011100210"  # 10 дней фиктивного года? — нет, проверим через patch длины
    # 365 символов: январские праздники 1-8 закрыты, 9-й открыт
    codes = ["1"] * 8 + ["0"] + ["0"] * 356
    resp.text = "".join(codes)
    resp.raise_for_status = lambda: None
    http_mock = mock.MagicMock()
    http_mock.get.return_value = resp
    http_mock.__enter__ = lambda s: s
    http_mock.__exit__ = lambda *a: None

    with mock.patch("httpx.Client", return_value=http_mock):
        out = ce.fetch_production_calendar(date(2027, 1, 1), date(2027, 1, 10))
    assert out is not None
    assert out[date(2027, 1, 4)] is False   # будня-праздник закрыт
    assert out[date(2027, 1, 9)] is True    # суббота-рабочий? нет: код 0 → открыт
    assert out[date(2027, 1, 1)] is False


def test_fetch_production_calendar_unavailable_returns_none():
    with mock.patch("httpx.Client", side_effect=Exception("boom")):
        assert ce.fetch_production_calendar(date(2027, 1, 1), date(2027, 2, 1)) is None


# --- macro -------------------------------------------------------------------

CBR_FIXTURE = """<html><body>
<div>2027 год 2026 год</div>
<div>12 февраля 2027 года</div>
<div>Заседание Совета директоров Банка России по ключевой ставке</div>
<div>Пресс-релиз по ключевой ставке</div>
<div>19 марта 2027 года</div>
<div>Заседание Совета директоров Банка России</div>
<div>26 марта 2027 года</div>
<div>Заседание Совета директоров Банка России по&nbsp;ключевой ставке</div>
<div>10 февраля 2024 года</div>
<div>Заседание Совета директоров Банка России по ключевой ставке</div>
</body></html>"""


def test_parse_cbr_schedule_filters_by_title_and_year():
    """Только заседания «по ключевой ставке»; обычные СД и чужие годы — мимо."""
    out = ce.parse_cbr_schedule(CBR_FIXTURE, 2026, 2027)
    assert out == [date(2027, 2, 12), date(2027, 3, 26)]


def test_parse_fomc_dates_shaded_and_tentative():
    """Чередование fomc-meeting/--shaded и '*' после tentative-дат."""
    html = """
    <a id="42">2027 FOMC Meetings</a>
    <div class="row fomc-meeting" ">
      <div class="fomc-meeting__month ..."><strong>January</strong></div>
      <div class="fomc-meeting__date ...">26-27</div>
    </div>
    <div class="fomc-meeting--shaded row fomc-meeting" ">
      <div class="fomc-meeting--shaded fomc-meeting__month ..."><strong>March</strong></div>
      <div class="fomc-meeting__date ...">16-17*</div>
    </div>
    <div class="panel panel-default"><a id="43">2028 FOMC Meetings</a></div>
    """
    out = ce.parse_fomc_dates(html, {2027})
    assert out == [date(2027, 1, 27), date(2027, 3, 17)]  # второй день = решение


def test_collect_macro_merges_sources_and_survives_failure(tmp_path):
    """Источники независимы: упавший FOMC не роняет ЦБ; manual.json читается."""
    today = date(2026, 11, 11)
    manual = tmp_path / "macro_manual.json"
    manual.write_text(
        '[{"name": "ИПЦ Росстата", "date": "2026-12-11", "region": "ru", "source": "rosstat"}]',
        encoding="utf-8",
    )
    with mock.patch.object(ce, "_http_get", side_effect=[CBR_FIXTURE, Exception("cf blocked")]), \
         mock.patch.object(ce, "MACRO_MANUAL", manual):
        rows = ce.collect_macro(settings=None, today=today)
    kinds = [(r[1], r[3]) for r in rows]
    assert ("macro:ru:cbr", date(2027, 2, 12)) in kinds
    assert ("macro:ru:rosstat", date(2026, 12, 11)) in kinds
    assert not any(uid == "macro:us:fomc" for uid, _ in kinds)  # FOMC упал — WARN, не crash
    # время объявления в payload у ЦБ
    cbr_payload = json.loads([r for r in rows if r[1] == "macro:ru:cbr"][0][-1])
    assert cbr_payload["announce_time_msk"] == "13:30"
    assert cbr_payload["press_conf_msk"] == "15:00"


def test_fomc_announce_time_msk_dst_boundary():
    """14:00 ET → 22:00 МСК зимой (EST), 21:00 летом (EDT); границы DST."""
    assert ce.fomc_announce_msk(date(2026, 12, 9)) == "22:00"    # EST
    assert ce.fomc_announce_msk(date(2027, 1, 27)) == "22:00"    # EST
    assert ce.fomc_announce_msk(date(2027, 3, 17)) == "21:00"    # EDT (DST с 14.03.2027)
    assert ce.fomc_announce_msk(date(2027, 6, 9)) == "21:00"     # EDT
    assert ce.fomc_announce_msk(date(2027, 11, 3)) == "21:00"    # EDT (DST до 07.11.2027)
    assert ce.fomc_announce_msk(date(2027, 11, 10)) == "22:00"   # EST после перехода
    # первый день DST — заседание после перехода 2:00 local
    assert ce.fomc_announce_msk(date(2027, 3, 14)) == "21:00"
