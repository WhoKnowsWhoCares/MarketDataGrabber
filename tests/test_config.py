"""Тесты для config.py."""
import os

import pytest

from mdgrabber.config import Settings


def test_missing_env_raises(monkeypatch, tmp_path):
    for k in ["T_INVEST_READ_TOKEN", "TELEGRAM_BOT_TOKEN", "ADMIN_CHAT_ID",
              "CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD"]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("mdgrabber.config.REPO_ROOT", tmp_path)
    with pytest.raises(RuntimeError, match="Не заданы"):
        Settings.load()


def test_masked_has_no_full_token(monkeypatch, tmp_path):
    monkeypatch.setattr("mdgrabber.config.REPO_ROOT", tmp_path)
    monkeypatch.setenv("T_INVEST_READ_TOKEN", "verylongsecrettoken123456")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ADMIN_CHAT_ID", "123")
    monkeypatch.setenv("CLICKHOUSE_USER", "default")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "pw")
    s = Settings.load()
    m = s.masked()
    assert "verylongsecrettoken123456" not in str(m)
    assert m["t_invest_read_token"].endswith("***")
