"""Тесты для logging.py."""
import structlog

from mdgrabber.logging import _mask_secrets, setup_logging


def test_mask():
    d = {"api_token": "verylongsecret123456", "password": "pw", "ok_field": "visible"}
    out = _mask_secrets(None, "info", d)
    assert out["api_token"] == "verylo***"
    assert out["password"] == "***"
    assert out["ok_field"] == "visible"


def test_setup_logging_runs():
    setup_logging("INFO")
    structlog.get_logger("t").info("hello", api_token="abcd")
