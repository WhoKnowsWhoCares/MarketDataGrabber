#!/usr/bin/env python3
"""Рантайм сигналов (P4.2): detect → persist → Telegram-рассылка + Grafana-аннотации.

Конвейер:
    1. detect_signals по gold-витрине;
    2. persist_new — дедуп по (instrument_uid, signal_type, bar_date) и вставка;
    3. для вставленных:
       а) Telegram market-рассылка подписчикам (по их subscriptions), каждому —
          ОДНО сообщение со всеми его сигналами; подписчиков нет → админу
          с пометкой "(нет подписчиков)";
       б) Grafana-аннотация на каждый сигнал (ошибка не валит рантайм);
       в) mark_sent.
Идемпотентен: повторный запуск — "no new signals" (persist_new дедуплицирует).

Использование:
    set -a; source .env; set +a
    PYTHONPATH=src .venv/bin/python scripts/run_signals.py [--dry-run] [--force]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from mdgrabber.ch import get_client  # noqa: E402
from mdgrabber.config import Settings  # noqa: E402
from mdgrabber.logging import get_logger, setup_logging  # noqa: E402
from mdgrabber.notify import send_telegram  # noqa: E402
from mdgrabber.signals import (  # noqa: E402
    calibration,
    detect_signals,
    mark_sent,
    persist_new,
    fetch_unsent,
)
from mdgrabber.users import init_db, list_subscribers, list_subscriptions  # noqa: E402

log = get_logger("run_signals")

ETL_RUNS_TABLE = "market_data_aux.etl_runs"
GRAFANA_URL = os.getenv("GRAFANA_URL", "http://172.18.0.1:3000")


def load_instruments(path: Path) -> dict[str, str]:
    """config/instruments.json {ticker: {uid, ...}} -> {uid: ticker}."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return {v["uid"]: k for k, v in data.items() if v.get("uid")}


def subscription_matches(sub: dict, signal: dict) -> bool:
    """Совпадение подписки с сигналом: '*' или точное значение с обеих сторон."""
    instrument = str(sub["instrument"]).upper()
    return (
        instrument in ("*", str(signal["ticker"]).upper())
        and str(sub["signal_type"]) in ("*", signal["signal_type"])
    )


def filter_for_subscriber(subs: list[dict], signals: list[dict]) -> list[dict]:
    """Сигналы, подходящие хотя бы под одну подписку подписчика."""
    return [s for s in signals if any(subscription_matches(sub, s) for sub in subs)]


def filter_calibrated(signals: list[dict]) -> tuple[list[dict], int]:
    """Калибровочный фильтр (P4.3): только сигналы типов с deliver=True.

    Возвращает (доставляемые, сколько отфильтровано). Детекция по-прежнему
    сохраняет все 7 типов в signals — фильтр стоит только на доставке.
    """
    deliverable = [s for s in signals if calibration(s["signal_type"])["deliver"]]
    return deliverable, len(signals) - len(deliverable)


def format_message(signals: list[dict], settings: Settings | None = None) -> str:
    """HTML-сообщение со списком сигналов, <b>тикер</b> в заголовках."""
    lines = [f"🔔 <b>Новые сигналы</b> ({len(signals)}):"]
    for s in signals:
        lines.append(f"• <b>{s['ticker']}</b> [{s['signal_type']}] {s['bar_date']}: {s['message']}")
    return "\n".join(lines)


