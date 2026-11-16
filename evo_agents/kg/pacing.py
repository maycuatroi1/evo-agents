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
import queue
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
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
_between: ContextVar[Callable[[], None] | None] = ContextVar("evo_kg_paced_between", default=None)


@contextmanager
def pacing(between: Callable[[], None] | None = None):
    """``paced`` and ``paced_sorted`` give the GIL up while the block runs, in the threads it starts too, and call
    ``between`` each time they took it back."""
    token, step = _on.set(True), _between.set(between)
    try:
        yield
    finally:
        _between.reset(step)
        _on.reset(token)


def paced(items: Iterable[T]) -> Iterator[T]:
    """``items``, the GIL given up for ``PAUSE_SECONDS`` whenever ``YIELD_SECONDS`` went by since the last time;
    ``items`` themselves outside ``pacing()``."""
    if not _on.get():
        return iter(items)
    return _paced(items)


def _paced(items: Iterable[T]) -> Iterator[T]:
    clock, between = time.perf_counter, _between.get()
    last = clock()
    for item in items:
        yield item
        now = clock()
        if now - last >= YIELD_SECONDS:
            time.sleep(PAUSE_SECONDS)
            if between is not None:
                between()
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


CHUNK = 1024  # items ``let_go`` frees at once: a few milliseconds of the GIL
OPEN = 64  # items of a container ``let_go`` opens, not frees at once: such a container's items may be large ones
HAND_OVER = 0.01  # seconds the thread waits before it takes apart what it was given: ``let_go`` returns meanwhile
ALONE = 2  # ``sys.getrefcount`` of an object only a local variable holds: it and the argument of the call

_aside: queue.SimpleQueue | None = None  # what ``let_go`` was given, for the thread ``free_aside`` started


def free_aside() -> None:
    """From now on ``let_go`` frees what it is given in a thread of its own, paced: in the api, where a snapshot
    replaced or dropped is millions of objects, and freeing them in one go held the GIL, the event loop with it, for
    seconds. Once per process."""
    global _aside
    if _aside is None:
        _aside = queue.SimpleQueue()
        threading.Thread(target=_freeing, args=(_aside,), name="kg-free-aside", daemon=True).start()


def let_go(dying: Any) -> None:
    """From the ``__del__`` of ``dying``, an instance nothing holds any more: what it holds goes to the thread
    ``free_aside`` started, which frees it a part at a time; nothing before it ran: it is freed as ``dying`` is."""
    if _aside is not None:
        held = vars(dying)
        parts = list(held.values())
        held.clear()  # the thread is the only holder of what nothing else holds, once this returns
        _aside.put(parts)


def _freeing(given: queue.SimpleQueue) -> None:
    with pacing():
        while True:
            parts = given.get()
            time.sleep(HAND_OVER)
            _take_apart(parts)
            del parts


def _take_apart(objs: list) -> None:
    """Drop ``objs`` at most CHUNK references at a time, pacing in between. What is given, and the attributes of an
    instance opened, are opened too: an instance into its attributes, a container of more than OPEN items into lists of
    CHUNK of them, so dropping it frees none of them, and each list dropped after frees at most CHUNK, each of which is
    small or opened in its turn. Only what
    nothing else holds is opened, and nothing is changed. What another part holds too waits until the rest is taken
    apart, then is opened in its turn, as many rounds as each frees one; what is held from outside after that is left
    to its other holder (a snapshot and a copy of it share their parts: the second one dropped gives them here
    again)."""
    stack: list[tuple[list, bool]] = [(objs, True)]  # (references, whether an instance among them is opened)
    del objs
    opened: set[int] = set()  # what was opened: an object in a cycle is opened once
    x = None
    while stack:
        held: dict[int, Any] = {}  # what another part held too, once each
        for _ in paced(iter(lambda: bool(stack), False)):
            parts, deep = stack.pop()
            while parts:
                x = parts.pop()  # the one before freed, but what it held when opened
                if id(x) in opened:
                    continue
                if sys.getrefcount(x) > ALONE:
                    held[id(x)] = x
                elif isinstance(x, (tuple, list, set, frozenset, dict, MappingProxyType)):
                    if len(x) > OPEN:
                        items = [*x.keys(), *x.values()] if isinstance(x, (dict, MappingProxyType)) else list(x)
                        opened.add(id(x))
                        stack.extend((items[i : i + CHUNK], False) for i in range(0, len(items), CHUNK))
                        del items
                elif deep and hasattr(x, "__dict__") and not isinstance(x, type):
                    opened.add(id(x))
                    stack.append((list(vars(x).values()), True))
                x = None
        alone = [h for h in held.values() if sys.getrefcount(h) <= ALONE + 1]  # ``held`` holds it too
        del held
        if alone:
            stack.append((alone, True))
        del alone
