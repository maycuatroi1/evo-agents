"""The api's in-RAM graphs (plan kg-centralized-on-hub, criterion a4): per project, the snapshot
(``evo_agents.kg.snapshot.Snapshot``) of the main branch as the hub's tables keep it (``kg_graph_rows.read_graph``).

A project's snapshot is loaded on its first read and kept. A build that writes the tables notifies ``kg_graph``
(``kg_graph_rows.notify``); the coordinator, subscribed on the process's listener (``evo_agents.hub.server.listen``),
then builds the project's new snapshot in a background task and swaps it in with one assignment: a reader holds the
snapshot it was given until it is done. A reader that found a build newer than the snapshot held waits up to
FRESH_WAIT for that load, so a graph read right after a build answers from it, and is given the older one after. After the listener (re)connects, every project
held is read again, since a notification sent while nobody listened is lost. A snapshot is the whole graph: each
reader sees it through its own clearance and audience (``evo_agents.kg.snapshot_search.SnapshotSearch``)."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import gc
import json
import logging
import os
import time
import weakref
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import kg_branches, tables
from evo_agents.hub.kg_graph_rows import CHANNEL, MAIN, assemble_graph, fetch_graph
from evo_agents.kg import pacing as kg_pacing
from evo_agents.kg import settings as kg_settings
from evo_agents.kg.pacing import pacing
from evo_agents.kg.snapshot import Snapshot, SnapshotView, StoreRows

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Held:
    """A project's snapshot and the build that wrote the rows it was made of."""

    build_id: int | None
    snapshot: Snapshot

    def view(
        self,
        reader: frozenset[str],
        ceiling: tuple[int, int] | None = None,
        tuning: kg_settings.Tuning = kg_settings.DEFAULT,
    ) -> SnapshotView:
        """The snapshot as ``reader`` reads it: a view of its own, so a caller that sets its reader or clearance
        changes no other's, over the one snapshot every view shares."""
        return self.snapshot.view(reader, ceiling, tuning)


async def load(engine: AsyncEngine, project_id: int) -> Held | None:
    """The snapshot of the project's main branch read from the tables now, not kept: for a job that reads the graph
    once (``evo_agents.hub.server.kg_health``); None before its first build kept a graph. The rows are read on a
    connection of their own, in one of the process's ``READS`` slots, and it goes back to the pool before the snapshot
    is made of them: making it takes minutes on a large graph, and a connection held meanwhile, idle in its
    transaction, is one the workers' heartbeats and claims wait for."""
    with uncollected():
        async with timed(project_id) as timing:
            async with read_slot():
                timing.phase("slot")
                async with engine.connect() as conn:
                    fetched = await _rows(conn, project_id)
                if fetched is not None:  # still in the slot, so the raw rows of at most READS loads wait in memory
                    fetched = fetched[0], await _assembled(fetched[1])
            timing.phase("rows")
            if fetched is None:
                return None
            held = await _made(*fetched)
            timing.phase("made")
            return held


async def load_on(conn: AsyncConnection, project_id: int) -> Held | None:
    """``load`` on the caller's connection, which it holds while the snapshot is made: a caller that can give its
    connection back first uses ``load``."""
    with uncollected():
        found = await _rows(conn, project_id)
        return None if found is None else await _made(found[0], await _assembled(found[1]))


_loads = 0  # loads of the process running now, in ``uncollected``
ONLY_YOUNG = 2**31 - 1  # the last threshold of ``gc.set_threshold`` while loads run: no full collection is reached
# objects made since the last full collection after which a load's thread collects everything and freezes it
# (``settle``), as the collections of the middle generation count them: a collection then walks about as many
SETTLE = 100_000
_settled: list[float] = []  # the seconds of each ``settle`` that collected since the first of the loads running now


