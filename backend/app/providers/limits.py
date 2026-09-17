"""Concurrency and rate bounds for provider calls.
"""

from __future__ import annotations
import asyncio
import time
from collections import deque
from types import TracebackType


class TokenBucketLimiter:
    """Sliding-window rate limiter over the last 60 seconds.
    """

    def __init__(self, *, rate_per_minute: int, name: str = "limiter") -> None:
        if rate_per_minute < 1:
            raise ValueError("rate_per_minute must be >= 1")
        self._rate = rate_per_minute
        self._name = name
        self._events: deque[float] = deque()
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self._name

    async def acquire(self) -> float:
        """Block until a slot is free. Returns seconds spent waiting."""
        waited = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                cutoff = now - 60.0
                while self._events and self._events[0] <= cutoff:
                    self._events.popleft()

                if len(self._events) < self._rate:
                    self._events.append(now)
                    return waited

                sleep_for = self._events[0] - cutoff

            sleep_for = max(sleep_for, 0.05)
            await asyncio.sleep(sleep_for)
            waited += sleep_for


class ConcurrencyGate:
    """Bounded concurrency with a limiter, as one reusable async context."""

    def __init__(self, *, limiter: TokenBucketLimiter, max_concurrent: int) -> None:
        self._limiter = limiter
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self.last_wait_s = 0.0

    async def __aenter__(self) -> ConcurrencyGate:
        await self._semaphore.acquire()
        try:
            self.last_wait_s = await self._limiter.acquire()
        except BaseException:
            self._semaphore.release()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._semaphore.release()
