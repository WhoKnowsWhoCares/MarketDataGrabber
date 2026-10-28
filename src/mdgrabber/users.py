"""Управление пользователями и подписками (SQLite).

Таблицы:
  users            — chat_id → user_id, флаг админа
  subscriptions    — фильтры сигналов по пользователю

При init_db() сидируется запись админа из Settings.ADMIN_CHAT_ID.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional

from .config import REPO_ROOT, Settings

log = logging.getLogger(__name__)

DB_PATH = REPO_ROOT / "data" / "bot.sqlite"


def _connect() -> sqlite3.Connection:
    """Открыть соединение с SQLite."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(settings: Optional[Settings] = None) -> None:
    """Создать таблицы и сидировать админа."""
    conn = _connect()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id   INTEGER PRIMARY KEY,
                chat_id   TEXT    NOT NULL UNIQUE,
                is_admin  INTEGER NOT NULL DEFAULT 0,
                created_at TEXT   NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id        INTEGER NOT NULL REFERENCES users(user_id),
                instrument     TEXT    NOT NULL DEFAULT '*',
                signal_type    TEXT    NOT NULL DEFAULT '*',
                min_importance INTEGER NOT NULL DEFAULT 0,
                quiet_hours    TEXT    NOT NULL DEFAULT '',
                UNIQUE(user_id, instrument)
            );
        """)
        # Сидирование админа из .env
        if settings is not None:
            admin_chat = str(settings.admin_chat_id)
            conn.execute(
                "INSERT OR IGNORE INTO users (user_id, chat_id, is_admin) "
                "VALUES (1, ?, 1)",
                (admin_chat,),
            )
            conn.commit()
    finally:
        conn.close()


def get_user(chat_id: str) -> Optional[dict]:
    """Вернуть dict пользователя по chat_id или None."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT user_id, chat_id, is_admin, created_at FROM users WHERE chat_id = ?",
            (str(chat_id),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def add_user(chat_id: str, is_admin: bool = False) -> int:
    """Зарегистрировать пользователя. user_id = hash(chat_id)."""
    uid = hash(chat_id) % (10 ** 12)
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, chat_id, is_admin) "
            "VALUES (?, ?, ?)",
            (uid, str(chat_id), 1 if is_admin else 0),
        )
        conn.commit()
        return uid
    finally:
        conn.close()


def list_subscribers(category: str = "market") -> list[str]:
    """chat_id всех пользователей с подпиской на категорию.

    category='market' → все активные пользователи (пока нет per-category в БД,
    возвращаем всех). В будущем можно расширить через subscriptions.signal_type.
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT chat_id FROM users WHERE is_admin = 0"
        ).fetchall()
        return [r["chat_id"] for r in rows]
    finally:
        conn.close()


def add_subscription(chat_id: str, instrument: str, signal_type: str) -> None:
    """Добавить/обновить подписку (upsert)."""
    user = get_user(str(chat_id))
    if user is None:
        uid = add_user(str(chat_id))
    else:
        uid = user["user_id"]
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO subscriptions (user_id, instrument, signal_type) "
            "VALUES (?, ?, ?)",
            (uid, instrument.upper(), signal_type),
        )
        conn.commit()
    finally:
        conn.close()


def remove_subscription(chat_id: str, instrument: Optional[str] = None) -> int:
    """Удалить подписку. Если instrument=None — все подписки пользователя."""
    user = get_user(str(chat_id))
    if user is None:
        return 0
    conn = _connect()
    try:
        if instrument:
            cur = conn.execute(
                "DELETE FROM subscriptions WHERE user_id = ? AND instrument = ?",
                (user["user_id"], instrument.upper()),
            )
        else:
            cur = conn.execute(
                "DELETE FROM subscriptions WHERE user_id = ?",
                (user["user_id"],),
            )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