def format_message_with_trade_buttons(signals: list[dict], settings: Settings) -> tuple[str, dict | None]:
    """HTML-сообщение с inline-кнопками исполнения для bullish-сигналов.
    
    Возвращает (текст, reply_markup_dict или None).
    Кнопки только для ma_cross_up и rsi_oversold, если SHOW_TRADE_BUTTONS=1.
    """
    if not getattr(settings, "show_trade_buttons", False):
        return format_message(signals), None
    
    # Bullish signals that get trade buttons
    bullish_types = {"ma_cross_up", "rsi_oversold"}
    
    lines = [f"🔔 <b>Новые сигналы</b> ({len(signals)}):"]
    keyboard_rows = []
    
    for s in signals:
        lines.append(f"• <b>{s['ticker']}</b> [{s['signal_type']}] {s['bar_date']}: {s['message']}")
        
        # Добавляем кнопку для bullish-сигналов
        if s["signal_type"] in bullish_types:
            # callback_data: trade:signal:{ticker}:{bar_date}
            bar_date = str(s["bar_date"])
            callback_id = f"{s['ticker']}:{bar_date}"
            keyboard_rows.append([{
                "text": f"⚡ Исполнить: BUY 1 {s['ticker']} (SANDBOX)",
                "callback_data": f"trade:signal:{callback_id}"
            }])
    
    text = "\n".join(lines)
    
    if keyboard_rows:
        reply_markup = {
            "inline_keyboard": keyboard_rows
        }
        return text, reply_markup
    return text, None


def annotation_payload(signal: dict) -> dict:
    """Payload для POST /api/annotations: time = bar_date в ms UTC."""
    d = signal["bar_date"]
    if isinstance(d, str):
        dt = datetime.strptime(d, "%Y-%m-%d")
    else:
        dt = datetime(d.year, d.month, d.day)
    return {
        "time": int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000),
        "tags": ["mdg", "signal", signal["signal_type"], signal["ticker"]],
        "text": signal["message"],
    }


def post_annotation(signal: dict) -> None:
    """Grafana-аннотация (basic auth админа из .env)."""
    user = os.environ["GRAFANA_ADMIN_USER"]
    password = os.environ["GRAFANA_ADMIN_PASSWORD"]
    with httpx.Client(timeout=10.0) as http:
        r = http.post(
            f"{GRAFANA_URL}/api/annotations",
            json=annotation_payload(signal),
            auth=(user, password),
        )
        r.raise_for_status()


