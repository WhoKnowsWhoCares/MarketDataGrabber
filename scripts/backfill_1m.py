#!/usr/bin/env python3
"""Бэкофилл минутных свечей из годовых архивов T-Invest → market_data.candles_1m.

Для каждого инструмента из config/instruments.json перебирает годы 2019..текущий,
тянет ZIP-архив через history-data (параметр — ТОЛЬКО figi), парсит CSV и грузит
в ClickHouse батчами с source='archive'. Дедупликация не нужна: ReplacingMergeTree
схлопывает по ключу, повторный запуск безопасен.

Использование:
    set -a; source .env; set +a; .venv/bin/python scripts/backfill_1m.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mdgrabber.ch import get_client
from mdgrabber.config import Settings
from mdgrabber.notify import send_telegram
from mdgrabber.tinvest import TInvestClient, TInvestError

INSTRUMENTS = ROOT / "config" / "instruments.json"
FIRST_YEAR = 2019
BATCH = 50_000
SLEEP_SEC = 1.0
COLUMNS = ["instrument_uid", "bar_time", "open", "high", "low", "close", "volume", "source"]


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except Exception as e:  # noqa: BLE001
        print(f"WARN: git sha недоступен: {e}")
        return ""


def resolve_instruments(settings: Settings, inv: dict) -> dict:
    """Дозаполнить пустые uid/figi через API и сохранить справочник."""
    client = TInvestClient(settings)
    if any(i["type"] == "future" and not i["figi"] for i in inv.values()):
        futures = client.futures_all()
        by_ticker = {f.get("ticker"): f for f in futures}
    else:
        by_ticker = {}
    for ticker, info in inv.items():
        if info["type"] == "share" and (not info["figi"] or not info["uid"]):
            inst = client.share_by(ticker, info["class_code"])
            info["figi"] = info["figi"] or inst.get("figi", "")
            info["uid"] = info["uid"] or inst.get("uid", "")
            print(f"resolve {ticker}: figi={info['figi']} uid={info['uid']}")
        elif info["type"] == "future" and not info["figi"]:
            inst = by_ticker.get(ticker, {})
            # у фьючерса для history-data берём поле figi из ответа
            info["figi"] = info["figi"] or inst.get("figi", "")
            info["uid"] = info["uid"] or inst.get("uid", "")
            print(f"resolve {ticker}: figi={info['figi']} uid={info['uid']}")
    INSTRUMENTS.write_text(json.dumps(inv, indent=2, ensure_ascii=False) + "\n")
    print(f"справочник обновлён: {INSTRUMENTS}")
    return inv


def parse_bar_time(raw: str) -> datetime:
    """'2024-01-02T10:30:00Z' (возможны доли секунды) → datetime UTC."""
    raw = raw.strip().rstrip("Z")
    if "." in raw:
        raw = raw.split(".", 1)[0]
    return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def gaps_in_year(rows: list[dict]) -> int:
    """Паузы > 10 мин между соседними минутками внутри одного календарного дня."""
    ts = sorted(r["bar_time"] for r in rows)
    gaps = 0
    for a, b in zip(ts, ts[1:]):
        if (b - a).total_seconds() > 600 and a.date() == b.date():
            gaps += 1
    return gaps


def main() -> int:
    settings = Settings.load()
    inv = json.loads(INSTRUMENTS.read_text())
    inv = resolve_instruments(settings, inv)

    ti = TInvestClient(settings)
    ch = get_client(settings)
    sha = git_sha()
    current_year = datetime.now(timezone.utc).year
    years = list(range(FIRST_YEAR, current_year + 1))

    report: dict[str, list[dict]] = {}
    anomalies: list[str] = []
    total_out = 0
    failed = 0

    for ticker, info in inv.items():
        uid, figi = info["uid"], info["figi"]
        if not figi or not uid:
            print(f"SKIP {ticker}: не удалось резолвить figi/uid")
            failed += 1
            anomalies.append(f"{ticker}: не резолвится")
            continue
        report[ticker] = []
        inst_rows_in = inst_rows_out = 0
        inst_min = inst_max = None
        for year in years:
            time.sleep(SLEEP_SEC)
            try:
                data = ti.history_zip(figi, year)
                rows = TInvestClient.parse_history_zip(data)
            except TInvestError as e:
                msg = str(e)
                if "HTTP 400" in msg or "HTTP 404" in msg or "HTTP 204" in msg:
                    print(f"{ticker} {year}: нет архива ({msg.splitlines()[0][:80]}) — пропуск")
                    continue
                print(f"ERROR {ticker} {year}: {msg}")
                anomalies.append(f"{ticker}/{year}: ошибка API")
                failed += 1
                continue
            if not rows:
                print(f"{ticker} {year}: пустой архив — пропуск")
                continue

            parsed = [
                {
                    "instrument_uid": uid,
                    "bar_time": parse_bar_time(r["utc"]),
                    "open": r["open"],
                    "high": r["high"],
                    "low": r["low"],
                    "close": r["close"],
                    "volume": r["volume"],
                    "source": "archive",
                }
                for r in rows
            ]
            for i in range(0, len(parsed), BATCH):
                ch.insert(
                    table="candles_1m",
                    database="market_data",
                    column_names=COLUMNS,
                    data=[
                        [
                            b["instrument_uid"], b["bar_time"], b["open"], b["high"],
                            b["low"], b["close"], b["volume"], b["source"],
                        ]
                        for b in parsed[i : i + BATCH]
                    ],
                )
            tmin = min(b["bar_time"] for b in parsed)
            tmax = max(b["bar_time"] for b in parsed)
            gaps = gaps_in_year(parsed)
            report[ticker].append(
                {"year": year, "rows": len(parsed), "min": tmin, "max": tmax, "gaps": gaps}
            )
            inst_rows_in += len(rows)
            inst_rows_out += len(parsed)
            inst_min = tmin if inst_min is None else min(inst_min, tmin)
            inst_max = tmax if inst_max is None else max(inst_max, tmax)
            print(f"{ticker} {year}: {len(parsed)} строк, {tmin:%Y-%m-%d}..{tmax:%Y-%m-%d}, дыр={gaps}")
            if gaps:
                anomalies.append(f"{ticker}/{year}: {gaps} дыр >10мин")

        # etl_runs: одна запись на инструмент
        status = "ok" if inst_rows_out else "failed"
        ch.insert(
            table="etl_runs",
            database="market_data_aux",
            column_names=["transform", "date_from", "date_to", "rows_in", "rows_out",
                          "errors", "status", "finished_at", "code_version"],
            data=[[
                "backfill_1m",
                inst_min.date() if inst_min else None,
                inst_max.date() if inst_max else None,
                inst_rows_in,
                inst_rows_out,
                failed if status == "failed" else 0,
                status,
                datetime.now(timezone.utc),
                sha,
            ]],
        )
        total_out += inst_rows_out

    # --- Отчёт полноты -------------------------------------------------------
    print("\n=== Отчёт полноты (instrument × year) ===")
    for ticker, years_rep in report.items():
        print(f"\n{ticker}:")
        for y in years_rep:
            print(
                f"  {y['year']}: {y['rows']:>8} строк, "
                f"{y['min']:%Y-%m-%dT%H:%M} .. {y['max']:%Y-%m-%dT%H:%M} UTC, дыр={y['gaps']}"
            )

    # --- Telegram-сводка -----------------------------------------------------
    lines = [
        "backfill_1m: бэкофилл минутных свечей завершён",
        f"строк всего: {total_out}",
    ]
    for ticker, years_rep in report.items():
        s = sum(y["rows"] for y in years_rep)
        yrs = ", ".join(f"{y['year']}:{y['rows']}" for y in years_rep) or "нет архивов"
        lines.append(f"{ticker}: {s} ({yrs})")
    if anomalies:
        lines.append("аномалии: " + "; ".join(anomalies[:15]))
    if failed:
        lines.append(f"ошибок: {failed}")
    try:
        send_telegram(settings, "\n".join(lines), category="ops")
        print("\nTelegram: сводка отправлена")
    except Exception as e:  # noqa: BLE001
        print(f"\nWARN: Telegram недоступен: {e} — продолжаем (exit code не меняется)")

    return 0 if total_out else 1


if __name__ == "__main__":
    sys.exit(main())
