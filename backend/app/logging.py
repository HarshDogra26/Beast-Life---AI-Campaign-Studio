"""Structured logging with campaign/stage correlation.
"""

from __future__ import annotations
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import structlog

_campaign_id: ContextVar[str | None] = ContextVar("campaign_id", default=None)
_stage: ContextVar[str | None] = ContextVar("stage", default=None)


def _inject_context(_logger: object, _name: str, event: dict) -> dict:
    if (cid := _campaign_id.get()) is not None:
        event.setdefault("campaign_id", cid)
    if (stage := _stage.get()) is not None:
        event.setdefault("stage", stage)
    return event


def configure_logging(*, level: str = "INFO", json_output: bool = False) -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    renderer = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _inject_context,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


@contextmanager
def log_context(*, campaign_id: str | None = None, stage: str | None = None) -> Iterator[None]:
    """Bind correlation ids for the duration of a block."""
    tokens = []
    if campaign_id is not None:
        tokens.append((_campaign_id, _campaign_id.set(campaign_id)))
    if stage is not None:
        tokens.append((_stage, _stage.set(stage)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
