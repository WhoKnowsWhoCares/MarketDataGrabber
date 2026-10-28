"""Telegram-бот (aiogram 3, long polling).

Команды: /start, /digest, /status, /subscribe, /unsubscribe.
Прокси: socks5h:// через AiohttpSession.
Ошибка в хендлере → лог + дружелюбное сообщение, бот не падает.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryError
from aiogram.filters import Command
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# ---------------------------------------------------------------------------
# Локальные импорты
# ---------------------------------------------------------------------------
from .config import Settings
from .users import add_user, add_subscription, get_user, init_db, list_subscribers, remove_subscription
from .ratelimit import allow as _allow
from .notify import send_telegram
from .report import build_digest

log = logging.getLogger(__name__)

router = Router()


# ---------------------------------------------------------------------------
# Вспомогательные
# ---------------------------------------------------------------------------

async def _safe_reply(msg: Message, text: str) -> None:
    """Отправить ответ с защитой от ошибок."""
    try:
        await msg.answer(text, parse_mode=ParseMode.HTML)
    except Exception:
        log.exception("Ошибка отправки ответа")


def _error_text(exc: Exception) -> str:
    return f"⚠️ Произошла ошибка. Попробуйте позже."


# ---------------------------------------------------------------------------
# Хендлеры команд
# ---------------------------------------------------------------------------

@router.message(Command("start"))
async def cmd_start(msg: Message, bot: Bot) -> None:
    user = get_user(str(msg.from_user.id))
    if user is None:
        uid = add_user(str(msg.from_user.id))
        log.info("Новый пользователь зарегистрирован: chat_id=%s user_id=%d", msg.from_user.id, uid)
        await _safe_reply(
            msg,
            "👋 Добро пожаловать!\n\n"
            "Я — бот для рыночных данных.\n\n"
            "Доступные команды:\n"
            "/digest — ежедневный дайджест\n"
            "/status — свежесть данных\n"
            "/subscribe <TICKER> [тип] — подписаться на сигналы\n"
            "/unsubscribe — отписаться",
        )
    else:
        await _safe_reply(msg, "Вы уже зарегистрированы. Используйте /help для команд.")


@router.message(Command("digest"))
async def cmd_digest(msg: Message, bot: Bot) -> None:
    try:
        settings = Settings.load()
        if not _allow(f"digest:{msg.from_user.id}", ttl_seconds=300):
            await _safe_reply(msg, "⏳ Дайджест уже отправлен. Подождите 5 минут.")
            return
        text = build_digest(settings)
        await bot.send_message(
            chat_id=msg.from_user.id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        log.exception("Ошибка /digest")
        await _safe_reply(msg, _error_text(exc))


@router.message(Command("status"))
async def cmd_status(msg: Message, bot: Bot) -> None:
    try:
        from .ch import get_client
        settings = Settings.load()
        client = get_client(settings)
        row_date = client.query(
            "SELECT max(bar_date) AS d FROM market_data_gold.mart_daily_metrics"
        )
        row_time = client.query(
            "SELECT max(bar_time) AS t FROM market_data_gold.mart_daily_metrics"
        )
        bar_date = list(row_date.named_results())[0]["d"]
        bar_time = list(row_time.named_results())[0]["t"] or ""
        now = datetime.now(timezone.utc)
        if bar_time:
            latest_dt = f"{bar_date}T{bar_time}"
            age_minutes = (now - datetime.fromisoformat(latest_dt).replace(tzinfo=timezone.utc)).total_seconds() / 60
        else:
            latest_dt = str(bar_date)
            age_minutes = (now.date() - bar_date).days * 1440

        if age_minutes < 120:
            freshness = "✅ свежие"
        elif age_minutes < 1440:
            freshness = "⚠️ до вчерашнего дня"
        else:
            freshness = "❌ устарели"

        await _safe_reply(
            msg,
            f"📊 Данные за {bar_date}\n"
            f"Последнее обновление: {latest_dt}\n"
            f"Статус: {freshness}",
        )
    except Exception as exc:
        log.exception("Ошибка /status")
        await _safe_reply(msg, _error_text(exc))


@router.message(Command("subscribe"))
async def cmd_subscribe(msg: Message) -> None:
    try:
        parts = msg.text.split()
        if len(parts) < 2:
            await _safe_reply(
                msg,
                "Использование: /subscribe <TICKER|*> [signal_type]\n"
                "Примеры:\n"
                "/subscribe SBER\n"
                "/subscribe * buy\n"
                "/subscribe GAZP sell",
            )
            return
        instrument = parts[1].upper()
        signal_type = parts[2] if len(parts) > 2 else "*"
        add_subscription(str(msg.from_user.id), instrument, signal_type)
        await _safe_reply(
            msg,
            f"✅ Подписка добавлена: {instrument}"
            + (f" ({signal_type})" if signal_type != "*" else ""),
        )
    except Exception as exc:
        log.exception("Ошибка /subscribe")
        await _safe_reply(msg, _error_text(exc))


@router.message(Command("unsubscribe"))
async def cmd_unsubscribe(msg: Message) -> None:
    try:
        parts = msg.text.split()
        instrument = parts[1].upper() if len(parts) > 1 else None
        removed = remove_subscription(str(msg.from_user.id), instrument)
        if removed:
            target = f" {instrument}" if instrument else ""
            await _safe_reply(msg, f"✅ Подписка{target} удалена.")
        else:
            await _safe_reply(
                msg,
                "⚠️ Подписок не найдено.\n"
                "Использование: /unsubscribe [TICKER]",
            )
    except Exception as exc:
        log.exception("Ошибка /unsubscribe")
        await _safe_reply(msg, _error_text(exc))


# ---------------------------------------------------------------------------
# Глобальный мидлварь-ловушка ошибок
# ---------------------------------------------------------------------------

async def error_middleware(handler, event, data):
    try:
        return await handler(event, data)
    except Exception as exc:
        log.exception("Unhandled exception in handler")
        # Пытаемся достать message из data
        msg = data.get("event_from_user")
        if hasattr(msg, "answer"):
            try:
                await msg.answer(_error_text(exc))
            except Exception:
                pass
        raise


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    settings = Settings.load()
    init_db(settings)

    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    dp.include_router(router)

    # Прокси-сессия для aiogram
    from aiogram.client.session.aiohttp import AiohttpSession
    import aiohttp
    proxy_url = settings.telegram_proxy.replace("socks5h://", "socks5://")
    session = AiohttpSession(
        connector=aiohttp.ProxyConnector.from_url(proxy_url),
    )
    bot.session = session

    dp.feed_update.start(bot)
    print("🤖 Bot starting (long polling)...")
    dp.run_polling(bot, allowed_updates=Message.ALL_UPDATE_TYPES)


if __name__ == "__main__":
    main()
