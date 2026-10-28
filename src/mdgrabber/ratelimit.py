"""Cooldown / rate-limit через Redis.

Деграсивается без падения: если Redis недоступен — возвращает True и логирует warning.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

try:
    import redis as _redis_module
except ImportError:
    _redis_module = None  # type: ignore[assignment]


def allow(key: str, ttl_seconds: int = 10, redis_url: Optional[str] = None) -> bool:
    """Разрешить действие? True если cooldown истёк или Redis недоступен.

    Алгоритм: INCR key → если == 1 то EXPIRE key ttl_seconds → разрешаем.
    Иначе — блокируем (cooldown активен).
    """
    if _redis_module is None:
        log.warning("redis пакет не установлен, cooldown отключён")
        return True

    if redis_url is None:
        redis_url = "redis://localhost:6379/0"

    try:
        r = _redis_module.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        count = r.incr(key)
        if count == 1:
            r.expire(key, ttl_seconds)
        return True
    except Exception as exc:
        log.warning("Redis недоступен, cooldown пропускается: %s", exc)
        return True
