"""Проверка T-Invest read-токена на живом API. Запуск: .venv/bin/python scripts/smoke_tinvest.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mdgrabber.config import Settings
from mdgrabber.tinvest import TInvestClient


def main() -> int:
    s = Settings.load()
    c = TInvestClient(s)
    accs = c.get_accounts().get("accounts", [])
    print(f"accounts: {len(accs)}")
    sber = c.share_by("SBER")
    print(f"SBER uid: {sber.get('uid')}")
    candles = c.get_candles(
        sber["uid"], "CANDLE_INTERVAL_1_MIN", "2026-10-23T10:00:00Z", "2026-10-23T11:00:00Z"
    )
    print(f"SBER 1m candles: {len(candles)}")
    fut = [f for f in c.futures_all() if f.get("ticker") == "CNYRUBF"]
    print(f"CNYRUBF найден: {bool(fut)}")
    data = c.history_zip("BBG004730N88", 2024)
    rows = c.parse_history_zip(data)
    print(f"SBER 2024 архив: {len(rows)} минуток, первая: {rows[0]['utc'] if rows else '—'}")
    ok = len(accs) >= 1 and sber.get("uid") and len(candles) > 0 and len(rows) > 100000
    print("SMOKE:", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
