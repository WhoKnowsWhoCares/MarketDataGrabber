"""FastMCP-сервер «marketdata»: 5 read-only инструментов над gold-витринами.

Регистрация инструментов (функции — в mcp_tools). Запуск:
    PYTHONPATH=src python -m mdgrabber.mcp_server
endpoint: /mcp (streamable-http), аутентификация — X-API-Key через
mcp_asgi.ApiKeyAuthMiddleware.
"""
from __future__ import annotations

import os

from . import mcp_tools

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "Пакет mcp не установлен: pip install 'mcp>=1.2.0,<2'"
    ) from e

mcp = FastMCP(
    "marketdata",
    host="0.0.0.0",
    port=int(os.environ.get("MCP_PORT", "8900")),
)


@mcp.tool()
def list_instruments() -> list[dict]:
    """Список инструментов юниверса: тикер, uid, figi, asset_uid."""
    return mcp_tools.list_instruments(mcp_tools.get_ro_client())


@mcp.tool()
def get_daily_metrics(ticker: str, days: int = 30) -> list[dict]:
    """Последние N дневных строк по тикеру: close, ret_1d, volume, volume_ratio, rsi14, atr14."""
    return mcp_tools.get_daily_metrics(mcp_tools.get_ro_client(), ticker, days)


@mcp.tool()
def get_latest_status(ticker: str) -> dict:
    """Последний статус по тикеру: close, ret_1d/5d/20d, rsi14, volume_ratio, gap_pct, положение к MA20/MA50."""
    return mcp_tools.get_latest_status(mcp_tools.get_ro_client(), ticker)


@mcp.tool()
def get_events(ticker: str | None = None) -> list[dict]:
    """Ближайшие события (дивиденды/отчёты/экспирации) с today() вперёд; фильтр по тикеру опционален."""
    return mcp_tools.get_events(mcp_tools.get_ro_client(), ticker)


@mcp.tool()
def get_fundamentals(ticker: str) -> dict:
    """Последний фундаментал-снапшот: peRatioTtm, priceToBookTtm, forwardAnnualDividendYield."""
    return mcp_tools.get_fundamentals(mcp_tools.get_ro_client(), ticker)


def main() -> None:
    """uvicorn поверх auth-обёртки streamable_http_app (X-API-Key)."""
    import uvicorn

    from .config import Settings
    from .mcp_asgi import create_app

    settings = Settings.load()
    app = create_app()
    uvicorn.run(
        app,
        host=settings.mcp_bind_ip,
        port=settings.mcp_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
