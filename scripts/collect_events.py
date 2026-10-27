#!/usr/bin/env python3
"""Сборщик событий → market_data.events (календарь сессий, дивиденды,
экспирации, отчётность). Повторный запуск безопасен: ReplacingMergeTree
схлопывает дубли по (event_type, instrument_uid, event_time).

Ключевая конвенция: event_time = полночь UTC даты события — иначе все
события одного типа/инструмента имели бы одинаковый ключ сортировки
(event_time Nullable → epoch) и FINAL схлопнул бы их в одну строку.
expected_date = дата события; для прошедших отчётов заполняется actual.

Дивиденды: окно now−7 лет .. now+1 год (покрывает исторические отсечки,
например GAZP 2021-07-15 / SBER 2021-05-12); expected_date = recordDate
(дата отсечки = день гэпа на MOEX T+1), lastBuyDate — в payload.

Календарь: окно today±60 дней, открытые И закрытые дни (is_open).
Ограничение TradingSchedules проверено на живом API: from в прошлом →
400, период длиннее 14 дней → 400, дефолт отдаёт ровно today..today+14,
поэтому источники такие:
  - today..today+14 — TradingSchedules площадки MOEX (isTradingDay);
  - прошлое — фактические торговые дни из candles_1d (distinct bar_date),
    остальные дни окна считаются закрытыми;
  - будущее за пределами окна API — будни открыты, выходные закрыты
    (красные дни на буднях в ближайшие 60 дней попадают в окно API).
payload нормализован: {"date", "is_open", "exchange", "derived", "api"};
is_open — snake_case для проверочных LIKE-запросов.

Экспирации: даты с годом ≥ 2050 — placeholder API (CNYRUBF, IMOEXF отдают
2099-12-31) и не записываются.

Использование:
    set -a; source .env; set +a; .venv/bin/python scripts/collect_events.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mdgrabber.ch import get_client
from mdgrabber.config import Settings
from mdgrabber.tinvest import TInvestClient

INSTRUMENTS = ROOT / "config" / "instruments.json"
EXCHANGE = "MOEX"
CAL_BACK_DAYS = 60           # прошлое — из candles_1d (см. collect_calendar)
CAL_FWD_DAYS = 60
DIV_YEARS_BACK = 7           # окно ревью: сегодня − 7 лет
DIV_YEARS_FWD = 1
DIV_CHUNK_MONTHS = 12        # размер чанка для итеративного запроса дивидендов (1 год)
# Для полноты истории GAZP (≥5 дивидендов) запрашиваем до 2010 года
DIV_FULL_HISTORY_FROM = 2010
EXPIRY_MAX_YEAR = 2050       # 2099-12-31 у CNYRUBF/IMOEXF — placeholder API
SLEEP_SEC = 1.0
SOURCE = "tinkoff"
EVENT_COLUMNS = [
    "event_type", "instrument_uid", "event_time", "expected_date",
    "actual", "importance", "source", "payload",
]


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except Exception as e:  # noqa: BLE001
        print(f"WARN: git sha недоступен: {e}")
        return ""


def parse_dt(raw: str) -> datetime:
    """'2026-10-27T00:00:00Z' (возможны доли секунды) → datetime UTC."""
    raw = raw.strip().rstrip("Z")
    if "." in raw:
        raw = raw.split(".", 1)[0]
    return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def parse_date(raw: str) -> date:
    return parse_dt(raw).date()


def midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def row(event_type, uid, event_time, expected, actual, importance, payload) -> list:
    return [event_type, uid, event_time, expected, actual, importance, SOURCE,
            json.dumps(payload, ensure_ascii=False)]


def collect_calendar(ti: TInvestClient, ch, today: date) -> list[list]:
    """trading_day: один ряд на календарный день в окне today±60 — открытые
    И закрытые (payload.is_open true/false). См. docstring модуля про
    источники: API даёт только today..today+14, прошлое — из candles_1d,
    будущее за пределами API — будни/выходные."""
    api_days: dict[date, dict] = {}
    for e in ti.get_trading_schedules():
        if e.get("exchange") == EXCHANGE:
            for day in e.get("days", []):
                api_days[parse_date(day["date"])] = day
    if not api_days:
        print(f"WARN: площадка {EXCHANGE} не найдена в расписании")
    start = today - timedelta(days=CAL_BACK_DAYS)
    end = today + timedelta(days=CAL_FWD_DAYS)
    open_past = {
        r[0] for r in ch.query(
            "SELECT DISTINCT bar_date FROM market_data.candles_1d "
            f"WHERE bar_date >= '{start}' AND bar_date <= '{end}'"
        ).result_rows
    }
    rows: list[list] = []
    for i in range((end - start).days + 1):
        d = start + timedelta(days=i)
        day = api_days.get(d)
        if day is not None:
            is_open = bool(day.get("isTradingDay"))
            derived = False
        else:
            is_open = (d in open_past) if d <= today else (d.weekday() < 5)
            derived = True
        payload = {"date": d.isoformat(), "is_open": is_open,
                   "exchange": EXCHANGE, "derived": derived, "api": day}
        rows.append(row("trading_day", None, midnight(d), d, None, 0, payload))
    return rows


def _chunk_dates(from_dt: datetime, to_dt: datetime, chunk_months: int) -> list[tuple[str, str]]:
    """Разбить диапазон from_dt..to_dt на чанки по chunk_months месяцев.
    Возвращает список кортежей (from_iso, to_iso) включительно по границам."""
    chunks = []
    cur = from_dt
    while cur < to_dt:
        # конец чанка: cur + chunk_months месяцев - 1 день
        # используем замену месяца для точного перехода
        month = cur.month + chunk_months
        year = cur.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        try:
            chunk_end = cur.replace(year=year, month=month) - timedelta(days=1)
        except ValueError:
            # конец месяца (например, 31 января + 1 месяц = 28/29 февраля)
            chunk_end = cur.replace(year=year, month=month, day=1) - timedelta(days=1)
        if chunk_end > to_dt:
            chunk_end = to_dt
        chunks.append((
            cur.strftime("%Y-%m-%dT%H:%M:%SZ"),
            chunk_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ))
        cur = chunk_end + timedelta(days=1)
    return chunks


def collect_dividends(ti: TInvestClient, inv: dict, now: datetime) -> list[list]:
    rows = []
    today = now.date()
    # Запрашиваем полную доступную историю (с 2010 года) для полноты данных
    from_dt = datetime(DIV_FULL_HISTORY_FROM, 1, 1, tzinfo=timezone.utc)
    to_dt = now + timedelta(days=DIV_YEARS_FWD * 365)

    # разбиваем окно на чанки по DIV_CHUNK_MONTHS месяцев
    chunks = _chunk_dates(from_dt, to_dt, DIV_CHUNK_MONTHS)
    print(f"Дивиденды: окно {from_dt.date()}..{to_dt.date()}, чанков: {len(chunks)} по {DIV_CHUNK_MONTHS} мес.")

    for ticker, info in inv.items():
        if info.get("type") != "share" or not info.get("uid"):
            continue
        all_divs = []
        for i, (chunk_from, chunk_to) in enumerate(chunks):
            time.sleep(SLEEP_SEC)
            divs = ti.get_dividends(info["uid"], chunk_from, chunk_to)
            all_divs.extend(divs)
            print(f"  {ticker} чанк {i+1}/{len(chunks)} {chunk_from[:10]}..{chunk_to[:10]}: {len(divs)} записей")

        # дедупликация по (instrument_uid, expected_date) перед добавлением в rows
        seen = set()
        for dv in all_divs:
            rec = dv.get("recordDate") or dv.get("lastBuyDate")
            if not rec:
                continue
            d = parse_date(rec)
            if d > today + timedelta(days=DIV_YEARS_FWD * 365):
                continue
            key = (info["uid"], d)
            if key in seen:
                continue
            seen.add(key)
            rows.append(row("dividend", info["uid"], midnight(d), d, None, 2, dv))
        print(f"{ticker}: дивидендов всего {len(all_divs)}, уникальных после дедуп: {len(seen)}")
    return rows


def collect_expiries(ti: TInvestClient, inv: dict) -> list[list]:
    fut_uids = {info["uid"]: t for t, info in inv.items()
                if info.get("type") == "future" and info.get("uid")}
    futures = [f for f in ti.futures_all() if f.get("uid") in fut_uids]
    rows = []
    for f in futures:
        exp = f.get("expirationDate")
        if not exp:
            continue
        d = parse_date(exp)
        if d.year >= EXPIRY_MAX_YEAR:
            print(f"{fut_uids[f['uid']]}: пропуск заглушки экспирации {d}")
            continue
        rows.append(row("expiry", f["uid"], midnight(d), d, None, 2, f))
        print(f"{fut_uids[f['uid']]}: экспирация {d}")
    return rows


def collect_reports(ti: TInvestClient, inv: dict, today: date) -> list[list]:
    rows = []
    for ticker, info in inv.items():
        if info.get("type") != "share" or not info.get("uid"):
            continue
        time.sleep(SLEEP_SEC)
        events = ti.get_asset_reports(info["uid"])
        kept = 0
        for ev in events:
            rd = ev.get("reportDate")
            if not rd:
                continue
            d = parse_date(rd)
            actual = d if d <= today else None  # прошлое — факт, будущее — расписание
            rows.append(row("report", info["uid"], midnight(d), d, actual, 2, ev))
            kept += 1
        print(f"{ticker}: отчётов {kept}")
    return rows


def main() -> int:
    settings = Settings.load()
    inv = json.loads(INSTRUMENTS.read_text())
    ti = TInvestClient(settings)
    ch = get_client(settings)
    sha = git_sha()
    now = datetime.now(timezone.utc)
    today = now.date()

    errors = 0
    try:
        cal = collect_calendar(ti, ch, today)
        divs = collect_dividends(ti, inv, now)
        exps = collect_expiries(ti, inv)
        reps = collect_reports(ti, inv, today)
        rows = cal + divs + exps + reps

        counts: dict[str, int] = {}
        for r in rows:
            counts[r[0]] = counts.get(r[0], 0) + 1
        for t, n in sorted(counts.items()):
            print(f"{t}: {n}")
        print(f"итого событий: {len(rows)}")

        # события размазаны по месяцам (партиция = toYYYYMM(event_time)):
        # >100 партиций в одном блоке ClickHouse не принимает (код 252)
        by_month: dict[tuple[int, int], list] = {}
        for r in rows:
            ts = r[2]
            key = (ts.year, ts.month) if ts else (1970, 1)
            by_month.setdefault(key, []).append(r)
        for data in by_month.values():
            for i in range(0, len(data), 10_000):
                ch.insert(
                    table="events", database="market_data",
                    column_names=EVENT_COLUMNS, data=data[i : i + 10_000],
                )

        ch.insert(
            table="etl_runs", database="market_data_aux",
            column_names=["transform", "date_from", "date_to", "rows_in", "rows_out",
                          "errors", "status", "finished_at", "code_version"],
            data=[[
                "collect_events",
                today - timedelta(days=CAL_BACK_DAYS),
                today + timedelta(days=CAL_FWD_DAYS),
                len(rows), len(rows), 0,
                "ok" if rows else "failed",
                datetime.now(timezone.utc), sha,
            ]],
        )
        return 0 if rows else 1
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: сбор событий не удался: {e}")
        ch.insert(
            table="etl_runs", database="market_data_aux",
            column_names=["transform", "rows_in", "rows_out", "errors", "status",
                          "finished_at", "code_version"],
            data=[["collect_events", 0, 0, 1, "failed", now, sha]],
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())