@contextlib.contextmanager
def uncollected():
    """No full collection of Python's cyclic garbage collector reached by its thresholds while a snapshot is read and
    made, and what it made frozen out of the later ones (``gc.freeze``). A snapshot is millions of objects the collector
    tracks: each full collection walked them all, holding the GIL, the loop of the api with it, for seconds, dozens of
    times while a large graph loaded and again now and then once it was held, and the heartbeats and claims waiting
    meanwhile timed out on the pool. A snapshot has no reference cycle, so one dropped is freed though frozen. Loads
    share the process's collector: while one runs, the collections of the young generations go on, each walking the few
    thousand objects made since the one before, so the cyclic garbage the requests served meanwhile leave is freed as it
    would be. The thread making a snapshot, each time about SETTLE objects were made, collects everything and freezes
    what is left (``settle``), and the last load to end does it once more: a cycle that lived long enough to reach the
    old generation before it died is collected, not frozen with the snapshot, never to be freed, and each collection
    walks what was made since the one before, not the whole snapshot. One collection at the end walked the whole new
    snapshot instead, 9 seconds of a stalled loop on the hub's largest graph. An object of a request running at one of
    them is frozen with the snapshot, and a cycle it is in later is not collected."""
    global _loads, _thresholds
    if _loads == 0:
        _thresholds = gc.get_threshold()
        gc.set_threshold(_thresholds[0], _thresholds[1], ONLY_YOUNG)
        _settled.clear()
    _loads += 1
    try:
        yield
    finally:
        _loads -= 1
        if _loads == 0:
            start = time.perf_counter()
            gc.collect()
            gc.freeze()
            log.info(
                "kg snapshot frozen",
                extra={
                    "collect_s": round(time.perf_counter() - start, 3),
                    "frozen": gc.get_freeze_count(),
                    "settled": len(_settled),
                    "settle_max_s": round(max(_settled, default=0.0), 3),
                },
            )
            gc.set_threshold(*_thresholds)


def settle() -> None:
    """While loads run, once about SETTLE objects were made since the last full collection: collect everything and
    freeze what is left (``uncollected``). Called by a load's paced thread (``tend``). Each collection of the middle
    generation is one after ``threshold1`` of the youngest, each after ``threshold0`` objects made."""
    if _loads and gc.get_count()[2] * _thresholds[0] * _thresholds[1] >= SETTLE:
        start = time.perf_counter()
        gc.collect()
        gc.freeze()
        _settled.append(time.perf_counter() - start)


_thresholds = gc.get_threshold()  # the collector's thresholds before the first of the loads running now


def tend() -> None:
    """What a load's paced thread calls between two steps: ``settle``, and a look at the api's memory
    (``SnapshotCoordinator.pressed``)."""
    settle()
    if serving is not None:
        serving.pressed()


PRESSURE = 0.75  # share of the api's memory limit past which it drops the snapshots it holds (``SnapshotCoordinator``)
PRESS_EVERY = 1.0  # seconds between two looks at the api's memory while a load runs


def memory_limit() -> int | None:
    """The memory limit of the process's cgroup in bytes (v2, else v1); None without one."""
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            text = Path(path).read_text().strip()
        except OSError:
            continue
        return int(text) if text.isdigit() and int(text) < 2**60 else None  # "max", or v1's way to say none
    return None


def resident() -> int:
    """The process's resident memory in bytes; 0 where /proc does not tell it."""
    try:
        return int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0


STALL = 0.1  # seconds the loop's watch sleeps between two looks at the clock


class Timing:
    """The seconds each phase of a load took and, meanwhile, the longest time the event loop ran nothing else: what a
    heartbeat or a claim arriving then waited before its handler started."""

    def __init__(self) -> None:
        self.phases: dict[str, dict[str, float]] = {}
        self._start = self._slept = time.perf_counter()
        self._stall = 0.0

    def phase(self, name: str) -> None:
        """End the phase running now as ``name``; the next one starts. A stall still running, which the watch has not
        woken up from yet, is the phase's too."""
        now = time.perf_counter()
        stall = max(self._stall, now - self._slept - STALL)
        self.phases[name] = {"s": round(now - self._start, 3), "stall_s": round(stall, 3)}
        self._start = self._slept = now
        self._stall = 0.0

    async def watch(self) -> None:
        """Sleep STALL at a time and keep the longest overrun: a loop that ran nothing else for a while wakes late."""
        while True:
            self._slept = time.perf_counter()
            await asyncio.sleep(STALL)
            self._stall = max(self._stall, time.perf_counter() - self._slept - STALL)


@contextlib.asynccontextmanager
async def timed(project_id: int):
    """A ``Timing`` of a load of the project's snapshot, logged as it ends."""
    timing = Timing()
    watch = asyncio.get_running_loop().create_task(timing.watch(), name=f"kg-snapshot-watch-{project_id}")
    try:
        yield timing
    finally:
        watch.cancel()
        log.info("kg snapshot load timed", extra={"project_id": project_id, "phases": timing.phases})


async def _rows(conn: AsyncConnection, project_id: int) -> tuple[int | None, tuple] | None:
    """(the build that wrote the rows of the project's main branch, those rows as ``fetch_graph`` read them, still to
    be put together by ``assemble_graph``, which needs no connection); None before its first build."""
    heads = tables.kg_graph_heads
    build_id = await conn.scalar(
        select(heads.c.build_id).where(heads.c.project_id == project_id, heads.c.branch_id == MAIN)
    )
    with pacing(tend):
        rows = await fetch_graph(conn, project_id)
    return None if rows is None else (build_id, rows)


