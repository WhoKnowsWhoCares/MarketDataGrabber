"""Тесты для ratelimit.py (mock Redis)."""
from unittest.mock import MagicMock, patch

from mdgrabber.ratelimit import allow


def test_allow_with_redis():
    """При рабочем Redis ключ свободен (SET NX вернул True) → разрешаем."""
    mock_r = MagicMock()
    mock_r.set.return_value = True

    with patch("mdgrabber.ratelimit._redis_module") as mock_redis:
        mock_redis.from_url.return_value = mock_r
        result = allow("test_key", ttl_seconds=10, redis_url="redis://localhost:6379/0")
    assert result is True
    mock_r.set.assert_called_once_with("test_key", 1, ex=10, nx=True)


def test_allow_redis_blocked():
    """Ключ уже стоит (SET NX вернул None) → cooldown активен, блокируем."""
    mock_r = MagicMock()
    mock_r.set.return_value = None

    with patch("mdgrabber.ratelimit._redis_module") as mock_redis:
        mock_redis.from_url.return_value = mock_r
        result = allow("test_key", ttl_seconds=10)
    assert result is False


def test_allow_redis_unavailable():
    """Redis недоступен → возвращаем True и логируем warning (fail-open)."""
    with patch("mdgrabber.ratelimit._redis_module") as mock_redis:
        mock_redis.from_url.return_value = mock_r = MagicMock()
        mock_r.set.side_effect = ConnectionError("refused")
        result = allow("test_key", ttl_seconds=10)
    assert result is True
