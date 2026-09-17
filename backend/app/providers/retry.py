"""Retry policy driven by the error taxonomy.
"""

from __future__ import annotations
import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar
from ..logging import get_logger
from .base import ProviderError, RateLimitError

T = TypeVar("T")
log = get_logger(__name__)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, ProviderError):
        return exc.retryable
    return isinstance(exc, (asyncio.TimeoutError, ConnectionError))


async def with_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base_delay_s: float,
    label: str,
    max_delay_s: float = 30.0,
) -> T:
    """Run ``operation`` with exponential backoff and full jitter.
    """
    last_exc: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except asyncio.CancelledError:
            raise
        except BaseException as exc: 
            last_exc = exc
            if not _is_retryable(exc):
                log.warning(
                    "provider.terminal_error",
                    label=label,
                    attempt=attempt,
                    kind=getattr(exc, "kind", type(exc).__name__),
                    error=str(exc)[:300],
                )
                raise
            if attempt == attempts:
                log.error(
                    "provider.retries_exhausted",
                    label=label,
                    attempts=attempts,
                    error=str(exc)[:300],
                )
                raise

            backoff = min(base_delay_s * (2 ** (attempt - 1)), max_delay_s)
            delay = random.uniform(0, backoff)
            if isinstance(exc, RateLimitError) and exc.retry_after_s:
                delay = max(delay, exc.retry_after_s)

            log.warning(
                "provider.retrying",
                label=label,
                attempt=attempt,
                of=attempts,
                delay_s=round(delay, 2),
                kind=getattr(exc, "kind", type(exc).__name__),
            )
            await asyncio.sleep(delay)

    assert last_exc is not None
    raise last_exc
