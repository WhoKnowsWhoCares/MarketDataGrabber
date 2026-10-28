#!/usr/bin/env python3
"""Ежедневный Telegram-дайджест по universe из gold-витрин.

Собирает текст через mdgrabber.report.build_digest (переиспользуется ботом
в P3.2) и отправляет админу через mdgrabber.notify (category='market').

Использование:
    set -a; source .env; set +a; .venv/bin/python scripts/daily_digest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mdgrabber.config import Settings
from mdgrabber.logging import get_logger, setup_logging
from mdgrabber.notify import send_telegram
from mdgrabber.report import build_digest

log = get_logger("daily_digest")


def main() -> int:
    setup_logging()
    settings = Settings.load()
    text = build_digest(settings)
    try:
        send_telegram(settings, text, category="market", parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        log.error("telegram_send_failed", error=str(e))
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
