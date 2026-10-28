"""Загрузка конфигурации из .env в корне репо. Секреты в git не попадают."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]

_REQUIRED = [
    "T_INVEST_READ_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "ADMIN_CHAT_ID",
    "CLICKHOUSE_USER",
    "CLICKHOUSE_PASSWORD",
]


@dataclass(frozen=True)
class Settings:
    t_invest_read_token: str
    telegram_bot_token: str
    admin_chat_id: str
    clickhouse_user: str
    clickhouse_password: str
    clickhouse_host: str
    clickhouse_http_port: int
    telegram_proxy: str
    tls_verify: bool
    log_level: str
    mcp_ro_password: str
    mcp_port: int
    mcp_bind_ip: str
    mcp_smoke_key: str

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(REPO_ROOT / ".env")
        missing = [k for k in _REQUIRED if not os.getenv(k)]
        if missing:
            raise RuntimeError(f"Не заданы переменные окружения: {missing} (см. .env.example)")
        return cls(
            t_invest_read_token=os.environ["T_INVEST_READ_TOKEN"],
            telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
            admin_chat_id=os.environ["ADMIN_CHAT_ID"],
            clickhouse_user=os.environ["CLICKHOUSE_USER"],
            clickhouse_password=os.environ["CLICKHOUSE_PASSWORD"],
            clickhouse_host=os.getenv("CLICKHOUSE_HOST", "127.0.0.1"),
            clickhouse_http_port=int(os.getenv("CLICKHOUSE_HTTP_PORT", "8123")),
            # Telegram с этого хоста доступен ТОЛЬКО через SOCKS-прокси docker-шлюза.
            telegram_proxy=os.getenv("TELEGRAM_PROXY", "socks5h://172.21.0.1:10808"),
            # TLS на хосте перехватывается MITM-прокси; CA в системном доверии нет.
            tls_verify=os.getenv("TLS_VERIFY", "false").lower() == "true",
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            # MCP: read-only пароль mdg_ro, порт/бинд сервера, ключ для smoke.
            mcp_ro_password=os.getenv("MCP_RO_PASSWORD", ""),
            mcp_port=int(os.getenv("MCP_PORT", "8900")),
            mcp_bind_ip=os.getenv("MCP_BIND_IP", "0.0.0.0"),
            mcp_smoke_key=os.getenv("MCP_SMOKE_KEY", ""),
        )

    def masked(self) -> dict:
        """Сводка конфига без секретов — безопасно для логов."""
        return {
            "clickhouse": f"{self.clickhouse_user}@{self.clickhouse_host}:{self.clickhouse_http_port}",
            "telegram_proxy": self.telegram_proxy,
            "tls_verify": self.tls_verify,
            "admin_chat_id": self.admin_chat_id,
            "t_invest_read_token": self.t_invest_read_token[:6] + "***",
        }


if __name__ == "__main__":
    print(Settings.load().masked())
