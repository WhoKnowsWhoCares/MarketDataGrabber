#!/usr/bin/env python3
"""Ежедневный инкремент свечей 1m + 1d через GetCandles → market_data.

Для каждого инструмента из config/instruments.json:
  1m: last = max(bar_time) из candles_1m; старт = last − 1 час
      (перекрытие на дозакрытие свечей), конец = сейчас. Если диапазон
      больше суток — режется на посуточные запросы (лимит API 2400
      свечей ≈ 1 торговый день), пауза 1 c между запросами. Пустой
      ответ — норма (праздник). Вставка батчем, source='api'.
  1d: last_date = max(bar_date); один запрос от last_date до сейчас,
      вставка, source='api'.
ReplacingMergeTree дедуплицирует по ключу — повторная вставка безопасна.

Опция --days N принудительно обновляет последние N дней (починка дыр):
окно 1m = сейчас − N дней, окно 1d = (сейчас − N дней).date() .. сейчас.

Использование:
    set -a; source .env; set +a; .venv/bin/python scripts/increment_candles.py
    .venv/bin/python scripts/increment_candles.py --days 3
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mdgrabber.ch import get_client
from mdgrabber.config import Settings
from mdgrabber.notify import send_telegram
from mdgrabber.tinvest import TInvestClient

INSTRUMENTS = ROOT / "config" / "instruments.json"
SLEEP_SEC = 1.0          # ≤1 запрос/сек к API
OVERLAP = timedelta(hours=1)   # перекрытие 1m на дозакрытие
CHUNK = timedelta(days=1)      # посуточная нарезка диапазона 1m
COLUMNS_1M = ["instrument_uid", "bar_time", "open", "high", "low", "close", "volume", "source"]
COLUMNS_1D = ["instrument_uid", "bar_date", "open", "high", "low", "close", "volume", "source"]


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except Exception as e:  # noqa: BLE001
        print(f"WARN: git sha недоступен: {e}")
        return ""


def parse_ts(raw: str) -> datetime:
    """'2024-01-02T10:30:00Z' (возможны доли секунды) → datetime UTC."""
    raw = raw.strip().rstrip("Z")
    if "." in raw:
        raw = raw.split(".", 1)[0]
    return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def day_chunks(start: datetime, end: datetime):
    """[start, end) посуточными кусками — лимит API 2400 минуток за запрос."""
    cur = start
    while cur < end:
        chunk_end = min(cur + CHUNK, end)
        yield cur, chunk_end
        cur = chunk_end


def main() -> int:
    ap = argparse.ArgumentParser(description="Инкремент свечей 1m+1d из GetCandles")
    ap.add_argument("--days", type=int, default=0, metavar="N",
                    help="принудительно обновить последние N дней (починка дыр)")
    args = ap.parse_args()

    settings = Settings.load()
    inv = json.loads(INSTRUMENTS.read_text())

    ti = TInvestClient(settings)
    ch = get_client(settings)
    sha = git_sha()
    now = datetime.now(timezone.utc)

    total_1m = total_1d = 0
    failed = 0
    summary: dict[str, dict] = {}

    for n, (ticker, info) in enumerate(inv.items()):
        if n:
            time.sleep(SLEEP_SEC)  # ≤1 запрос/сек
        uid = info.get("uid") or ""
        if not uid:
            print(f"SKIP {ticker}: нет uid")
            failed += 1
            continue

        # ---------- 1m ----------
        if args.days > 0:
            start_1m = now - timedelta(days=args.days)
        else:
            row = ch.query(
                "SELECT max(bar_time) FROM market_data.candles_1m WHERE instrument_uid = {uid:String}",
                parameters={"uid": uid},
            ).result_rows
            last = row[0][0] if row and row[0][0] else None
            if last is None:
                start_1m = now - timedelta(days=3)
            else:
                start_1m = last.replace(tzinfo=timezone.utc) - OVERLAP

        new_1m = ins_1m = 0
        max_1m: datetime | None = None
        candles_fetched = 0
        err_1m = None
        for i, (frm, to) in enumerate(day_chunks(start_1m, now)):
            if i or n:
                time.sleep(SLEEP_SEC)
            from_iso = frm.strftime("%Y-%m-%dT%H:%M:%SZ")
            to_iso = to.strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                candles = ti.get_candles(uid, "CANDLE_INTERVAL_1_MIN", from_iso, to_iso)
            except Exception as e:  # noqa: BLE001
                print(f"ERROR {ticker} 1m [{from_iso}..{to_iso}]: {e}")
                err_1m = e
                break
            candles_fetched += len(candles)
            if candles:
                rows = [
                    [uid, parse_ts(c.time), c.open, c.high, c.low, c.close, c.volume, "api"]
                    for c in candles
                ]
                ch.insert(table="candles_1m", database="market_data",
                          column_names=COLUMNS_1M, data=rows)
                ins_1m += len(rows)
                chunk_max = max(r[1] for r in rows)
                max_1m = chunk_max if max_1m is None else max(max_1m, chunk_max)
        if max_1m is not None:
            row = ch.query(
                "SELECT max(bar_time) FROM market_data.candles_1m WHERE instrument_uid = {uid:String}",
                parameters={"uid": uid},
            ).result_rows
            db_max = row[0][0].replace(tzinfo=timezone.utc) if row and row[0][0] else max_1m
            new_1m = ins_1m  # вставлено строк за этот запуск

        # ---------- 1d ----------
        if args.days > 0:
            from_date = (now - timedelta(days=args.days)).date()
        else:
            row = ch.query(
                "SELECT max(bar_date) FROM market_data.candles_1d WHERE instrument_uid = {uid:String}",
                parameters={"uid": uid},
            ).result_rows
            from_date = row[0][0] if row and row[0][0] else (now - timedelta(days=7)).date()

        time.sleep(SLEEP_SEC)
        from_iso = datetime.combine(from_date, dtime.min, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        to_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        ins_1d = 0
        max_1d = None
        try:
            candles_d = ti.get_candles(uid, "CANDLE_INTERVAL_DAY", from_iso, to_iso)
        except Exception as e:  # noqa: BLE001
            print(f"ERROR {ticker} 1d [{from_iso}..{to_iso}]: {e}")
            candles_d = []
            failed += 1
        if err_1m:
            failed += 1
        if candles_d:
            rows_d = [
                [uid, parse_ts(c.time).date(), c.open, c.high, c.low, c.close, c.volume, "api"]
                for c in candles_d
            ]
            ch.insert(table="candles_1d", database="market_data",
                      column_names=COLUMNS_1D, data=rows_d)
            ins_1d = len(rows_d)
            max_1d = max(r[1] for r in rows_d)

        summary[ticker] = {
            "ins_1m": ins_1m, "max_1m": max_1m,
            "ins_1d": ins_1d, "max_1d": max_1d,
        }
        total_1m += ins_1m
        total_1d += ins_1d
        m1 = f"{max_1m:%Y-%m-%d %H:%M} UTC" if max_1m else "—"
        d1 = max_1d.isoformat() if max_1d else "—"
        print(f"{ticker}: 1m +{ins_1m} (max {m1}), 1d +{ins_1d} (max {d1})")

    # ---------- etl_runs: одна запись на запуск скрипта ----------
    max_dates = [s["max_1m"].date() for s in summary.values() if s["max_1m"]]
    max_dates += [s["max_1d"] for s in summary.values() if s["max_1d"]]
    covered_from = min(max_dates) if max_dates else None
    ch.insert(
        table="etl_runs",
        database="market_data_aux",
        column_names=["transform", "date_from", "date_to", "rows_in", "rows_out",
                      "errors", "status", "finished_at", "code_version"],
        data=[[
            "increment_candles",
            covered_from,
            now.date(),
            total_1m + total_1d,
            total_1m + total_1d,
            failed,
            "ok" if not failed else "failed",
            datetime.now(timezone.utc),
            sha,
        ]],
    )

    # ---------- сводка ----------
    print(f"\nитого: минуток +{total_1m}, дневок +{total_1d}, ошибок: {failed}")
    lines = [
        "increment_candles: инкремент 1m+1d ЗАВЕРШЁН С ОШИБКАМИ",
        f"минуток: +{total_1m}, дневок: +{total_1d}",
    ]
    for ticker, s in summary.items():
        m1 = f"{s['max_1m']:%Y-%m-%d %H:%M}" if s["max_1m"] else "—"
        d1 = s["max_1d"].isoformat() if s["max_1d"] else "—"
        lines.append(f"{ticker}: 1m +{s['ins_1m']} (max {m1}), 1d +{s['ins_1d']} (max {d1})")
    if failed:
        lines.append(f"ошибок: {failed}")
        # В Telegram — только неуспешные прогоны (успешные видны в логах/etl_runs).
        try:
            send_telegram(settings, "\n".join(lines), category="ops")
            print("Telegram: алерт об ошибках отправлен")
        except Exception as e:  # noqa: BLE001
            print(f"WARN: Telegram недоступен: {e} — продолжаем")

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
