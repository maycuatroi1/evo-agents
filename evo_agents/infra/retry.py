"""When to try a failing call again, and how long to wait first (docs/structure.md, section Shared infrastructure).

A ``RetryPolicy`` holds three strategies: how many tries (``tries``, None for no end), the delay before each new try
(a ``Delays``: ``Exponential`` doubles to a ceiling with jitter, ``Steps`` walks a fixed list), and which errors are
worth another try (``retry_on``: exception types, or a predicate). ``call`` and ``acall`` run a callable under it;
``Backoff`` counts the failures of a loop that runs itself and hands out the next delay.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

T = TypeVar("T")


class Delays(Protocol):
    def __call__(self, failures: int) -> float:
        """The seconds to wait after ``failures`` failures in a row (0 for the first)."""
        ...


@dataclass(frozen=True)
class Exponential:
    """``first`` seconds, doubling at each failure up to ``most``, each within ``jitter`` (a fraction) of that."""

    first: float
    most: float
    jitter: float = 0.0
    uniform: Callable[[float, float], float] = random.uniform

    def __call__(self, failures: int) -> float:
        # the exponent stops at 32: 2**1024 does not fit a float, and seven hours of failed heartbeats reached it
        delay = min(self.most, self.first * 2.0 ** min(failures, 32))
        if not self.jitter:
            return delay
        return max(0.0, delay * (1 + self.uniform(-self.jitter, self.jitter)))


@dataclass(frozen=True)
class Steps:
    """The ``failures``-th of ``delays``, then ``after`` once they run out (the last of them when None)."""

    delays: Sequence[float]
    after: float | None = None

    def __call__(self, failures: int) -> float:
        if failures < len(self.delays):
            return self.delays[failures]
        return self.after if self.after is not None else (self.delays[-1] if self.delays else 0.0)


@dataclass(frozen=True)
class Fixed:
    seconds: float

    def __call__(self, failures: int) -> float:
        return self.seconds


@dataclass(frozen=True)
class RetryPolicy:
    """Try up to ``tries`` times (None: until it works), waiting ``delays(n)`` after the n-th failure, again only on an
    error ``retry_on`` takes; any other error, or the last try's, raises as it is."""

    delays: Delays = field(default_factory=lambda: Fixed(0.0))
    tries: int | None = 3
    retry_on: tuple[type[BaseException], ...] | Callable[[BaseException], bool] = (Exception,)
    # the seconds an error asks to wait itself (an HTTP Retry-After), or None to wait what ``delays`` says
    asked: Callable[[BaseException], float | None] | None = None

    def delay(self, failures: int, error: BaseException | None = None) -> float:
        """The seconds to wait after ``failures`` failures, the last of them ``error``."""
        wanted = self.asked(error) if self.asked is not None and error is not None else None
        return self.delays(failures) if wanted is None else wanted

    def retries(self, error: BaseException, failures: int) -> bool:
        """Whether a call that failed ``failures`` times now (this one included) with ``error`` goes again."""
        if self.tries is not None and failures >= self.tries:
            return False
        if isinstance(self.retry_on, tuple):
            return isinstance(error, self.retry_on)
        return self.retry_on(error)

    def call(
        self,
        fn: Callable[[], T],
        *,
        sleep: Callable[[float], object] | None = None,
        retrying: Callable[[BaseException, float], object] | None = None,
    ) -> T:
        """``fn()`` under the policy, waiting with ``sleep`` (time.sleep, looked up at each wait) between tries;
        ``retrying(error, seconds)`` is told of each wait before it."""
        failures = 0
        while True:
            try:
                return fn()
            except BaseException as error:
                failures += 1
                if not self.retries(error, failures):
                    raise
                seconds = self.delay(failures - 1, error)
                if retrying is not None:
                    retrying(error, seconds)
                (sleep or time.sleep)(seconds)

    async def acall(
        self,
        fn: Callable[[], Awaitable[T]],
        *,
        sleep: Callable[[float], Awaitable[object]] | None = None,
        retrying: Callable[[BaseException, float], object] | None = None,
    ) -> T:
        """``await fn()`` under the policy, as ``call``, with asyncio.sleep; a cancellation is never retried."""
        failures = 0
        while True:
            try:
                return await fn()
            except BaseException as error:
                if isinstance(error, asyncio.CancelledError):
                    raise
                failures += 1
                if not self.retries(error, failures):
                    raise
                seconds = self.delay(failures - 1, error)
                if retrying is not None:
                    retrying(error, seconds)
                await (sleep or asyncio.sleep)(seconds)


class Backoff:
    """The delays of a loop that tries a failing call again itself: ``next()`` after each failure, ``reset()`` once
    it works."""

    def __init__(self, delays: Delays):
        self.delays = delays
        self.failures = 0

    def next(self) -> float:
        delay = self.delays(self.failures)
        self.failures += 1
        return delay

    def reset(self) -> None:
        self.failures = 0
