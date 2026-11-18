"""Past ``PRESSURE`` of its memory limit the api drops the snapshots it holds, one at a time
(``evo_agents.hub.kg_snapshots.SnapshotCoordinator._ease``). A large graph is gigabytes and a build's new snapshot is
made beside the old one: with two large graphs held, or one loaded again, the api went over its limit, the kernel
killed it, and the first load after each restart looked like a leak."""

from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("sqlalchemy", reason="the api's snapshots need the hub-server extra, evo-ak[hub-server]")

from evo_agents.hub import kg_snapshots  # noqa: E402
from evo_agents.hub.kg_snapshots import Held, SnapshotCoordinator  # noqa: E402
from evo_agents.kg import pacing  # noqa: E402
from evo_agents.kg.snapshot import Snapshot  # noqa: E402

GB = 1 << 30


@pytest.fixture
def memory(monkeypatch):
    """The api's resident memory, under a limit of 10 GB, freed at once (no thread frees aside)."""
    used = {"bytes": 0}
    # an api an earlier test of this process started frees its snapshots aside: until that thread is done, _ease waits
    deadline = time.monotonic() + 30
    while pacing.freeing() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not pacing.freeing(), "the thread an earlier test's api started still frees its snapshots"
    monkeypatch.setattr(kg_snapshots, "memory_limit", lambda: 10 * GB)
    monkeypatch.setattr(kg_snapshots, "resident", lambda: used["bytes"])
    monkeypatch.setattr(pacing, "_aside", None)
    return used


def snapshot() -> Snapshot:
    return Snapshot.__new__(Snapshot)


def coordinator(projects=(1, 2, 3), loading=(), branches=()) -> SnapshotCoordinator:
    """A coordinator holding ``projects``, read least recently first, loads running for ``loading``."""
    co = SnapshotCoordinator()
    for p in projects:
        co._held[p] = Held(p, snapshot())
    for p in loading:
        co._loading[p] = object()
    for key in branches:
        co._branches[key] = ((key,), Held(None, snapshot()))
    return co


def dropping(co: SnapshotCoordinator) -> list:
    """What ``_ease`` drops, in order, until it drops nothing."""
    out = []
    while True:
        before = set(co._held) | set(co._branches)
        co._ease()
        gone = before - set(co._held) - set(co._branches)
        if not gone:
            return out
        out.extend(gone)


def test_under_pressure_the_branches_then_the_projects_read_least_recently_are_dropped(memory):
    memory["bytes"] = 8 * GB
    co = coordinator(projects=(1, 2, 3), branches=[(2, "draft")])
    assert dropping(co) == [(2, "draft"), 1, 2]
    assert list(co._held) == [3], "the project read last stays: dropped, it would be loaded again at its next read"


def test_the_old_snapshot_of_a_project_loading_again_goes_once_the_resting_ones_did(memory):
    memory["bytes"] = 8 * GB
    co = coordinator(projects=(1, 2, 3), loading=(1,))
    co._asked[1] = 9
    assert dropping(co) == [2, 1]
    assert list(co._held) == [3] and co._asked == {}


def test_below_pressure_nothing_is_dropped(memory):
    memory["bytes"] = 5 * GB
    co = coordinator(projects=(1, 2), loading=(1,), branches=[(1, "draft")])
    assert dropping(co) == []
    assert kg_snapshots.memory_limit() * kg_snapshots.PRESSURE > memory["bytes"]


def test_without_a_limit_nothing_is_dropped(memory, monkeypatch):
    monkeypatch.setattr(kg_snapshots, "memory_limit", lambda: None)
    memory["bytes"] = 100 * GB
    assert dropping(coordinator(projects=(1, 2))) == []


def test_one_at_a_time_the_next_once_the_one_before_is_freed(memory, monkeypatch):
    memory["bytes"] = 8 * GB
    co = coordinator(projects=(1, 2, 3))
    read = co._held[1].snapshot  # a reader holds it: not freed yet
    co._ease()
    assert list(co._held) == [2, 3] and co.lingering() == 1
    co._ease()
    assert list(co._held) == [2, 3], "what the first frees is counted before more is dropped"
    del read
    monkeypatch.setattr(pacing, "_pending", 1)  # freed, but the thread that frees it aside is not done
    co._ease()
    assert list(co._held) == [2, 3]
    monkeypatch.setattr(pacing, "_pending", 0)
    co._ease()
    assert list(co._held) == [3]


def test_a_load_thread_under_pressure_has_the_loop_drop_a_snapshot(memory):
    memory["bytes"] = 8 * GB

    async def run():
        co = coordinator(projects=(1, 2))
        co._loop = asyncio.get_running_loop()
        kg_snapshots.serving, before = co, kg_snapshots.serving
        try:
            await asyncio.to_thread(kg_snapshots.tend)
            await asyncio.to_thread(kg_snapshots.tend)  # within PRESS_EVERY of the first: no second look
            await asyncio.sleep(0)
        finally:
            kg_snapshots.serving = before
        return list(co._held)

    assert asyncio.run(run()) == [2]


def test_the_thread_freeing_aside_counts_what_it_holds_and_gives_the_memory_back(monkeypatch):
    given = []
    monkeypatch.setattr(pacing, "give_back", lambda: given.append(True))
    pacing.free_aside()

    class Parts:
        def __init__(self):
            self.rows = [object() for _ in range(1000)]

    pacing.let_go(Parts())
    deadline = time.monotonic() + 10
    while pacing.freeing() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not pacing.freeing()
    assert given, "once nothing is left to free, what the allocator keeps goes back to the system"


def test_give_back_trims_where_glibc_is_and_does_nothing_elsewhere():
    pacing.give_back()


def test_requests_reading_a_snapshot_at_once_share_one_load_per_build_and_free_the_old_ones(memory, monkeypatch):
    """A judge's verdict read the graph with a load of its own, and the worker posting it again after 30 seconds had
    the api make a copy of its largest graph for each post, gigabytes each, at every build, until it was restarted."""
    import gc
    import weakref

    loads = []

    async def load(engine, project_id):
        loads.append(head["build"])
        await asyncio.sleep(0.05)  # the minutes a large graph takes
        return Held(head["build"], snapshot())

    head = {"build": 1}
    monkeypatch.setattr(kg_snapshots, "load", load)
    co = SnapshotCoordinator(engine=object())
    monkeypatch.setattr(kg_snapshots, "serving", co)

    async def builds():
        reader, old = None, []
        for build in (1, 2, 3):
            head["build"] = build
            got = await asyncio.gather(*(kg_snapshots.shared(None, 7, build) for _ in range(4)))
            assert {held.build_id for held in got} == {build}
            if reader is not None:
                old.append(weakref.ref(reader.snapshot))
            reader = got[0]  # a reader still reading the snapshot of the build before while the next one loads
            del got
        del reader
        gc.collect()
        return old

    old = asyncio.run(builds())
    assert loads == [1, 2, 3], "one load per build, however many requests asked at once"
    assert [ref() for ref in old] == [None, None] and co.lingering() == 0
