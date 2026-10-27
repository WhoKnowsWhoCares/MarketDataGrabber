#!/usr/bin/env python3
"""Ежедневный снапшот фундаментальных показателей акций → fundamentals_snapshots.

GetAssetFundamentals принимает asset_uid (не instrument_uid!). Резолв через
GetAssetBy кэшируется в config/instruments.json (поле asset_uid) — API-история
фундаментала недоступна, point-in-time историю накапливаем сами.

Idempotency: ReplacingMergeTree ORDER BY (asset_uid, snapshot_ts) — snapshot_ts
= полночь UTC текущего дня (та же конвенция, что event_time у событий), поэтому
повторный запуск в тот же день схлопывается после OPTIMIZE FINAL.

Опционально: GetConsensusForecasts → событие event_type='consensus'
(importance=1) в market_data.events.

Использование:
    .venv/bin/python scripts/collect_fundamentals.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, time as dtime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mdgrabber.ch import get_client
from mdgrabber.config import Settings
from mdgrabber.tinvest import TInvestClient

INSTRUMENTS = ROOT / "config" / "instruments.json"
SLEEP_SEC = 1.0  # ≤1 запрос/сек к API


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except Exception as e:  # noqa: BLE001
        print(f"WARN: git sha недоступен: {e}")
        return ""


def midnight_utc() -> datetime:
    """Полночь UTC текущего дня — ключ дня для ReplacingMergeTree."""
    now = datetime.now(timezone.utc)
    return datetime.combine(now.date(), dtime(0, 0), tzinfo=timezone.utc)


def resolve_asset_uids(ti: TInvestClient, instruments: dict) -> dict[str, dict]:
    """Для акций: instrument_uid → asset_uid (кэш в instruments.json).

    GetAssetBy по этим акциям отвечает 404 «Asset not found» (баг REST-API),
    фолбэк — поиск по GetAssets: у каждого актива есть instruments[] с uid.
    """
    changed = False
    shares = {t: meta for t, meta in instruments.items() if meta.get("type") == "share"}
    todo = {meta["uid"]: t for t, meta in shares.items() if not meta.get("asset_uid")}
    if todo:
        assets = None
        for uid, ticker in todo.items():
            try:
                asset = ti.get_asset_by(uid)
                time.sleep(SLEEP_SEC)
            except Exception as e:  # noqa: BLE001
                print(f"{ticker}: GetAssetBy не сработал ({e}), фолбэк GetAssets")
                if assets is None:
                    assets = ti.get_assets()
                    time.sleep(SLEEP_SEC)
                asset = next(
                    (a for a in assets
                     if any(i.get("uid") == uid for i in a.get("instruments", []))),
                    {},
                )
            if not asset.get("uid"):
                print(f"WARN: {ticker}: asset_uid не найден — пропуск")
                continue
            shares[ticker]["asset_uid"] = asset["uid"]
            changed = True
            print(f"{ticker}: asset_uid = {asset['uid']}")
    if changed:
        INSTRUMENTS.write_text(
            json.dumps(instruments, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print("config/instruments.json обновлён (asset_uid закэширован)")
    return {t: m for t, m in shares.items() if m.get("asset_uid")}


def main() -> int:
    settings = Settings.load()
    ti = TInvestClient(settings)
    ch = get_client(settings)

    instruments = json.loads(INSTRUMENTS.read_text(encoding="utf-8"))
    shares = resolve_asset_uids(ti, instruments)
    if not shares:
        print("Нет акций в справочнике — нечего собирать")
        return 1

    asset_uids = [m["asset_uid"] for m in shares.values()]
    resp = ti.get_fundamentals(asset_uids)
    # API отдаёт camelCase: assetUid, peRatioTtm, priceToBookTtm, ...
    by_asset = {f["assetUid"]: f for f in resp.get("fundamentals", [])}

    snapshot_ts = midnight_utc()
    rows, summary = [], []
    for ticker, meta in shares.items():
        fund = by_asset.get(meta["asset_uid"])
        if not fund:
            print(f"WARN: {ticker}: фундаментал не получен")
            continue
        payload = json.dumps(fund, ensure_ascii=False)
        rows.append([snapshot_ts, meta["asset_uid"], payload])
        # выжимка для отчёта
        keys = ["peRatioTtm", "priceToBookTtm", "marketCapitalization",
                "forwardAnnualDividendYield", "dividendYieldDailyTtm", "exDividendDate"]
        summary.append({k: fund[k] for k in keys if fund.get(k)} | {"ticker": ticker})

    if not rows:
        print("FAIL: ни одного снапшота не собрано")
        return 1

    ch.insert(
        table="fundamentals_snapshots",
        database="market_data",
        column_names=["snapshot_ts", "asset_uid", "payload"],
        data=rows,
    )
    print(f"fundamentals_snapshots: вставлено {len(rows)} снапшотов (snapshot_ts={snapshot_ts})")

    # --- опционально: консенсус-прогнозы → events (event_type='consensus') ---
    consensus_note = "пропущено"
    try:
        time.sleep(SLEEP_SEC)
        cf = ti.get_consensus_forecasts()
        items = cf.get("items", [])
        asset_to_ticker = {m["asset_uid"]: t for t, m in shares.items()}
        ev_rows = []
        for it in items:
            ticker = asset_to_ticker.get(it.get("assetUid"))
            if not ticker:
                continue
            ev_rows.append([
                "consensus",
                shares[ticker]["uid"],
                snapshot_ts,          # полночь UTC — тот же ключ дня
                None, None,
                1,
                "api",
                json.dumps(it, ensure_ascii=False),
            ])
        if ev_rows:
            # ≤100 партиций в одном блоке: событий мало, но группируем на всякий случай
            ch.insert(
                table="events",
                database="market_data",
                column_names=["event_type", "instrument_uid", "event_time", "expected_date",
                              "actual", "importance", "source", "payload"],
                data=ev_rows,
            )
            consensus_note = f"вставлено {len(ev_rows)}"
    except Exception as e:  # noqa: BLE001
        consensus_note = f"пропущено: {e}"
    print(f"consensus-прогнозы: {consensus_note}")

    # --- etl_runs ---
    ch.insert(
        table="etl_runs",
        database="market_data_aux",
        column_names=["transform", "date_from", "date_to", "rows_in", "rows_out",
                      "errors", "status", "finished_at", "code_version"],
        data=[[
            "collect_fundamentals",
            snapshot_ts.date(),
            snapshot_ts.date(),
            len(rows),
            len(rows),
            0,
            "ok",
            datetime.now(timezone.utc),
            git_sha(),
        ]],
    )

    # --- выжимка ---
    print("\n=== Фундаментал ===")
    for s in summary:
        print(json.dumps(s, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
