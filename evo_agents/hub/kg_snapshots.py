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
import json
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import tables
from evo_agents.hub.kg_graph_rows import CHANNEL, MAIN, read_graph
from evo_agents.kg import settings as kg_settings
from evo_agents.kg.snapshot import Snapshot, SnapshotView

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


class SnapshotCoordinator:
    def __init__(self, engine: AsyncEngine | None = None, listener=None):
        self.engine = engine  # the api's, set once it opened
        self._listener = listener
        self._held: dict[int, Held] = {}
        self._loading: dict[int, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()
        if listener is not None:
            listener.on(CHANNEL, self.notified)

    async def get(self, project_id: int) -> Held | None:
        """The project's snapshot, loaded on first use; None before its first build kept a graph."""
        if self._listener is not None:
            self._listener.start()  # the first claim, stream or graph read starts it
        held = self._held.get(project_id)
        if held is not None:
            return held
        return await self._load(project_id)

    async def view(
        self, project_id: int, reader: frozenset[str], ceiling: tuple[int, int] | None = None
    ) -> SnapshotView | None:
        """The project's snapshot as ``reader`` reads it (``Held.view``); None before its first build kept a graph."""
        held = await self.get(project_id)
        return None if held is None else held.view(reader, ceiling)

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
        heads = tables.kg_graph_heads
        assert self.engine is not None
        async with self.engine.connect() as conn:
            build_id = await conn.scalar(
                select(heads.c.build_id).where(heads.c.project_id == project_id, heads.c.branch_id == MAIN)
            )
            rows = await read_graph(conn, project_id)
        if rows is None:
            return None
        held = Held(build_id, await asyncio.to_thread(Snapshot.from_stored, rows))
        self._held[project_id] = held  # one assignment: a reader sees the old snapshot or the new one
        log.info("kg snapshot loaded", extra={"project_id": project_id, "build_id": build_id})
        return held

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
        for task in [*self._tasks, *self._loading.values()]:
            task.cancel()
        await asyncio.gather(*self._tasks, *self._loading.values(), return_exceptions=True)