async def _assembled(fetched: tuple) -> StoreRows:
    """The rows ``_rows`` fetched, put together in a paced thread, with no connection held: it takes minutes on a large
    graph, and a connection held meanwhile, idle in its transaction, is one the heartbeats and claims wait for."""
    with pacing(tend):
        return await assemble_graph(fetched)


async def _made(build_id: int | None, rows: StoreRows) -> Held:
    """The snapshot of ``rows``, made in a thread, with no connection held. The thread sleeps 1 ms every 2 ms
    (``evo_agents.kg.pacing.paced``): a large graph's snapshot takes a minute or two to make, and a
    thread that kept the GIL until forced to switch left the event loop one step every 5 ms meanwhile, every pooled
    connection held in a transaction the loop could not finish, and the workers' heartbeats and claims timed out
    waiting for one."""
    with pacing(tend):
        return Held(build_id, await asyncio.to_thread(Snapshot.from_stored, *hub_build(rows, build_id)))


READS = 2  # graph reads a process runs at once, each on a connection of the pool; the next waits for a slot


_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()


def read_slot() -> asyncio.Semaphore:
    """The semaphore of the running loop that bounds its graph reads to ``READS``: the rows of a large graph take a
    connection for a minute, so loads of many projects, or of the same project by several jobs, would otherwise hold
    the whole pool. One per loop, as a semaphore belongs to the loop it first waited on."""
    loop = asyncio.get_running_loop()
    slot = _slots.get(loop)
    if slot is None:
        slot = _slots[loop] = asyncio.Semaphore(READS)
    return slot


def hub_build(rows: StoreRows, build_id: int | None) -> tuple[StoreRows, int]:
    """(``rows`` whose summary names the kg_builds id ``build_id``, that id): a build makes its rows in a fresh store,
    whose own build is 1, so a tool's answer would name build 1 instead of the hub's."""
    if build_id is None:
        return rows, 0
    return dataclasses.replace(rows, summary={**rows.summary, "build_id": build_id}), build_id


async def held_for(conn: AsyncConnection, project_id: int, build_id: int) -> Held | None:
    """The project's snapshot for a reader that has only its connection: the one the api keeps (``serving``,
    ``SnapshotCoordinator.held``), None while its first load runs, else one read on ``conn`` (``load_on``), as in a
    process with no api. The reader holds ``conn`` in its transaction, so it never waits for a load."""
    coordinator = serving
    if coordinator is not None and coordinator.engine is not None:
        return await coordinator.held(project_id, build_id)
    return await load_on(conn, project_id)


HELD_PROJECTS = 16  # projects whose snapshot the api keeps in memory, the most recently read; another is loaded again
HELD_BRANCHES = 8  # draft branches whose snapshot the api keeps, the most recently read
FRESH_WAIT = 5.0  # seconds a read that found a newer build waits for its snapshot before the older one answers


def _over() -> bool:
    """Whether the api's resident memory is past PRESSURE of its limit; never without a limit."""
    limit = memory_limit()
    return limit is not None and resident() > limit * PRESSURE


async def _branch_key(conn: AsyncConnection, project_id: int, name: str) -> tuple:
    """What a draft branch's snapshot is made of: the branch, its status and operations, and main's rows now. Raises
    kg_branches.BranchError 404 for a branch the project lacks."""
    branch = await kg_branches.get(conn, project_id, name)
    heads = tables.kg_graph_heads
    head = await conn.scalar(
        select(heads.c.content_hash).where(heads.c.project_id == project_id, heads.c.branch_id == MAIN)
    )
    return branch.id, branch.status, branch.ops, head


