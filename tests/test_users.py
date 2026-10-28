"""Тесты для users.py (SQLite через tmp_path)."""
import os
import tempfile
from pathlib import Path

import pytest

from mdgrabber.users import (
    add_subscription,
    add_user,
    get_user,
    init_db,
    list_subscribers,
    remove_subscription,
)


@pytest.fixture()
def db_dir(tmp_path):
    """Перенаправляем DB_PATH в tmp_path."""
    from mdgrabber import users as u
    old = u.DB_PATH
    u.DB_PATH = tmp_path / "bot.sqlite"
    yield tmp_path
    u.DB_PATH = old


def test_init_creates_tables(db_dir):
    init_db()
    assert db_dir.joinpath("bot.sqlite").exists()


def test_add_and_get_user(db_dir):
    init_db()
    uid = add_user("12345")
    user = get_user("12345")
    assert user is not None
    assert user["chat_id"] == "12345"
    assert user["is_admin"] == 0

    # Неизвестный chat_id → None
    assert get_user("99999") is None


def test_add_user_is_admin(db_dir):
    init_db()
    uid = add_user("123", is_admin=True)
    user = get_user("123")
    assert user["is_admin"] == 1


def test_add_user_idempotent(db_dir):
    init_db()
    uid1 = add_user("12345")
    uid2 = add_user("12345")
    # INSERT OR IGNORE → одинаковый user_id
    assert uid1 == uid2


def test_admin_seeded(db_dir, monkeypatch):
    monkeypatch.setenv("ADMIN_CHAT_ID", "99999")
    from mdgrabber.config import Settings
    init_db(Settings.load())
    admin = get_user("99999")
    assert admin is not None
    assert admin["is_admin"] == 1


def test_list_subscribers(db_dir):
    init_db()
    add_user("100")
    add_user("200", is_admin=True)
    subs = list_subscribers()
    assert "100" in subs
    assert "200" not in subs  # админ исключён


def test_add_subscription(db_dir):
    init_db()
    add_user("123")
    add_subscription("123", "SBER", "*")
    # Проверяем через БД напрямую
    import sqlite3
    conn = sqlite3.connect(str(db_dir.joinpath("bot.sqlite")))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT instrument, signal_type FROM subscriptions WHERE user_id = ?",
        (hash("123") % (10**12),),
    ).fetchone()
    assert row is not None
    assert row["instrument"] == "SBER"
    conn.close()


def test_remove_subscription(db_dir):
    init_db()
    add_user("123")
    add_subscription("123", "SBER", "*")
    removed = remove_subscription("123", "SBER")
    assert removed == 1

    # Ещё раз — ничего не удалено
    removed2 = remove_subscription("123", "SBER")
    assert removed2 == 0


def test_remove_all_subscriptions(db_dir):
    init_db()
    add_user("123")
    add_subscription("123", "SBER", "*")
    add_subscription("123", "GAZP", "buy")
    removed = remove_subscription("123")  # без instrument → все
    assert removed == 2
