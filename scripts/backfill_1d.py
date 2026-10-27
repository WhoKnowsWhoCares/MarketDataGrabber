#!/usr/bin/env python3
"""Бэкофилл дневных свечей через GetCandles → market_data.candles_1d.

Для каждого инструмента из config/instruments.json ОДИН запрос
get_candles(uid, 'CANDLE_INTERVAL_DAY', from_iso, to_iso), where
from_iso = (сейчас − 6 лет), to_iso = сейчас (UTC). bar_date = дата из
поля time (UTC), volume как есть (лоты), source='api'. Дедупликация не
нужна: ReplacingMergeTree схлопывает по (instrument_uid, bar_date),
повторный запуск безопасен.

Использование:
    set -a; source .env; set +a; .venv/bin/python scripts/backfill_1d.py
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
from mdgrabber.notify import send_telegram
from mdgrabber.tinvest import TInvestClient

INSTRUMENTS = ROOT / "config" / "instruments.json"
YEARS_BACK = 6
SLEEP_SEC = 1.0
COLUMNS = ["instrument_uid", "bar_date", "open", "high", "low", "close", "volume", "source"]


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except Exception as e:  # noqa: BLE001
        print(f"WARN: git sha недоступен: {e}")
        return ""


def parse_candle_date(raw: str):
    """'2024-01-02T00:00:00Z' (возможны доли секунды) → date (UTC)."""
    raw = raw.strip().rstrip("Z")
    if "." in raw:
        raw = raw.split(".", 1)[0]
    return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).date()


def main() -> int:
    settings = Settings.load()
    inv = json.loads(INSTRUMENTS.read_text())

    ti = TInvestClient(settings)
    ch = get_client(settings)
    sha = git_sha()
    now = datetime.now(timezone.utc)
    from_iso = (now - timedelta(days=YEARS_BACK * 365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    to_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"окно запроса: {from_iso} .. {to_iso}")

    summary: dict[str, int] = {}
    first: dict[str, str] = {}
    last: dict[str, str] = {}
    failed = 0

    for n, (ticker, info) in enumerate(inv.items()):
        if n:
            time.sleep(SLEEP_SEC)  # ≤1 запрос/сек
        uid = info.get("uid") or ""
        if not uid:
            print(f"SKIP {ticker}: нет uid")
            failed += 1
            continue
        try:
            candles = ti.get_candles(uid, "CANDLE_INTERVAL_DAY", from_iso, to_iso)
        except Exception as e:  # noqa: BLE001
            print(f"ERROR {ticker}: {e}")
            failed += 1
            ch.insert(
                table="etl_runs",
                database="market_data_aux",
                column_names=["transform", "rows_in", "rows_out", "errors", "status",
                              "finished_at", "code_version"],
                data=[["backfill_1d", 0, 0, 1, "failed", datetime.now(timezone.utc), sha]],
            )
            continue

        rows = [
            [
                uid,
                parse_candle_date(c.time),
                c.open, c.high, c.low, c.close, c.volume, "api",
            ]
            for c in candles
        ]
        for i in range(0, len(rows), 10_000):
            ch.insert(
                table="candles_1d",
                database="market_data",
                column_names=COLUMNS,
                data=rows[i : i + 10_000],
            )
        dates = [r[1] for r in rows]
        summary[ticker] = len(rows)
        first[ticker] = min(dates).isoformat() if dates else ""
        last[ticker] = max(dates).isoformat() if dates else ""
        ch.insert(
            table="etl_runs",
            database="market_data_aux",
            column_names=["transform", "date_from", "date_to", "rows_in", "rows_out",
                          "errors", "status", "finished_at", "code_version"],
            data=[[
                "backfill_1d",
                min(dates) if dates else None,
                max(dates) if dates else None,
                len(candles),
                len(rows),
                0,
                "ok" if rows else "failed",
                datetime.now(timezone.utc),
                sha,
            ]],
        )
        rng = f"{first[ticker]}..{last[ticker]}" if dates else "пусто"
        print(f"{ticker}: {len(rows)} дневных свечей, {rng}")

    total = sum(summary.values())
    print(f"\nитого строк: {total}, ошибок: {failed}")

    lines = ["backfill_1d: бэкофилл дневных свечей завершён", f"строк всего: {total}"]
    for ticker, cnt in summary.items():
        lines.append(f"{ticker}: {cnt} ({first[ticker]}..{last[ticker]})")
    if failed:
        lines.append(f"ошибок: {failed}")
    try:
        send_telegram(settings, "\n".join(lines), category="ops")
        print("Telegram: сводка отправлена")
    except Exception as e:  # noqa: BLE001
        print(f"WARN: Telegram недоступен: {e} — продолжаем")

    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())
