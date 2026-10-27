"""Диспетчер уведомлений Telegram.

Категории (по SPEC):
- ops    — технические алерты, ТОЛЬКО админу (ADMIN_CHAT_ID), мимо подписок;
- market — рыночные сигналы/дайджесты, пока тоже админу;
          в P3 появится таблица подписок и per-user фильтры.
Прямые вызовы Telegram API из коллекторов запрещены — всё через этот модуль.
"""
from __future__ import annotations

import httpx

from .config import Settings

API = "https://api.telegram.org"


def send_telegram(
    settings: Settings,
    text: str,
    category: str = "ops",
    chat_id: str | None = None,
) -> dict:
    """Отправить сообщение. ops — всегда админу (chat_id игнорируется)."""
    if category not in ("ops", "market"):
        raise ValueError(f"неизвестная категория: {category}")
    target = settings.admin_chat_id if (category == "ops" or chat_id is None) else chat_id
    with httpx.Client(
        verify=settings.tls_verify, proxy=settings.telegram_proxy, timeout=30.0
    ) as http:
        r = http.post(
            f"{API}/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": target, "text": text},
        )
        r.raise_for_status()
        return r.json()
