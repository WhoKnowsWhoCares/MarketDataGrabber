#!/usr/bin/env python3
"""Smoke replay-движка бэктеста (P5.1): replay_signals на живом CH, без вставки.

Использование:
    set -a; source .env; set +a
    PYTHONPATH=src .venv/bin/python scripts/smoke_backtest.py
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mdgrabber.backtest import replay_signals  # noqa: E402
from mdgrabber.ch import get_client  # noqa: E402
from mdgrabber.config import Settings  # noqa: E402


def load_instruments(path: Path) -> dict[str, str]:
    """config/instruments.json {ticker: {uid, ...}} -> {uid: ticker}."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return {v["uid"]: k for k, v in data.items() if v.get("uid")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    instruments = load_instruments(ROOT / "config" / "instruments.json")
    print(f"Инструментов в справочнике: {len(instruments)}")

    client = get_client(Settings.load())
    rows = replay_signals(client, instruments)

    print(f"Найдено сигналов: {len(rows)} (вставки нет — только replay)")
    counts = Counter((r["ticker"], r["signal_type"]) for r in rows)
    for (ticker, signal_type), n in sorted(counts.items()):
        print(f"  {ticker:<12} {signal_type:<15} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
