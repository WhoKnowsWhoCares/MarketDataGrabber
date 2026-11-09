"""Cooldown / rate-limit через Redis.

Деграсивается без падения: если Redis недоступен — возвращает True и логирует warning.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

try:
    import redis as _redis_module
except ImportError:
    _redis_module = None  # type: ignore[assignment]


def allow(key: str, ttl_seconds: int = 10, redis_url: Optional[str] = None) -> bool:
    """Разрешить действие? True если cooldown истёк или Redis недоступен.

    Атомарный SET NX EX: ключ поставился — действие разрешено и занял окно
    cooldown; ключ уже есть — окно активно, блокируем (False).
    """
    if _redis_module is None:
        log.warning("redis пакет не установлен, cooldown отключён")
        return True

    if redis_url is None:
        # compose передаёт REDIS_URL (redis://redis:6379/0); вне docker — localhost
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    try:
        r = _redis_module.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        # SET NX EX — атомарно: True если ключа не было (окно занято нами)
        return bool(r.set(key, 1, ex=ttl_seconds, nx=True))
    except Exception as exc:
        log.warning("Redis недоступен, cooldown пропускается: %s", exc)
        return True
