"""Тонкая обёртка над clickhouse-connect."""
from __future__ import annotations

from typing import Optional

import clickhouse_connect

from .config import Settings


def get_client(
    settings: Settings,
    user: Optional[str] = None,
    password: Optional[str] = None,
):
    """Клиент к ClickHouse. user/password переопределяют настройки из .env
    (например, MCP-сервер ходит под read-only mdg_ro)."""
    return clickhouse_connect.get_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_http_port,
        username=user if user is not None else settings.clickhouse_user,
        password=password if password is not None else settings.clickhouse_password,
    )
