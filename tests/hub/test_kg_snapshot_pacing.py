"""The api makes a large graph's snapshot in a thread beside its event loop (``evo_agents.hub.kg_snapshots._made``):
the thread gives the GIL up every few milliseconds (``evo_agents.kg.pacing``). A thread that kept the GIL until the
interpreter forced a switch left the loop one step every 5 ms for the minute or two the snapshot of a large graph
took: every pooled connection sat in a transaction the loop could not finish, and the workers' heartbeats and claims
timed out waiting for one."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

pytest.importorskip("sqlalchemy", reason="the api's snapshots need the hub-server extra, evo-ak[hub-server]")

from evo_agents.hub import kg_snapshots  # noqa: E402
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
    with pacing.pacing():
        assert list(pacing.paced(range(10))) == list(range(10))
    assert slept == [pacing.PAUSE_SECONDS] * 3


def test_paced_sorted_sorts_as_sorted_does_equal_items_in_their_order(monkeypatch):
    monkeypatch.setattr(pacing, "RUN", 4)
    items = [(i * 7 % 5, i) for i in range(23)]
    with pacing.pacing():
        assert pacing.paced_sorted(items) == sorted(items)
        by_first = pacing.paced_sorted(items, key=lambda x: x[0])
        assert by_first == sorted(items, key=lambda x: x[0])  # stable as sorted: equal firsts keep their order
        assert pacing.paced_sorted([]) == []


def test_a_build_paces_only_within_pacing(monkeypatch):
    slept = []
    sleep = pacing.time.sleep

    def counted(seconds):  # the thread freeing aside, which a test of the api left running, paces on its own
        if threading.current_thread().name == "kg-free-aside":
            sleep(seconds)
        else:
            slept.append(seconds)

    monkeypatch.setattr(pacing, "YIELD_SECONDS", 0)
    monkeypatch.setattr(pacing.time, "sleep", counted)
    assert list(pacing.paced(range(3))) == [0, 1, 2] and pacing.paced_sorted([2, 1]) == [1, 2]
    assert slept == []  # ``kg build`` and the bench, with no event loop beside them, run at full speed

    async def made():
        with pacing.pacing():
            return await asyncio.to_thread(lambda: list(pacing.paced(range(3))))

    assert asyncio.run(made()) == [0, 1, 2]
    assert slept == [pacing.PAUSE_SECONDS] * 3  # the thread of the api's load got it


def test_a_snapshot_dropped_in_the_api_is_freed_in_a_thread_a_part_at_a_time(monkeypatch):
    """In the api (``pacing.free_aside``), a snapshot replaced or dropped is freed by a thread of its own, a part at a
    time with the GIL given up in between: freed where it was dropped, on the event loop, a large graph's snapshot
    held the loop for seconds, and the workers' heartbeats and claims timed out on the pool meanwhile."""
    import dataclasses
    import threading

    from evo_agents.kg.snapshot import Snapshot

    freed: list[str] = []
    done = threading.Event()
    parts = 3 * pacing.CHUNK

    class Part:
        def __del__(self):
            freed.append(threading.current_thread().name)
            if len(freed) == parts:
                done.set()

    monkeypatch.setattr(pacing, "_aside", None)  # a thread of this test's own, gone with it
    pacing.free_aside()
    snap = Snapshot.from_stored(graph_rows(10))
    # parts held as a large graph's snapshot holds its own: in a tuple, and in lists inside the values of a mapping
    snap = dataclasses.replace(snap, nodes=tuple(Part() for _ in range(parts // 3)))
    object.__setattr__(snap, "review", ({"parts": [Part() for _ in range(2 * parts // 3)]},))
    del snap
    assert done.wait(30)
    assert set(freed) == {"kg-free-aside"}  # none on the thread that dropped it


def test_a_part_that_raises_as_it_is_taken_apart_does_not_stop_the_thread_freeing_the_next_snapshot(monkeypatch):
    """One part raising as it is opened is logged, and the thread goes on: ended, it left every snapshot dropped
    after it in memory until the api restarted."""
    import threading

    from evo_agents.kg.snapshot import Snapshot

    freed = threading.Event()

    class Raising:
        def __getattribute__(self, name):
            if name == "__dict__":
                raise RuntimeError("a part that cannot be opened")
            return object.__getattribute__(self, name)

    class Part:
        def __del__(self):
            freed.set()

    monkeypatch.setattr(pacing, "_aside", None)  # a thread of this test's own, gone with it
    pacing.free_aside()
    snap = Snapshot.from_stored(graph_rows(10))
    object.__setattr__(snap, "review", Raising())
    del snap
    snap = Snapshot.from_stored(graph_rows(10))
    object.__setattr__(snap, "review", Part())
    del snap
    assert freed.wait(30)


def _sleeps(monkeypatch, into: list) -> None:
    sleep = pacing.time.sleep

    def counted(seconds):
        if threading.current_thread().name == "kg-free-aside":
            sleep(seconds)
        else:
            into.append(seconds)

    monkeypatch.setattr(pacing, "YIELD_SECONDS", 0)
    monkeypatch.setattr(pacing.time, "sleep", counted)


def test_paced_sleeps_longer_while_requests_wait_but_never_in_the_thread_of_the_loop(monkeypatch):
    slept, waiting = [], [True]
    _sleeps(monkeypatch, slept)

    async def made(busy):
        with pacing.pacing(busy=busy):
            in_thread = await asyncio.to_thread(lambda: list(pacing.paced(range(2))))
            on_loop = list(pacing.paced(range(2)))  # a sleep here would stop every request
            return in_thread, on_loop

    assert asyncio.run(made(lambda: waiting[0])) == ([0, 1], [0, 1])
    assert slept == [pacing.BUSY_PAUSE_SECONDS] * 2 + [pacing.PAUSE_SECONDS] * 2
    slept.clear()
    waiting[0] = False
    asyncio.run(made(lambda: waiting[0]))
    assert slept == [pacing.PAUSE_SECONDS] * 4


def test_a_snapshot_is_made_at_the_slow_pace_while_a_request_holds_a_connection_of_the_pool(monkeypatch):
    """A heartbeat is ten queries, each a turn of the loop that waits for the GIL the thread making a snapshot holds:
    on the busy host of the hub the thread's 1 ms pauses left it 2 s, held in its transaction."""
    from types import SimpleNamespace

    from evo_agents.hub import db
    from tests.hub import pg

    if not pg.DSN:
        pytest.skip(pg.SKIP_REASON)
    slept = []
    _sleeps(monkeypatch, slept)
    rows = graph_rows(300)

    async def go():
        pool = db.create_pool(SimpleNamespace(dsn=pg.DSN, pool_min_size=1, pool_max_size=2, pool_timeout=5.0))
        await pool.open(wait=True, timeout=10)
        try:
            assert not kg_snapshots.requests_waiting()
            await kg_snapshots._made(1, rows)
            at_rest = list(slept)
            slept.clear()
            async with pool.connection():  # a request in the middle of its transaction
                assert kg_snapshots.requests_waiting()
                await kg_snapshots._made(1, rows)
            return at_rest, list(slept)
        finally:
            await pool.close()

    at_rest, busy = asyncio.run(go())
    assert at_rest and set(at_rest) == {pacing.PAUSE_SECONDS}
    assert busy and set(busy) == {pacing.BUSY_PAUSE_SECONDS}


def test_the_rows_a_load_is_done_with_are_freed_by_the_thread_aside_not_on_the_loop(monkeypatch):
    """The raw rows, once put together, and the assembled rows, once the snapshot is made, were freed on the loop as
    ``load`` returned: millions of objects, a second or more in which no request ran, a heartbeat's connection held
    meanwhile."""
    import contextlib

    freed: list[str] = []

    class Part:
        def __del__(self):
            freed.append(threading.current_thread().name)

    class Rows:
        def __init__(self):
            self.parts = tuple(Part() for _ in range(2 * pacing.CHUNK))

    class Engine:
        @contextlib.asynccontextmanager
        async def connect(self):
            yield None

    async def rows(conn, project_id):
        return 7, Rows()

    async def assembled(raw):
        return Rows()

    async def made(build_id, rows):
        return kg_snapshots.Held(build_id, None)

    monkeypatch.setattr(pacing, "_aside", None)  # a thread of this test's own, gone with it
    pacing.free_aside()
    monkeypatch.setattr(kg_snapshots, "_rows", rows)
    monkeypatch.setattr(kg_snapshots, "_assembled", assembled)
    monkeypatch.setattr(kg_snapshots, "_made", made)
    held = asyncio.run(kg_snapshots.load(Engine(), 1))
    assert held.build_id == 7
    deadline = time.monotonic() + 30
    while (len(freed) < 4 * pacing.CHUNK or pacing.freeing()) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(freed) == 4 * pacing.CHUNK and set(freed) == {"kg-free-aside"}


def test_the_thread_freeing_aside_paces_slowly_while_requests_wait(monkeypatch):
    slept = []
    sleep = pacing.time.sleep

    def counted(seconds):
        if threading.current_thread().name == "kg-free-aside":
            slept.append(seconds)
        sleep(min(seconds, 0.001))

    monkeypatch.setattr(pacing, "YIELD_SECONDS", 0)
    monkeypatch.setattr(pacing.time, "sleep", counted)
    monkeypatch.setattr(pacing, "_aside", None)  # a thread of this test's own, gone with it
    pacing.free_aside(lambda: True)
    pacing.give_aside([[[object()] * 3 for _ in range(3 * pacing.CHUNK)]])
    deadline = time.monotonic() + 30
    while (pacing.freeing() or not slept) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pacing.BUSY_PAUSE_SECONDS in slept and pacing.PAUSE_SECONDS not in slept