def broadcast(settings: Settings, signals: list[dict], dry_run: bool = False) -> list[tuple[str, list[dict]]]:
    """План рассылки (chat_id → сигналы) и (если не dry-run) сама рассылка.

    Подписчиков нет → один market-получатель: админ, текст с пометкой
    "(нет подписчиков)".
    """
    # Создаем pending в Redis для сигналов с кнопками (если не dry-run)
    if not dry_run and getattr(settings, "show_trade_buttons", False):
        _create_signal_pending(settings, signals)

    subscribers = list_subscribers()
    if subscribers:
        plan = [
            (chat_id, matching)
            for chat_id in subscribers
            if (matching := filter_for_subscriber(list_subscriptions(chat_id), signals))
        ]
        suffix = ""
    else:
        plan = [(str(settings.admin_chat_id), signals)]
        suffix = " (нет подписчиков)"

    if dry_run:
        for chat_id, msgs in plan:
            log.info(
                "dry_run_would_send",
                chat_id=chat_id,
                signals=[f"{m['ticker']}:{m['signal_type']}" for m in msgs],
            )
        return plan

    for chat_id, msgs in plan:
        text, reply_markup = format_message_with_trade_buttons(msgs, settings)
        text = text + suffix
        send_telegram(
            settings,
            text=text,
            category="market",
            chat_id=chat_id,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
        log.info("sent", chat_id=chat_id, signals=len(msgs))
    return plan


def _create_signal_pending(settings: Settings, signals: list[dict]) -> None:
    """Создает pending-записи в Redis для сигналов с кнопками исполнения.
    
    Ключ: trade:pending:{chat_id}:{ticker}:{bar_date}
    TTL: 60 секунд.
    """
    bullish_types = {"ma_cross_up", "rsi_oversold"}
    signals_with_buttons = [s for s in signals if s["signal_type"] in bullish_types]
    if not signals_with_buttons:
        return

    # Получаем FIGI для каждого тикера
    instruments_path = Path(__file__).resolve().parents[1] / "config" / "instruments.json"
    try:
        instruments_data = json.loads(instruments_path.read_text(encoding="utf-8"))
    except Exception:
        log.warning("Не удалось прочитать instruments.json для pending")
        return

    # Получаем admin chat_id (для случая если нет подписчиков)
    admin_chat_id = str(settings.admin_chat_id)

    try:
        import redis
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        r = redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        
        # Для каждого чата (подписчика или админа) создаем pending
        subscribers = list_subscribers()
        chat_ids = subscribers if subscribers else [admin_chat_id]
        
        for chat_id in chat_ids:
            for s in signals_with_buttons:
                ticker = s["ticker"]
                bar_date = str(s["bar_date"])
                
                info = instruments_data.get(ticker.upper())
                if not info:
                    continue
                figi = info.get("figi") or info.get("uid")
                if not figi:
                    continue
                
                signal_key = f"{ticker}:{bar_date}"
                pending_key = f"trade:pending:{chat_id}:{signal_key}"
                order_data = {
                    "figi": figi,
                    "ticker": ticker,
                    "direction": "buy",  # сигналы всегда BUY
                    "lots": 1,
                    "chat_id": chat_id,
                }
                r.setex(pending_key, 60, json.dumps(order_data))
    except Exception as exc:
        log.warning("Redis недоступен, pending для сигналов не создан: %s", exc)


def record_run(client, status: str, rows: int, started_at: datetime) -> None:
    """Запись о прогоне в market_data_aux.etl_runs (job='signals')."""
    finished = datetime.now(timezone.utc).replace(microsecond=0)
    start_str = started_at.strftime("%Y-%m-%d %H:%M:%S")
    finish_str = finished.strftime("%Y-%m-%d %H:%M:%S")
    try:
        client.command(
            f"""
            INSERT INTO {ETL_RUNS_TABLE}
                (transform, date_from, date_to, rows_in, rows_out, errors,
                 status, started_at, finished_at, code_version)
            VALUES
                ('signals', NULL, NULL, 0, {int(rows)}, 0,
                 '{status}', '{start_str}', '{finish_str}', '')
            """
        )
    except Exception as exc:  # noqa: BLE001 — запись телеметрии не валит рантайм
        log.warning("etl_run_record_failed", error=str(exc))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Рантайм сигналов: Telegram + Grafana")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="detect+persist, но НЕ слать и НЕ аннотировать — печатает план",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="игнорировать дедуп (разослать все детектированные сигналы)",
    )
    args = parser.parse_args(argv)

    setup_logging(os.getenv("LOG_LEVEL", "INFO"))
    started_at = datetime.now(timezone.utc).replace(microsecond=0)

    settings = Settings.load()
    client = get_client(settings)
    init_db(settings)  # идемпотентно: таблицы users/subscriptions + сид админа

    instruments = load_instruments(ROOT / "config" / "instruments.json")
    signals = detect_signals(client, instruments)

    if args.force:
        persist_new(client, signals)  # новые сохраняем, дедуп рассылки игнорируем
        fresh = signals
    else:
        persist_new(client, signals)
        # доставляем всё, что сохранено, но ещё не доставлено (sent=0):
        # покрывает и свежие сигналы, и «застрявшие» после dry-run/сбоя доставки
        fresh = fetch_unsent(client)

    if not fresh:
        log.info("no new signals", detected=len(signals))
        record_run(client, "ok", 0, started_at)
        return 0

    log.info("new_signals", count=len(fresh), dry_run=args.dry_run)
    deliverable, filtered = filter_calibrated(fresh)
    if filtered:
        log.info("filtered_by_calibration", filtered=filtered, kept=len(deliverable))

    if not deliverable:
        log.info("no deliverable signals after calibration",
                 detected=len(signals), filtered=filtered)
        record_run(client, "ok", 0, started_at)
        return 0

    broadcast(settings, deliverable, dry_run=args.dry_run)

    if not args.dry_run:
        for s in deliverable:
            try:
                post_annotation(s)
            except Exception as exc:  # noqa: BLE001 — аннотация не валит рантайм
                log.warning(
                    "annotation_failed",
                    ticker=s["ticker"],
                    signal_type=s["signal_type"],
                    error=str(exc),
                )
        mark_sent(client, deliverable)

    record_run(client, "ok", len(deliverable), started_at)
    log.info("run_finished", delivered=len(deliverable),
             filtered_by_calibration=filtered, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())