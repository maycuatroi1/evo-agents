"""The api's in-RAM graphs (plan kg-centralized-on-hub, criterion a4): per project, the snapshot
(``evo_agents.kg.snapshot.Snapshot``) of the main branch as the hub's tables keep it (``kg_graph_rows.read_graph``).

A project's snapshot is loaded on its first read and kept. A build that writes the tables notifies ``kg_graph``
(``kg_graph_rows.notify``); the coordinator, subscribed on the process's listener (``evo_agents.hub.server.listen``),
then builds the project's new snapshot in a background task and swaps it in with one assignment: a reader holds the
snapshot it was given until it is done and never waits for a build. After the listener (re)connects, every project
held is read again, since a notification sent while nobody listened is lost. A snapshot is the whole graph: each
reader sees it through its own clearance and audience (``evo_agents.kg.snapshot_search.SnapshotSearch``)."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import gc
import json
import logging
import weakref
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import kg_branches, tables
from evo_agents.hub.kg_graph_rows import CHANNEL, MAIN, read_graph
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
        async with read_slot(), engine.connect() as conn:
            found = await _rows(conn, project_id)
        return None if found is None else await _made(*found)


async def load_on(conn: AsyncConnection, project_id: int) -> Held | None:
    """``load`` on the caller's connection, which it holds while the snapshot is made: a caller that can give its
    connection back first uses ``load``."""
    with uncollected():
        found = await _rows(conn, project_id)
        return None if found is None else await _made(*found)


_loads = 0  # loads of the process running now, in ``uncollected``
ONLY_YOUNG = 2**31 - 1  # the last threshold of ``gc.set_threshold`` while loads run: no full collection is reached


@contextlib.contextmanager
def uncollected():
    """No full collection of Python's cyclic garbage collector while a snapshot is read and made, and what it made
    frozen out of the later ones (``gc.freeze``). A snapshot is millions of objects the collector tracks: each full
    collection walked them all, holding the GIL, the loop of the api with it, for seconds, dozens of times while a
    large graph loaded and again now and then once it was held, and the heartbeats and claims waiting meanwhile timed
    out on the pool. A snapshot has no reference cycle, so one dropped is freed though frozen. Loads share the
    process's collector: while one runs, the collections of the young generations go on, each walking the few
    thousand objects made since the one before, so the cyclic garbage the requests served meanwhile leave is freed as
    it would be. The last load to end collects everything once, so a cycle that lived long enough to reach the old
    generation before it died is not frozen with the snapshot, never to be freed; then it freezes the rest and gives
    the collector its thresholds back. That one collection walks the new snapshot, not those frozen before: one stall
    of a second or two per load, where full collections during it stalled the loop dozens of times. An object of a
    request still running then is frozen with the snapshot, and a cycle it is in later is not collected."""
    global _loads, _thresholds
    if _loads == 0:
        _thresholds = gc.get_threshold()
        gc.set_threshold(_thresholds[0], _thresholds[1], ONLY_YOUNG)
    _loads += 1
    try:
        yield
    finally:
        _loads -= 1
        if _loads == 0:
            gc.collect()
            gc.freeze()
            gc.set_threshold(*_thresholds)


_thresholds = gc.get_threshold()  # the collector's thresholds before the first of the loads running now


async def _rows(conn: AsyncConnection, project_id: int) -> tuple[int | None, StoreRows] | None:
    """(the build that wrote the rows of the project's main branch, those rows); None before its first build."""
    heads = tables.kg_graph_heads
    build_id = await conn.scalar(
        select(heads.c.build_id).where(heads.c.project_id == project_id, heads.c.branch_id == MAIN)
    )
    with pacing():
        rows = await read_graph(conn, project_id)
    return None if rows is None else (build_id, rows)


async def _made(build_id: int | None, rows: StoreRows) -> Held:
    """The snapshot of ``rows``, made in a thread, with no connection held. The thread sleeps 1 ms every 2 ms
    (``evo_agents.kg.pacing.paced``): a large graph's snapshot takes a minute or two to make, and a
    thread that kept the GIL until forced to switch left the event loop one step every 5 ms meanwhile, every pooled
    connection held in a transaction the loop could not finish, and the workers' heartbeats and claims timed out
    waiting for one."""
    with pacing():
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
        self._tasks: set[asyncio.Task] = set()
        self._branches: dict[tuple[int, str], tuple[tuple, Held]] = {}  # (project, branch): (its key, snapshot)
        self._asked: dict[int, int] = {}  # project: the newest build ``current`` loaded again for
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

    async def current(self, project_id: int, build_id: int) -> Held | None:
        """The project's snapshot, None before its first build kept a graph. One older than build ``build_id``, the
        newest the caller found, is still given, and a load starts, once per build, unless one runs: a caller that
        missed the notification, or reads without a listener, catches up without waiting for it."""
        held = await self.get(project_id)
        if held is not None and held.build_id != build_id and self._asked.get(project_id) != build_id:
            self._asked[project_id] = build_id
            task = self._load(project_id)
            self._tasks.add(task)
            task.add_done_callback(self._done)
        return held

    async def held(self, project_id: int, build_id: int) -> Held | None:
        """``current`` for a caller that must not wait for a load: None while the project has no snapshot in memory,
        whose load this starts in the background (``kg_graph.reading`` then answers "try again shortly")."""
        if project_id in self._held:
            return await self.current(project_id, build_id)
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
            with uncollected(), pacing():
                async with read_slot(), self.engine.connect() as conn:
                    build_id = await conn.scalar(
                        select(heads.c.build_id).where(heads.c.project_id == project_id, heads.c.branch_id == MAIN)
                    )
                    _, _, rows = await kg_branches.graph(conn, project_id, name)
                found = key, await _made(build_id, rows)
        self._branches[(project_id, name)] = found
        while len(self._branches) > HELD_BRANCHES:
            del self._branches[next(iter(self._branches))]
        return found[1]

    def _load(self, project_id: int, fresh: bool = False) -> asyncio.Task:
        """The task loading the project's snapshot; ``fresh``: one that reads the tables after this call, queued
        behind a load already running, which may have read them before the build that notified."""
        task = self._loading.get(project_id)
        if task is None or task.done() or fresh:
            build = self._build(project_id, None if task is None or task.done() else task)
            task = asyncio.get_running_loop().create_task(build, name=f"kg-snapshot-{project_id}")
            self._loading[project_id] = task
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
        if held is None:
            return None
        self._keep(project_id, held)
        log.info("kg snapshot loaded", extra={"project_id": project_id, "build_id": held.build_id})
        return held

    def _keep(self, project_id: int, held: Held) -> None:
        """Hold ``held`` as the project's snapshot, the most recently read, and drop the least recently read ones
        past HELD_PROJECTS: a notification for a dropped project is ignored, and its next read loads it again."""
        self._held.pop(project_id, None)
        self._held[project_id] = held  # one assignment: a reader sees the old snapshot or the new one
        while len(self._held) > HELD_PROJECTS:
            dropped = next(iter(self._held))
            del self._held[dropped]
            self._asked.pop(dropped, None)

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
