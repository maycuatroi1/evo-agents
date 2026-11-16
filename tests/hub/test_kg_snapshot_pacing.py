"""The api makes a large graph's snapshot in a thread beside its event loop (``evo_agents.hub.kg_snapshots._made``):
the thread gives the GIL up every few milliseconds (``evo_agents.kg.pacing``). A thread that kept the GIL until the
interpreter forced a switch left the loop one step every 5 ms for the minute or two the snapshot of a large graph
took: every pooled connection sat in a transaction the loop could not finish, and the workers' heartbeats and claims
timed out waiting for one."""

from __future__ import annotations

import asyncio
import time

from evo_agents.hub import kg_snapshots
from evo_agents.kg import pacing
from evo_agents.kg.snapshot import StoreRows

NODES = 30_000


def graph_rows(n: int) -> StoreRows:
    """A graph of ``n`` functions, four edges each, and a full-text row each: big enough that making its snapshot
    takes seconds."""
    node = lambda i: f"lumen-api:src/mod{i % 50}/file{i % 400}.py::fn_{i}"  # noqa: E731
    nodes = tuple(
        (node(i), "Function", f"fn_{i}", '{"path": "src/a.py", "line": 3}', None, 1.0, 1, 0, 0, "[]", None)
        for i in range(n)
    )
    rels = ("calls", "imports", "defines")
    edges = tuple(
        (f"e{i}", node(i % n), rels[i % 3], node((i * 7 + 1) % n), "{}", None, 1.0, 1, 0, 0, None) for i in range(4 * n)
    )
    keyed = tuple(
        (
            node(i),
            (node(i), "Function", f"fn_{i} helper {i % 97}", f"src/mod{i % 50}/file{i % 400}.py", f"word{i % 1000}"),
        )
        for i in range(n)
    )
    return StoreRows("northwind", nodes, edges, keyed, {}, ((1, 0), frozenset()))


def test_the_event_loop_keeps_turning_while_a_large_snapshot_is_made():
    rows = graph_rows(NODES)

    async def run():
        gaps, done = [], asyncio.Event()

        async def tick():
            last = time.perf_counter()
            while not done.is_set():
                await asyncio.sleep(0.001)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        ticker = asyncio.create_task(tick())
        with kg_snapshots.uncollected():  # as ``load`` makes it
            held = await kg_snapshots._made(7, rows)
        done.set()
        await ticker
        return held, sorted(gaps)

    held, gaps = asyncio.run(run())
    assert held.build_id == 7 and len(held.snapshot.ids) == NODES
    assert len(gaps) > 100
    # the loop turned every few milliseconds: with the GIL kept, one turn in a hundred took more than 0.1 s
    assert gaps[int(len(gaps) * 0.99)] < 0.05


def test_paced_gives_every_item_and_the_gil_up_as_time_goes_by(monkeypatch):
    slept, clock = [], iter(range(100))
    monkeypatch.setattr(pacing, "YIELD_SECONDS", 3)
    monkeypatch.setattr(pacing.time, "perf_counter", lambda: next(clock))
    monkeypatch.setattr(pacing.time, "sleep", slept.append)
    assert list(pacing.paced(range(10))) == list(range(10))
    assert slept == [pacing.PAUSE_SECONDS] * 3


def test_paced_sorted_sorts_as_sorted_does_equal_items_in_their_order(monkeypatch):
    monkeypatch.setattr(pacing, "RUN", 4)
    items = [(i * 7 % 5, i) for i in range(23)]
    assert pacing.paced_sorted(items) == sorted(items)
    by_first = pacing.paced_sorted(items, key=lambda x: x[0])
    assert by_first == sorted(items, key=lambda x: x[0])  # sorted is stable: the seconds of equal firsts stay in order
    assert pacing.paced_sorted([]) == []
