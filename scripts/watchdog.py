#!/usr/bin/env python3
"""Watchdog свежести данных MarketDataGrabber.

Запускается cron'ом ежечасно. Проверки:
- fresh_1m:  в candles_1m есть минутки за ожидаемый торговый день
             (открытие = 06:00 UTC; раньше сравнивали с «26 часов назад»,
             что ложно алертило в выходные);
- fresh_1d:  max(bar_date) candles_1d >= ожидаемый торговый день;
- dq_warn:   последняя dq_metrics gap_vs_calendar > 10;
- vitrines:  mart_daily_metrics max(bar_date) < ожидаемый торговый день.

Ожидаемый торговый день (expected): последний завершённый день, за который
ночной сбор (00:15 МСК, пн-сб) уже должен был отработать. Будни + суббота
(MOEX проводит субботние сессии); воскресенье сдвигает к субботе; до 00:45
МСК вчерашний ночной сбор ещё не прошёл — берём позавчерашний торговый день.
Праздники MOEX не учитываются (известное ограничение, до P7).

Все проверки ОК  -> печатает "WATCHDOG: OK", exit 0.
Любой провал     -> send_telegram(category='ops') с деталями, exit 1.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mdgrabber.ch import get_client
from mdgrabber.config import Settings
from mdgrabber.notify import send_telegram

# Свежесть 1m: минуты за ожидаемый торговый день (см. expected_trading_day).
FRESH_1M_OPEN_UTC = dtime(6, 0)  # 10:00 МСК — открытие основной сессии
# Порог DQ по расхождениям с торговым календарём.
DQ_GAP_THRESHOLD = 10.0
# Файл состояния cooldown для fresh_1m (1 алерт/день).
COOLDOWN_FILE = Path("/tmp/mdg_watchdog_fresh_1m_last_alert")
# Время МСК, после которого вчерашний ночной сбор (00:15/00:35 МСК) считается
# отработавшим (с запасом на длительность).
NIGHT_RUN_DONE_MSK = dtime(0, 45)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def expected_trading_day() -> date:
    """Последний торговый день, за который ночной сбор уже должен был пройти.

    MSK ≡ UTC+3 (лето/зима не различаются в России). Логика:
    - стартуем с «вчера» по МСК;
    - до 00:45 МСК вчерашний ночной сбор ещё не отработал — сдвиг на день назад;
    - воскресенье (нет сессий) — сдвиг на день назад (к субботе).
    """
    msk = _utcnow() + timedelta(hours=3)
    d = msk.date() - timedelta(days=1)
    if msk.time() < NIGHT_RUN_DONE_MSK:
        d -= timedelta(days=1)
    if d.weekday() == 6:  # воскресенье
        d -= timedelta(days=1)
    return d


def check_fresh_1m(ch) -> tuple[bool, str]:
    """True = OK. В candles_1m есть минутки за ожидаемый торговый день."""
    expected = expected_trading_day()
    threshold = datetime.combine(expected, FRESH_1M_OPEN_UTC, tzinfo=timezone.utc)
    row = ch.query("SELECT max(bar_time) FROM market_data.candles_1m FINAL").result_rows
    max_bar = row[0][0] if row and row[0][0] else None
    if max_bar is None:
        return False, "fresh_1m: candles_1m пуста"
    if max_bar.tzinfo is None:
        max_bar = max_bar.replace(tzinfo=timezone.utc)
    ok = max_bar >= threshold
    return ok, (
        f"fresh_1m: {'OK' if ok else 'FAIL'} — max(bar_time)={max_bar}, "
        f"ожидался день {expected} (порог {threshold:%Y-%m-%d %H:%M} UTC)"
    )


def check_fresh_1d(ch) -> tuple[bool, str]:
    """max(bar_date) candles_1d >= ожидаемый торговый день."""
    expected = expected_trading_day()
    row = ch.query("SELECT max(bar_date) FROM market_data.candles_1d FINAL").result_rows
    max_date = row[0][0] if row and row[0][0] else None
    if max_date is None:
        return False, "fresh_1d: candles_1d пуста"
    ok = max_date >= expected
    return ok, (
        f"fresh_1d: {'OK' if ok else 'FAIL'} — max(bar_date)={max_date}, "
        f"ожидалось >= {expected}"
    )


def check_dq_warn(ch) -> tuple[bool, str]:
    """Последняя запись gap_vs_calendar в dq_metrics <= 10."""
    row = ch.query(
        "SELECT value, run_ts FROM market_data_aux.dq_metrics "
        "WHERE metric = 'gap_vs_calendar' ORDER BY run_ts DESC LIMIT 1"
    ).result_rows
    if not row:
        # DQ ещё не запускался — не считаем провалом, но сообщаем.
        return True, "dq_warn: нет записей gap_vs_calendar (DQ не запускался)"
    value, run_ts = row[0]
    ok = float(value) <= DQ_GAP_THRESHOLD
    return ok, (
        f"dq_warn: {'OK' if ok else 'FAIL'} — gap_vs_calendar={value} "
        f"от {run_ts} (порог {DQ_GAP_THRESHOLD})"
    )


def check_vitrines(ch) -> tuple[bool, str]:
    """max(bar_date) mart_daily_metrics >= ожидаемый торговый день."""
    expected = expected_trading_day()
    row = ch.query(
        "SELECT max(bar_date) FROM market_data_gold.mart_daily_metrics"
    ).result_rows
    max_date = row[0][0] if row and row[0][0] else None
    if max_date is None:
        return False, "vitrines: market_data_gold.mart_daily_metrics пуста"
    ok = max_date >= expected
    return ok, (
        f"vitrines: {'OK' if ok else 'FAIL'} — max(bar_date)={max_date}, "
        f"ожидалось >= {expected}"
    )


def _fresh_1m_alert_allowed() -> bool:
    """Cooldown: fresh_1m алертится не чаще 1 раза в день."""
    today = _utcnow().date()
    try:
        if COOLDOWN_FILE.exists():
            last = datetime.strptime(
                COOLDOWN_FILE.read_text().strip(), "%Y-%m-%d"
            ).date()
            if last == today:
                return False
        COOLDOWN_FILE.write_text(today.isoformat())
        return True
    except OSError as e:
        print(f"WARN: cooldown file недоступен ({e}), алертим без cooldown")
        return True


def main() -> int:
    settings = Settings.load()
    ch = get_client(settings)

    checks = [
        ("fresh_1m", check_fresh_1m(ch)),
        ("fresh_1d", check_fresh_1d(ch)),
        ("dq_warn", check_dq_warn(ch)),
        ("vitrines", check_vitrines(ch)),
    ]

    failures = [detail for name, (ok, detail) in checks if not ok]
    for _, (_, detail) in checks:
        print(detail)

    if not failures:
        print("WATCHDOG: OK")
        return 0

    text = "WATCHDOG: FAIL\n" + "\n".join(failures)
    # fresh_1m вне торгов — не чаще 1 алерта/день (cooldown), статус печатается выше.
    fresh_1m_failed = any(d.startswith("fresh_1m:") for d in failures)
    others_failed = len(failures) > (1 if fresh_1m_failed else 0)
    if not fresh_1m_failed or others_failed or _fresh_1m_alert_allowed():
        send_telegram(settings, text=text, category="ops")
        print("WATCHDOG: алерт отправлен в Telegram (ops)")
    else:
        print("WATCHDOG: FAIL (fresh_1m, cooldown — алерт уже отправлялся сегодня)")
    print(text)
    return 1


if __name__ == "__main__":
    sys.exit(main())
