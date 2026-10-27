"""JSON-логгер (structlog) в stdout. Секреты маскируются."""
from __future__ import annotations

import logging
import sys

import structlog

_SECRET_SUBSTRINGS = ("token", "password", "secret", "authorization", "key")


def _mask_secrets(
    logger: object, method_name: str, event_dict: dict
) -> dict:
    for k in list(event_dict):
        if any(s in k.lower() for s in _SECRET_SUBSTRINGS):
            v = str(event_dict[k])
            event_dict[k] = (v[:6] + "***") if len(v) > 6 else "***"
    return event_dict


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _mask_secrets,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
    )


def get_logger(name: str):
    return structlog.get_logger(name)