class SnapshotCoordinator:
    def __init__(self, engine: AsyncEngine | None = None, listener=None):
        self.engine = engine  # the api's, set once it opened
        self._listener = listener
        self._held: dict[int, Held] = {}
        self._loading: dict[int, asyncio.Task] = {}
        self._began: dict[int, float] = {}  # project: when its load in ``_loading`` began, on the loop's clock
        self._tasks: set[asyncio.Task] = set()
        self._branches: dict[tuple[int, str], tuple[tuple, Held]] = {}  # (project, branch): (its key, snapshot)
        self._asked: dict[int, int] = {}  # project: the newest build ``current`` loaded again for
        self._dropped: list[weakref.ref[Snapshot]] = []  # the snapshots replaced or dropped, until they are freed
        self._loop: asyncio.AbstractEventLoop | None = None  # the api's, set by the first load
        self._looked = 0.0  # when ``pressed`` last looked at the memory
        if listener is not None:
            listener.on(CHANNEL, self.notified)

    async def get(self, project_id: int) -> Held | None:
        """The project's snapshot, loaded on first use; None before its first build kept a graph."""
        if self._listener is not None:
            self._listener.start()  # the first claim, stream or graph read starts it
        held = self._held.pop(project_id, None)
        if held is not None:
            self._held[project_id] = held  # the most recently read last: ``_keep`` drops the first ones
            return held
        return await self._load(project_id)

    async def view(
        self, project_id: int, reader: frozenset[str], ceiling: tuple[int, int] | None = None
    ) -> SnapshotView | None:
        """The project's snapshot as ``reader`` reads it (``Held.view``); None before its first build kept a graph."""
        held = await self.get(project_id)
        return None if held is None else held.view(reader, ceiling)

    async def current(self, project_id: int, build_id: int, wait: float | None = None) -> Held | None:
        """The project's snapshot, None before its first build kept a graph. One older than build ``build_id``, the
        newest the caller found, starts a load, once per build, unless one runs (a caller that missed the
        notification, or reads without a listener, catches up), and the caller waits up to ``wait`` seconds
        (FRESH_WAIT) for the load running, counted from when that load began: a read right after a build answers from
        it, as its writer expects, and one coming after the load ran that long waits no more. Past that, or when the
        load fails, the older snapshot answers; the load goes on."""
        held = await self.get(project_id)
        if held is None or held.build_id == build_id:
            return held
        if self._asked.get(project_id) != build_id:
            self._asked[project_id] = build_id
            task = self._load(project_id)
            self._tasks.add(task)
            task.add_done_callback(self._done)
        task = self._loading.get(project_id)
        wait = FRESH_WAIT if wait is None else wait
        if task is not None:
            wait -= asyncio.get_running_loop().time() - self._began.get(project_id, 0.0)
        if task is None or wait <= 0:
            return held
        with contextlib.suppress(Exception):  # a slow load times out, a failed one is logged by ``_done``
            await asyncio.wait_for(asyncio.shield(task), wait)
        return self._held.get(project_id, held)

    async def held(self, project_id: int, build_id: int) -> Held | None:
        """``current`` for a caller that must not wait for a load: None while the project has no snapshot in memory,
        whose load this starts in the background (``kg_graph.reading`` then answers "try again shortly")."""
        if project_id in self._held:
            return await self.current(project_id, build_id, wait=0)
        if self._listener is not None:
            self._listener.start()
        task = self._load(project_id)
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return None

    async def branch(self, project_id: int, name: str) -> Held:
        """The snapshot of draft branch ``name``: main's rows with its operations applied (``kg_branches.graph``),
        made again once it or main changed. Reads on connections of its own, given back before the snapshot is made,
        as ``load`` does: the caller holds none meanwhile. Raises kg_branches.BranchError 404 for a branch the
        project lacks."""
        assert self.engine is not None
        async with self.engine.connect() as conn:
            key = await _branch_key(conn, project_id, name)
        found = self._branches.pop((project_id, name), None)
        if found is None or found[0] != key:
            heads = tables.kg_graph_heads
            with uncollected(), pacing(tend):
                async with read_slot(), self.engine.connect() as conn:
                    build_id = await conn.scalar(
                        select(heads.c.build_id).where(heads.c.project_id == project_id, heads.c.branch_id == MAIN)
                    )
                    _, _, rows = await kg_branches.graph(conn, project_id, name)
                if found is not None:
                    self._watch(found[1].snapshot)
                found = key, await _made(build_id, rows)
        self._branches[(project_id, name)] = found
        while len(self._branches) > HELD_BRANCHES:
            self._watch(self._branches.pop(next(iter(self._branches)))[1].snapshot)
        return found[1]

    def _load(self, project_id: int, fresh: bool = False) -> asyncio.Task:
        """The task loading the project's snapshot; ``fresh``: one that reads the tables after this call, queued
        behind a load already running, which may have read them before the build that notified."""
        self._loop = asyncio.get_running_loop()
        task = self._loading.get(project_id)
        if task is None or task.done() or fresh:
            build = self._build(project_id, None if task is None or task.done() else task)
            task = asyncio.get_running_loop().create_task(build, name=f"kg-snapshot-{project_id}")
            self._loading[project_id] = task
            self._began[project_id] = asyncio.get_running_loop().time()
        return task

    async def _build(self, project_id: int, after: asyncio.Task | None = None) -> Held | None:
        if after is not None:
            await asyncio.gather(after, return_exceptions=True)
        assert self.engine is not None
        try:
            held = await load(self.engine, project_id)
        finally:
            if self._loading.get(project_id) is asyncio.current_task():
                del self._loading[project_id]  # a finished load is not kept; a fresh one queued behind it stays
                self._began.pop(project_id, None)
        if held is None:
            return None
        self._keep(project_id, held)
        log.info(
            "kg snapshot loaded",
            extra={
                "project_id": project_id,
                "build_id": held.build_id,
                "lingering": self.lingering(),
                "resident_mb": resident() >> 20,
            },
        )
        self._ease()
        return held

    def _keep(self, project_id: int, held: Held) -> None:
        """Hold ``held`` as the project's snapshot, the most recently read, and drop the least recently read ones
        past HELD_PROJECTS: a notification for a dropped project is ignored, and its next read loads it again."""
        old = self._held.pop(project_id, None)
        self._held[project_id] = held  # one assignment: a reader sees the old snapshot or the new one
        if old is not None and old.snapshot is not held.snapshot:
            self._watch(old.snapshot)
        while len(self._held) > HELD_PROJECTS:
            dropped = next(iter(self._held))
            self._watch(self._held.pop(dropped).snapshot)
            self._asked.pop(dropped, None)

    def pressed(self) -> None:
        """From a load's paced thread (``tend``), once a PRESS_EVERY at most: past PRESSURE of the api's memory limit,
        drop a snapshot on the loop (``_ease``)."""
        now = time.monotonic()
        if self._loop is None or now - self._looked < PRESS_EVERY:
            return
        self._looked = now
        if _over():
            self._loop.call_soon_threadsafe(self._ease)

    def _ease(self) -> None:
        """Past PRESSURE of the api's memory limit, drop one snapshot: a draft branch's, then the projects read least
        recently, and only then the old snapshot of a project whose load runs, whose readers then wait for the new
        one. A large graph is gigabytes and the api makes the new one beside the old: with two of
        them, or one loaded again, the api was over its limit and the kernel killed it, every graph lost. One at a
        time, the next only once the one before is freed (``lingering``, ``kg_pacing.freeing``), so what it frees is
        counted before more is dropped. The project read last keeps its snapshot unless it loads: dropped, the one just
        loaded would be loaded again at its next read."""
        if not _over() or self.lingering() or kg_pacing.freeing():
            return
        loading = [p for p in self._held if p in self._loading]
        resting = [p for p in self._held if p not in self._loading]
        if self._branches:
            key = next(iter(self._branches))
            self._watch(self._branches.pop(key)[1].snapshot)
            dropped = {"project_id": key[0], "branch": key[1]}
        elif resting[:-1] or loading:
            project_id = (resting[:-1] or loading)[0]
            self._watch(self._held.pop(project_id).snapshot)
            self._asked.pop(project_id, None)
            dropped = {"project_id": project_id, "loading": project_id in self._loading}
        else:
            return
        limit = memory_limit() or 0
        log.warning(
            "kg snapshot dropped for memory",
            extra={**dropped, "resident_mb": resident() >> 20, "limit_mb": limit >> 20},
        )

    def _watch(self, dropped: Snapshot) -> None:
        if isinstance(dropped, Snapshot):
            self._dropped.append(weakref.ref(dropped))

    def lingering(self) -> int:
        """The snapshots this coordinator replaced or dropped that are still in memory: a reader holds one until it is
        done, so a few for a while; more after each build means something holds them for good, and the api grows by a
        whole graph each time (``evo_agents.kg.snapshot.SnapshotView.ranker``). Logged with each load."""
        self._dropped = [ref for ref in self._dropped if ref() is not None]
        return len(self._dropped)

    def notified(self, payload: str | None) -> None:
        """A listener's handler: rebuild the snapshot a notification names, or each held one after a reconnection."""
        if payload is None:
            projects = list(self._held)
        else:
            try:
                note = json.loads(payload)
            except ValueError:
                return
            if note.get("branch_id", MAIN) != MAIN or note.get("project_id") not in self._held:
                return  # a project nobody read yet is loaded on its first read
            projects = [note["project_id"]]
        for project_id in projects:
            task = self._load(project_id, fresh=True)
            self._tasks.add(task)
            task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("kg snapshot rebuild failed", exc_info=task.exception())

    async def close(self) -> None:
        global serving
        if serving is self:
            serving = None
        for task in [*self._tasks, *self._loading.values()]:
            task.cancel()
        await asyncio.gather(*self._tasks, *self._loading.values(), return_exceptions=True)


serving: SnapshotCoordinator | None = None  # the api's, set as it opens: what ``held_for`` reads through
