"""Тонкая обёртка над clickhouse-connect."""
from __future__ import annotations

import clickhouse_connect

from .config import Settings


def get_client(settings: Settings):
    return clickhouse_connect.get_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_http_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
    )
