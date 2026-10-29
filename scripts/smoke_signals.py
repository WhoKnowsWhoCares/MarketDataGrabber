#!/usr/bin/env python3
"""Smoke движка сигналов (P4.1): detect_signals на живом CH, без вставки.

Использование:
    set -a; source .env; set +a
    PYTHONPATH=src .venv/bin/python scripts/smoke_signals.py [--lookback 3]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mdgrabber.ch import get_client  # noqa: E402
from mdgrabber.config import Settings  # noqa: E402
from mdgrabber.signals import detect_signals  # noqa: E402


def load_instruments(path: Path) -> dict[str, str]:
    """config/instruments.json {ticker: {uid, ...}} -> {uid: ticker}."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return {v["uid"]: k for k, v in data.items() if v.get("uid")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback", type=int, default=3)
    args = parser.parse_args()

    instruments = load_instruments(ROOT / "config" / "instruments.json")
    print(f"Инструментов в справочнике: {len(instruments)}")

    client = get_client(Settings.load())
    signals = detect_signals(client, instruments, lookback=args.lookback)

    print(f"Найдено сигналов: {len(signals)} (вставки нет — только детекция)")
    for s in signals:
        print(f"  {s['bar_date']}  {s['signal_type']:<15} {s['message']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
