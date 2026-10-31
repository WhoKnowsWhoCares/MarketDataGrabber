"""Telegram-бот (aiogram 3, long polling).

Команды: /start, /digest, /status, /subscribe, /unsubscribe, /trade.
Прокси: socks5h:// через AiohttpSession.
Ошибка в хендлере → лог + дружелюбное сообщение, бот не падает.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# ---------------------------------------------------------------------------
# Локальные импорты
# ---------------------------------------------------------------------------
from .config import Settings
from .logging import get_logger
from .ratelimit import allow as _allow
from .trade import TradeClient, TradeError
from .users import add_user, add_subscription, get_user, init_db, list_subscribers, list_subscriptions, remove_subscription
from .notify import send_telegram
from .report import build_digest

log = logging.getLogger(__name__)
# structlog-логгер для торгового флоу (JSON-события)
logger = get_logger("bot")

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


def _is_admin(chat_id: str, settings: Settings) -> bool:
    return str(chat_id) == str(settings.admin_chat_id)


def _ticker_to_figi(ticker: str) -> str | None:
    """Получить FIGI/UID по тикеру из instruments.json."""
    instruments_path = Path(__file__).resolve().parents[2] / "config" / "instruments.json"
    try:
        data = json.loads(instruments_path.read_text(encoding="utf-8"))
        info = data.get(ticker.upper())
        if info:
            return info.get("figi") or info.get("uid")
    except Exception:
        log.exception("Ошибка чтения instruments.json")
    return None


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
            "/unsubscribe — отписаться\n"
            "/trade <TICKER> buy|sell [lots] — исполнить сделку (только админ)",
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
            "SELECT max(bar_time) AS t FROM market_data.candles_1m"
        )
        bar_date = list(row_date.named_results())[0]["d"]
        bar_time = list(row_time.named_results())[0]["t"]  # datetime или None
        now = datetime.now(timezone.utc)
        if bar_time:
            # bar_time из candles_1m — уже полное datetime (UTC)
            latest_dt = bar_time.replace(tzinfo=timezone.utc)
            age_minutes = (now - latest_dt).total_seconds() / 60
        else:
            latest_dt = bar_date
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


@router.message(Command("trade"))
async def cmd_trade(msg: Message) -> None:
    """/trade TICKER buy|sell [lots] — только админ."""
    settings = Settings.load()
    chat_id = str(msg.from_user.id)

    # Guard: только админ
    if not _is_admin(chat_id, settings):
        logger.warning("trade_rejected", reason="not_admin")
        await _safe_reply(msg, "❌ Команда недоступна: только для администратора.")
        return

    # Guard: sandbox-only
    if settings.trade_mode != "sandbox":
        logger.warning("trade_rejected", reason="trade_error")
        await _safe_reply(msg, "❌ Trading disabled: trade_mode != sandbox")
        return

    # Parse аргументов
    parts = msg.text.split()
    if len(parts) < 3:
        await _safe_reply(
            msg,
            "Использование: /trade <TICKER> buy|sell [lots]\n"
            "Пример: /trade SBER buy 1\n"
            f"Макс. лотов: {settings.trade_max_lots}",
        )
        return

    ticker = parts[1].upper()
    direction = parts[2].lower()
    lots = int(parts[3]) if len(parts) > 3 else 1

    # Валидация
    if direction not in ("buy", "sell"):
        await _safe_reply(msg, "❌ Direction должен быть buy или sell")
        return

    if lots > settings.trade_max_lots or lots < 1:
        logger.warning("trade_rejected", reason="too_many_lots", lots=lots)
        await _safe_reply(msg, f"❌ Lots должен быть 1..{settings.trade_max_lots}")
        return

    figi = _ticker_to_figi(ticker)
    if not figi:
        await _safe_reply(msg, f"❌ Тикер {ticker} не найден в universe (config/instruments.json)")
        return

    # Cooldown 10с по chat_id
    if not _allow(f"trade:cooldown:{chat_id}", ttl_seconds=10):
        logger.warning("trade_rejected", reason="cooldown")
        await _safe_reply(msg, "⏳ Cooldown 10с: подождите перед следующей сделкой.")
        return

    # Формируем callback_data (короткий ключ)
    callback_id = f"{ticker}:{direction}:{lots}"
    pending_key = f"trade:pending:{chat_id}:{callback_id}"

    # Сохраняем pending в Redis (SET EX 60)
    try:
        import redis
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        r = redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        order_data = {
            "figi": figi,
            "ticker": ticker,
            "direction": direction,
            "lots": lots,
            "chat_id": chat_id,
        }
        r.setex(pending_key, 60, json.dumps(order_data))
    except Exception as exc:
        log.warning("Redis недоступен, pending не сохранён: %s", exc)
        await _safe_reply(msg, "⚠️ Сервис недоступен (Redis). Попробуйте позже.")
        return

    # Inline-панель подтверждения
    text = (
        f"⚡ SANDBOX {direction.upper()} {lots} {ticker} (~295.6 ₽/лот).\n"
        f"✅ Исполнить / ❌ Отмена (60с)"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Исполнить", callback_data=f"trade:exec:{callback_id}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"trade:cancel:{callback_id}"),
            ]
        ]
    )
    await msg.answer(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Callback handlers
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("trade:exec:"))
async def cb_trade_exec(cb: CallbackQuery) -> None:
    """Обработчик нажатия 'Исполнить'."""
    settings = Settings.load()
    chat_id = str(cb.from_user.id)
    logger.info("trade_callback", data=cb.data, chat_id=chat_id)

    # Guard: только админ
    if not _is_admin(chat_id, settings):
        logger.warning("trade_rejected", reason="not_admin")
        await cb.answer("❌ Только администратор может исполнять сделки", show_alert=True)
        return

    # Guard: sandbox-only
    if settings.trade_mode != "sandbox":
        logger.warning("trade_rejected", reason="trade_error")
        await cb.answer("❌ Trading disabled: trade_mode != sandbox", show_alert=True)
        return

    callback_id = cb.data.split(":", 2)[2]
    pending_key = f"trade:pending:{chat_id}:{callback_id}"

    # Получаем pending из Redis
    try:
        import redis
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        r = redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        pending_json = r.get(pending_key)
        if not pending_json:
            logger.warning("trade_rejected", reason="expired")
            await cb.answer("❌ Запрос истёк (60с). Повторите /trade.", show_alert=True)
            await cb.message.edit_text("❌ Время подтверждения истекло (60с).")
            return
        order = json.loads(pending_json)
        # Удаляем pending после получения
        r.delete(pending_key)
    except Exception as exc:
        log.warning("Redis недоступен при исполнении: %s", exc)
        await cb.answer("⚠️ Сервис недоступен (Redis).", show_alert=True)
        return

    # Исполняем сделку через TradeClient
    try:
        client = TradeClient(settings)
        resp = client.place_market_order(
            instrument_id=order["figi"],
            lots=order["lots"],
            direction=order["direction"],
        )
        order_id = resp.get("orderId", "unknown")
        exec_status = resp.get("executionReportStatus", "unknown")
        # Цена исполнения может быть в ответе
        price_info = ""
        if "initialOrderPrice" in resp:
            p = resp["initialOrderPrice"]
            price_info = f" по {p.get('units', '')}.{p.get('nano', 0):09d} ₽"
        elif "averagePrice" in resp:
            p = resp["averagePrice"]
            price_info = f" по {p.get('units', '')}.{p.get('nano', 0):09d} ₽"

        text = (
            f"✅ Исполнено: {order['direction'].upper()} {order['lots']} {order['ticker']}\n"
            f"Order ID: {order_id}\n"
            f"Статус: {exec_status}{price_info}"
        )
        await cb.message.edit_text(text, parse_mode=ParseMode.HTML)
        await cb.answer("Исполнено")
        logger.info(
            "trade_executed",
            order_id=order_id,
            ticker=order["ticker"],
            lots=order["lots"],
            status=exec_status,
        )
    except TradeError as exc:
        logger.warning("trade_rejected", reason="trade_error", code=exc.code)
        text = f"❌ Ошибка торговли: code={exc.code}, {exc.message}"
        await cb.message.edit_text(text, parse_mode=ParseMode.HTML)
        await cb.answer(f"Ошибка: {exc.code}", show_alert=True)
    except Exception as exc:
        logger.warning("trade_rejected", reason="trade_error", error=str(exc))
        log.exception("Ошибка исполнения сделки")
        await cb.message.edit_text(f"❌ Ошибка: {exc}")
        await cb.answer("Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("trade:cancel:"))
async def cb_trade_cancel(cb: CallbackQuery) -> None:
    """Обработчик нажатия 'Отмена'."""
    chat_id = str(cb.from_user.id)
    callback_id = cb.data.split(":", 2)[2]
    pending_key = f"trade:pending:{chat_id}:{callback_id}"

    # Удаляем pending из Redis
    try:
        import redis
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        r = redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        r.delete(pending_key)
    except Exception:
        pass  # best effort

    await cb.message.edit_text("❌ Отменено пользователем.")
    await cb.answer("Отменено")


@router.callback_query(F.data.startswith("trade:signal:"))
async def cb_trade_signal(cb: CallbackQuery) -> None:
    """Обработчик кнопки 'Исполнить' на сигнале (broadcast)."""
    settings = Settings.load()
    chat_id = str(cb.from_user.id)
    logger.info("trade_callback", data=cb.data, chat_id=chat_id)

    # Guard: только админ
    if not _is_admin(chat_id, settings):
        logger.warning("trade_rejected", reason="not_admin")
        await cb.answer("❌ Только администратор может исполнять сделки", show_alert=True)
        return

    # Guard: sandbox-only
    if settings.trade_mode != "sandbox":
        logger.warning("trade_rejected", reason="trade_error")
        await cb.answer("❌ Trading disabled: trade_mode != sandbox", show_alert=True)
        return

    # Parse callback: trade:signal:{ticker}:{bar_date}
    parts = cb.data.split(":")
    if len(parts) < 4:
        await cb.answer("❌ Неверный формат callback", show_alert=True)
        return
    
    ticker = parts[2]
    bar_date = parts[3]
    
    # Формируем ключ pending по сигналу: trade:pending:{chat_id}:{ticker}:{bar_date}
    signal_key = f"{ticker}:{bar_date}"
    pending_key = f"trade:pending:{chat_id}:{signal_key}"

    # Получаем pending из Redis
    try:
        import redis
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        r = redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        pending_json = r.get(pending_key)
        if not pending_json:
            logger.warning("trade_rejected", reason="expired")
            await cb.answer("❌ Запрос истёк (60с). Сигнал больше не актуален.", show_alert=True)
            await cb.message.edit_text("❌ Время подтверждения истекло (60с).")
            return
        order = json.loads(pending_json)
        # Удаляем pending после получения
        r.delete(pending_key)
    except Exception as exc:
        log.warning("Redis недоступен при исполнении сигнала: %s", exc)
        await cb.answer("⚠️ Сервис недоступен (Redis).", show_alert=True)
        return

    # Исполняем сделку через TradeClient (BUY 1 lot по сигналу)
    try:
        client = TradeClient(settings)
        resp = client.place_market_order(
            instrument_id=order["figi"],
            lots=order["lots"],
            direction=order["direction"],
        )
        order_id = resp.get("orderId", "unknown")
        exec_status = resp.get("executionReportStatus", "unknown")
        # Цена исполнения может быть в ответе
        price_info = ""
        if "initialOrderPrice" in resp:
            p = resp["initialOrderPrice"]
            price_info = f" по {p.get('units', '')}.{p.get('nano', 0):09d} ₽"
        elif "averagePrice" in resp:
            p = resp["averagePrice"]
            price_info = f" по {p.get('units', '')}.{p.get('nano', 0):09d} ₽"

        text = (
            f"✅ Исполнено по сигналу: {order['direction'].upper()} {order['lots']} {order['ticker']}\n"
            f"Order ID: {order_id}\n"
            f"Статус: {exec_status}{price_info}"
        )
        await cb.message.edit_text(text, parse_mode=ParseMode.HTML)
        await cb.answer("Исполнено")
        logger.info(
            "trade_executed",
            order_id=order_id,
            ticker=order["ticker"],
            lots=order["lots"],
            status=exec_status,
        )
    except TradeError as exc:
        logger.warning("trade_rejected", reason="trade_error", code=exc.code)
        text = f"❌ Ошибка торговли: code={exc.code}, {exc.message}"
        await cb.message.edit_text(text, parse_mode=ParseMode.HTML)
        await cb.answer(f"Ошибка: {exc.code}", show_alert=True)
    except Exception as exc:
        logger.warning("trade_rejected", reason="trade_error", error=str(exc))
        log.exception("Ошибка исполнения сделки по сигналу")
        await cb.message.edit_text(f"❌ Ошибка: {exc}")
        await cb.answer("Ошибка", show_alert=True)


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

    # Прокси-сессия для aiogram (SOCKS — через aiohttp-socks, extra aiogram[proxy]).
    # python_socks не понимает схему socks5h — нормализуем до socks5.
    from aiogram.client.session.aiohttp import AiohttpSession
    proxy_url = settings.telegram_proxy.replace("socks5h://", "socks5://")
    bot.session = AiohttpSession(proxy=proxy_url)

    print("🤖 Bot starting (long polling)...")
    dp.run_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    main()