"""Тесты для ratelimit.py (mock Redis)."""
from unittest.mock import MagicMock, patch

from mdgrabber.ratelimit import allow


def test_allow_with_redis():
    """При рабочем Redis — INCR возвращает 1 → разрешаем."""
    mock_r = MagicMock()
    mock_r.incr.return_value = 1

    with patch("mdgrabber.ratelimit._redis_module") as mock_redis:
        mock_redis.from_url.return_value = mock_r
        result = allow("test_key", ttl_seconds=10, redis_url="redis://localhost:6379/0")
    assert result is True
    mock_r.incr.assert_called_once_with("test_key")
    mock_r.expire.assert_called_once_with("test_key", 10)


def test_allow_redis_blocked():
    """INCR > 1 → cooldown активен, но функция всё равно возвращает True."""
    mock_r = MagicMock()
    mock_r.incr.return_value = 2

    with patch("mdgrabber.ratelimit._redis_module") as mock_redis:
        mock_redis.from_url.return_value = mock_r
        result = allow("test_key", ttl_seconds=10)
    assert result is True


def test_allow_redis_unavailable():
    """Redis недоступен → возвращаем True и логируем warning."""
    with patch("mdgrabber.ratelimit._redis_module") as mock_redis:
        mock_redis.from_url.side_effect = ConnectionError("refused")
        result = allow("test_key", ttl_seconds=10)
    assert result is True
