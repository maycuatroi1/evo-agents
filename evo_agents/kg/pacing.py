"""Pacing of a long CPU-bound build run in a thread of a process whose event loop serves requests: the api making a
graph's snapshot (``evo_agents.kg.snapshot.Snapshot.from_stored``) beside the requests of agents and workers.

A thread that never blocks keeps the GIL until the interpreter forces a switch, once per ``sys.getswitchinterval()``
(5 ms). The event loop gives the GIL up at every wait on its sockets and then waits up to that interval to get it
back, so each step of a request costs 5 ms, and 200 ms on the busy host of the hub, where a thread takes long to
wake: there, the minute or two a large graph's snapshot took to make held every pooled connection in a transaction long
enough that the workers' heartbeats and claims timed out waiting for one. ``paced`` gives the GIL up of its own will
for ``PAUSE_SECONDS`` every ``YIELD_SECONDS`` of the loop it wraps, so the event loop runs whole turns while the build
goes on, which takes a third longer. It paces only inside ``pacing()``, which the api's loads enter: a build with no
event loop beside it (``kg build``, the bench) runs at full speed."""

from __future__ import annotations

import heapq
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

T = TypeVar("T")

YIELD_SECONDS = 0.002  # the longest stretch ``paced`` runs before it gives the GIL up
# how long it sleeps then: ``time.sleep(0)`` takes the GIL back before a waiting thread wakes, and on a busy host the
# loop's thread takes a while to wake; a third of the build's time spent asleep kept the hub's loop turning every 10 ms
PAUSE_SECONDS = 0.001
RUN = 8192  # items ``paced_sorted`` sorts in one go

_on: ContextVar[bool] = ContextVar(
    "evo_kg_paced", default=False
)  # within ``pacing()``; a thread of ``asyncio.to_thread`` gets it


@contextmanager
def pacing():
    """``paced`` and ``paced_sorted`` give the GIL up while the block runs, in the threads it starts too."""
    token = _on.set(True)
    try:
        yield
    finally:
        _on.reset(token)


def paced(items: Iterable[T]) -> Iterator[T]:
    """``items``, the GIL given up for ``PAUSE_SECONDS`` whenever ``YIELD_SECONDS`` went by since the last time;
    ``items`` themselves outside ``pacing()``."""
    if not _on.get():
        return iter(items)
    return _paced(items)


def _paced(items: Iterable[T]) -> Iterator[T]:
    clock = time.perf_counter
    last = clock()
    for item in items:
        yield item
        now = clock()
        if now - last >= YIELD_SECONDS:
            time.sleep(PAUSE_SECONDS)
            last = clock()


def paced_sorted(items: Iterable[T], key: Callable[[T], Any] | None = None) -> list[T]:
    """``sorted(items, key=key)``, the GIL given up as it goes: one sort holds it from start to end, seconds for the
    hundreds of thousands of edges of a large graph, so runs of ``RUN`` items are sorted one at a time and merged. The
    merge is stable as ``sorted`` is: of two equal items, the one of the earlier run comes first. Outside ``pacing()``,
    one sort."""
    if not _on.get():
        return sorted(items, key=key)
    items = list(paced(items))
    runs = [sorted(items[i : i + RUN], key=key) for i in paced(range(0, len(items), RUN))]
    return list(paced(heapq.merge(*runs, key=key)))
