#!/usr/bin/env python3
"""DQ-проверки для bronze/silver/gold слоёв. Пишет метрики в market_data_aux.dq_metrics.

Проверки:
- ohlc_violations_1m: нарушения OHLC в минуточках за 30 дней
- null_share_1d: доля NULL/NaN в ценах дневок
- gap_vs_calendar: расхождения с календарём торгов за 30 дней
- stale_1m_hours: часов с момента max(bar_time) 1m до now

Критичность: DQ: WARN если gap_vs_calendar > 0 или stale_1m_hours > 24, иначе DQ: OK.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mdgrabber.ch import get_client
from mdgrabber.config import Settings


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except Exception as e:  # noqa: BLE001
        print(f"WARN: git sha недоступен: {e}")
        return ""


def write_dq_metric(
    ch,
    layer: str,
    metric: str,
    value: float,
    details: str = "",
) -> None:
    """Записывает одну DQ-метрику в market_data_aux.dq_metrics."""
    ch.insert(
        table="dq_metrics",
        database="market_data_aux",
        column_names=["run_ts", "layer", "metric", "value", "details"],
        data=[[datetime.now(timezone.utc), layer, metric, value, details]],
    )
    print(f"DQ metric: layer={layer} metric={metric} value={value} details={details}")


def check_ohlc_violations_1m(ch) -> float:
    """Количество строк в candles_1m за 30 дней с нарушениями OHLC."""
    # high < least(open, close) OR low > greatest(open, close) OR high < low OR volume < 0
    sql = """
    SELECT count()
    FROM market_data.candles_1m FINAL
    WHERE bar_time >= (SELECT max(bar_time) FROM market_data.candles_1m) - INTERVAL 30 DAY
      AND (
           high < LEAST(open, close)
        OR low > GREATEST(open, close)
        OR high < low
        OR volume < 0
      )
    """
    result = ch.query(sql).result_rows
    count = result[0][0] if result else 0
    write_dq_metric(ch, "bronze", "ohlc_violations_1m", float(count))
    return float(count)


def check_null_share_1d(ch) -> float:
    """Доля NULL/NaN в ценах (open, high, low, close) дневок за все время."""
    sql = """
    SELECT
        sum(CASE WHEN open IS NULL OR open != open THEN 1 ELSE 0 END) +
        sum(CASE WHEN high IS NULL OR high != high THEN 1 ELSE 0 END) +
        sum(CASE WHEN low IS NULL OR low != low THEN 1 ELSE 0 END) +
        sum(CASE WHEN close IS NULL OR close != close THEN 1 ELSE 0 END) AS null_count,
        count() * 4 AS total_cells
    FROM market_data.candles_1d FINAL
    """
    result = ch.query(sql).result_rows
    null_count = 0
    total_cells = 0
    if result and result[0][1] > 0:
        null_count, total_cells = result[0]
        share = null_count / total_cells
    else:
        share = 0.0
    write_dq_metric(ch, "bronze", "null_share_1d", share, f"nulls={null_count}, cells={total_cells}")
    return share


def check_gap_vs_calendar(ch) -> float:
    """Расхождения с календарём:
    - есть дневка, но calendar.is_open = false
    - торговый день (is_open = true после листинга), но нет дневки
    Проверка ограничена диапазоном календаря (min/max event_time trading_day)
    и только до максимальной даты в candles_1d (прошлое/настоящее).
    """
    # Получаем max bar_date из дневок
    sql_max_daily = "SELECT max(bar_date) FROM market_data.candles_1d FINAL"
    max_daily_row = ch.query(sql_max_daily).result_rows
    if not max_daily_row or max_daily_row[0][0] is None:
        print("WARN: нет данных в candles_1d, пропускаем gap_vs_calendar")
        write_dq_metric(ch, "gold", "gap_vs_calendar", 0.0, "no daily data")
        return 0.0
    max_daily_date = max_daily_row[0][0]

    # Получаем границы календаря trading_day (только до max_daily_date)
    sql_cal_bounds = f"""
    SELECT
        min(toDateOrNull(JSONExtractString(payload, 'date'))) AS min_cal_date,
        max(toDateOrNull(JSONExtractString(payload, 'date'))) AS max_cal_date
    FROM market_data.events FINAL
    WHERE event_type = 'trading_day'
      AND JSONHas(payload, 'date') = 1
      AND toDateOrNull(JSONExtractString(payload, 'date')) IS NOT NULL
      AND toDateOrNull(JSONExtractString(payload, 'date')) <= '{max_daily_date}'
    """
    cal_bounds = ch.query(sql_cal_bounds).result_rows
    if not cal_bounds or cal_bounds[0][0] is None:
        print("WARN: календарь trading_day пуст (до max daily), пропускаем gap_vs_calendar")
        write_dq_metric(ch, "gold", "gap_vs_calendar", 0.0, "calendar empty up to max daily")
        return 0.0

    min_cal_date, max_cal_date = cal_bounds[0]

    # Получаем торговые дни из календаря (events trading_day) в диапазоне календаря
    # is_open хранится в payload JSON
    sql_cal = f"""
    SELECT
        toDateOrNull(JSONExtractString(payload, 'date')) AS cal_date,
        JSONExtractBool(payload, 'is_open') AS is_open
    FROM market_data.events FINAL
    WHERE event_type = 'trading_day'
      AND JSONHas(payload, 'date') = 1
      AND toDateOrNull(JSONExtractString(payload, 'date')) IS NOT NULL
      AND toDateOrNull(JSONExtractString(payload, 'date')) BETWEEN '{min_cal_date}' AND '{max_cal_date}'
      AND JSONExtractBool(payload, 'is_open') = 1
    """
    cal_rows = ch.query(sql_cal).result_rows
    cal_dates = {row[0] for row in cal_rows}

    # Получаем дневки ТОЛЬКО в диапазоне календаря
    sql_daily = f"""
    SELECT DISTINCT instrument_uid, bar_date
    FROM market_data.candles_1d FINAL
    WHERE bar_date BETWEEN '{min_cal_date}' AND '{max_cal_date}'
    """
    daily_rows = ch.query(sql_daily).result_rows

    # Инструменты
    instruments = set(row[0] for row in daily_rows)
    daily_dates_by_inst = {}
    for inst, d in daily_rows:
        daily_dates_by_inst.setdefault(inst, set()).add(d)

    violations = 0
    details_parts = []

    for inst in instruments:
        inst_dates = daily_dates_by_inst.get(inst, set())
        # 1. Дневка есть, но календарь говорит is_open=false (проверяем через events)
        for d in inst_dates:
            if d not in cal_dates:
                violations += 1
                details_parts.append(f"{inst}:{d} daily but not trading_day")

        # 2. Торговый день есть в календаре, но дневки нет
        for d in cal_dates:
            if d not in inst_dates:
                violations += 1
                details_parts.append(f"{inst}:{d} trading_day but no daily")

    details = "; ".join(details_parts[:20])  # ограничим длину
    write_dq_metric(ch, "gold", "gap_vs_calendar", float(violations), details)
    return float(violations)


def check_stale_1m_hours(ch) -> float:
    """Часов с момента max(bar_time) в candles_1m до now."""
    sql = """
    SELECT max(bar_time) FROM market_data.candles_1m FINAL
    """
    result = ch.query(sql).result_rows
    max_bar = None
    if result and result[0][0]:
        max_bar = result[0][0]
        # max_bar может быть naive datetime, делаем aware UTC
        if max_bar.tzinfo is None:
            max_bar = max_bar.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        hours = (now - max_bar).total_seconds() / 3600.0
    else:
        hours = 9999.0
    write_dq_metric(ch, "bronze", "stale_1m_hours", hours, f"max_bar_time={max_bar if result else 'None'}")
    return hours


def main() -> int:
    settings = Settings.load()
    ch = get_client(settings)

    print("=== DQ Checks ===")

    ohlc = check_ohlc_violations_1m(ch)
    null_share = check_null_share_1d(ch)
    gap_cal = check_gap_vs_calendar(ch)
    stale = check_stale_1m_hours(ch)

    # Критичность
    is_warn = (gap_cal > 0) or (stale > 24)
    status = "WARN" if is_warn else "OK"
    print(f"\nDQ: {status}")
    if is_warn:
        if gap_cal > 0:
            print(f"  - gap_vs_calendar = {gap_cal} (>0)")
        if stale > 24:
            print(f"  - stale_1m_hours = {stale:.1f} (>24)")

    return 0


if __name__ == "__main__":
    sys.exit(main())