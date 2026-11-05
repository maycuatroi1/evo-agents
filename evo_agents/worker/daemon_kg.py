"""The night shift of a worker's daemon (``daemon``): keeping the graphs of the projects it is on duty for fresh.

The heartbeat answer's ``kg_sync`` names the projects whose night shift this worker is on duty for: every
``kg_sync.KG_SYNC_EVERY`` seconds, and at once when the hub took a run of one since the daemon last looked, it syncs
their git sources whose ref is origin/<branch> and pushes the runs the hub lacks with its own token (``kg_sync``), one
project at a time in a thread, under the lock of each checkout it fetches; each heartbeat says what the last look at
each project found. EVO_WORKER_KG_SYNC=off turns it off: the daemon then neither says the feature nor sends the report.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.hub import runs
from evo_agents.hub.client import Hub
from evo_agents.worker import kg_sync

if TYPE_CHECKING:
    from evo_agents.worker.daemon import Daemon

log = logging.getLogger("evo_agents.worker")


class Graphs:
    """The graphs ``daemon`` keeps fresh: the duties the hub gave it, and the loop that looks at each."""

    def __init__(self, daemon: Daemon):
        self.daemon = daemon
        self.on = kg_sync.enabled(daemon.env)
        self.every = kg_sync.every(daemon.env)
        self.sync: kg_sync.KgSync | None = None  # made once the loop runs, with the hub's URL and the token
        self.duties: dict[str, str | None] = {}  # project -> when the hub last took a run of its graph
        self.wake = asyncio.Event()  # made again as the daemon starts
        self._wrong: dict[str, tuple] = {}  # project -> what went wrong at its last look

    def report(self) -> dict | None:
        """kg_sync of the heartbeat; None before the loop runs."""
        return self.sync.report(sorted(self.duties)) if self.sync is not None else None

    def stop(self) -> None:
        if self.sync is not None:
            self.sync.stop.set()

    def take_duties(self, value) -> None:
        """The projects whose graph the hub asks this worker to keep fresh, with when it last took a run of each: a
        project new to the list, or one whose time moved, wakes the loop of ``kg_sync``."""
        found = {}
        for item in value if isinstance(value, list) else []:
            if isinstance(item, dict) and isinstance(item.get("project"), str):
                received = item.get("received_at")
                found[item["project"]] = received if isinstance(received, str) else None
        if found != self.duties:
            if set(found) != set(self.duties):
                log.info("graphs to keep fresh", extra={"projects": sorted(found)})
            self.duties = found
            self.wake.set()

    def _checkouts(self, project: str) -> dict[str, Path]:
        """The checkouts of ``project``'s repos here, by repo name."""
        prefix = f"{project}/"
        found = self.daemon.checkouts
        return {key[len(prefix) :]: Path(entry["path"]) for key, entry in found.items() if key.startswith(prefix)}

    def _lock(self, loop: asyncio.AbstractEventLoop):
        """A context manager for the thread of a look: it holds the daemon's lock of a checkout, as a run does."""
        repo_lock = self.daemon.repo_lock

        async def acquire(path: Path) -> None:
            await repo_lock(path).acquire()

        @contextlib.contextmanager
        def locked(path: Path):
            future = asyncio.run_coroutine_threadsafe(acquire(Path(path)), loop)
            while True:  # a daemon that stops no longer runs the loop: the look gives up rather than wait forever
                try:
                    future.result(timeout=1.0)
                    break
                except TimeoutError:
                    if self.sync is not None and self.sync.stop.is_set():
                        future.cancel()
                        raise RuntimeError("the daemon is stopping") from None
            try:
                yield
            finally:
                loop.call_soon_threadsafe(repo_lock(Path(path)).release)

        return locked

    async def loop(self) -> None:
        """Look at each project of kg_sync every ``every`` seconds, and at once at one whose time moved."""
        loop = asyncio.get_running_loop()
        daemon = self.daemon
        hub = Hub(daemon.config.url, daemon.token, headers={runs.PROTOCOL_HEADER: runs.PROTOCOL_VERSION})
        self.sync = kg_sync.KgSync(daemon.home.root, hub, lock=self._lock(loop), secrets=(daemon.token,))
        looked: dict[str, tuple[float, str | None]] = {}  # project -> when the daemon looked, and the time it saw
        while not daemon.stopping.is_set():
            self.wake.clear()
            for project, received in sorted(self.duties.items()):
                last = looked.get(project)
                if last is not None and last[1] == received and loop.time() - last[0] < self.every:
                    continue
                if daemon.stopping.is_set():
                    return
                looked[project] = (loop.time(), received)
                await self._look(self.sync, project)
            waits = [self.every - (loop.time() - at) for at, _ in looked.values()] or [self.every]
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.wake.wait(), max(1.0, min(waits)))

    async def _look(self, sync: kg_sync.KgSync, project: str) -> None:
        try:
            state = await asyncio.to_thread(sync.look, project, self._checkouts(project))
        except Exception:  # a bug here must not end the loop
            log.exception("keeping the graph fresh failed", extra={"project": project})
            return
        failed = {sid: found.error for sid, found in state.sources.items() if found.status == "failed"}
        wrong = (state.error, sorted(failed.items()))
        level = logging.WARNING if (state.error or failed) and wrong != self._wrong.get(project) else logging.INFO
        self._wrong[project] = wrong  # a failure that stays is warned of once, then logged at info
        log.log(
            level,
            "graph looked at",
            extra={
                "project": project,
                "error": state.error,
                "failed": failed,
                "pushed": sum(found.pushed for found in state.sources.values()),
                "pulled": sum(found.pulled for found in state.sources.values()),
            },
        )
