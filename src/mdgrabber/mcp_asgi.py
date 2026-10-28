"""ASGI-обёртка аутентификации для FastMCP streamable-http.

FastMCP не проверяет HTTP-заголовки сам: эта мини-обёртка читает X-API-Key,
проверяет его через verify_key (mcp_keys) и кладёт в contextvar api_key_var,
из которого инструменты берут ключ. Невалидный/отсутствующий ключ -> 401 JSON.

Ключи не логируются (verify_key пишет в логи только маску SHA256-хэша).
"""
from __future__ import annotations

import contextvars
import json
import logging

from .mcp_auth import verify_key
from .mcp_tools import api_key_var

logger = logging.getLogger(__name__)

_401_BODY = json.dumps({"error": "invalid api key"}).encode()


def check_key(key: str, client=None) -> bool:
    """Проверка ключа (отдельная функция — чтобы тесты могли мокать)."""
    return verify_key(key, client=client) is not None


class ApiKeyAuthMiddleware:
    """Pure-ASGI middleware вокруг mcp.streamable_http_app()."""

    def __init__(self, app, client_provider=None):
        self.app = app
        # client_provider: вызывается лениво при первом запросе — на момент
        # импорта модуля Settings ещё могут быть не готовы.
        self._client_provider = client_provider

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        key = headers.get(b"x-api-key", b"").decode("utf-8", "replace")
        api_key_var.set(key)

        client = self._client_provider() if self._client_provider else None
        if not key or not check_key(key, client=client):
            logger.warning("mcp_asgi: request rejected (missing/invalid key)")
            await self._reject(scope, receive, send)
            return

        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_401_BODY)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _401_BODY})


def create_app(client_provider=None):
    """Собрать итоговое ASGI-приложение: auth-middleware поверх streamable-http."""
    import os

    from .mcp_server import mcp

    inner = mcp.streamable_http_app()
    return ApiKeyAuthMiddleware(inner, client_provider=client_provider)


# re-export для единых импортов
api_key: contextvars.ContextVar[str] = api_key_var
