"""What a daemon before this one left (``daemon``), dealt with before any claim.

At the start, the events a previous daemon left in the spool are sent, and runs it left unfinished are marked ended,
their tmux sessions closed and their credential sockets removed. A previous daemon that died with an agent started
(``runs/<run>/agent.json``, ``orphans``) left it running: the first heartbeat that gets an answer also names those
runs, and before any claim the daemon stops the process group of each one's agent, SIGTERM then SIGKILL. The run's
worktree and its evo-run branch are removed when the hub no longer holds the run for this worker; a run parked, or
done because a new run resumes it, keeps its worktree for that run. A run the hub still holds keeps it too.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from evo_agents.worker import interactive, orphans, verify_jobs
from evo_agents.worker.browser import session as browser_session
from evo_agents.worker.credentials import socket_path
from evo_agents.worker.run import Sender
from evo_agents.worker.spool import Spool, leftover_runs

if TYPE_CHECKING:
    from evo_agents.worker.daemon import Daemon

log = logging.getLogger("evo_agents.worker")


class Leftovers:
    """The agents, runs, browsers and events a previous daemon of ``daemon``'s home left."""

    def __init__(self, daemon: Daemon):
        self.daemon = daemon

    async def clear(self) -> None:
        """What a daemon before this one left, before any claim: the agents it started, the runs it held, and the
        browsers of the runs here."""
        self.daemon.orphans = self._find_orphans()
        self._close_abandoned_runs()
        await browser_session.sweep(self.daemon.home)

    def _find_orphans(self) -> dict[int, dict]:
        """The runs a previous daemon left with an agent started (agent.json), and their records; before
        ``_close_abandoned_runs`` marks those records ended. A file of a run whose record says it ended is a leftover
        of a crash at the very end, and goes."""
        home = self.daemon.home
        found: dict[int, dict] = {}
        for run_id, agent in home.load_agents().items():
            record = home.load_run(run_id)
            if record is not None and record.get("finished_at"):
                with contextlib.suppress(OSError):
                    home.remove_agent(run_id)
                continue
            found[run_id] = {"agent": agent, "record": record}
        if found:
            log.warning(
                "a previous daemon left runs with their agents: the first heartbeat asks the hub about them",
                extra={"runs": sorted(found)},
            )
        return found

    async def settle_orphans(self, controls: Mapping[int, dict]) -> None:
        """Deal with the orphans the hub answered for (``controls``, by run id), each at once; the others are asked
        again at the next heartbeat."""
        left = self.daemon.orphans
        jobs = [self._settle_orphan(run_id, left.pop(run_id), control) for run_id, control in controls.items()]
        for outcome in await asyncio.gather(*jobs, return_exceptions=True):
            if isinstance(outcome, BaseException):
                log.error("an orphan of a previous daemon was not dealt with", exc_info=outcome)

    async def _settle_orphan(self, run_id: int, orphan: dict, control: dict) -> None:
        """Stop the agent of an orphan run, then remove its worktree and evo-run branch unless the hub parked the run
        (it keeps its session and worktree for the run that resumes it) or still holds it for this worker (its lease
        runs out, and its worktree ages)."""
        home = self.daemon.home
        stopped = await orphans.stop(orphan["agent"])
        with contextlib.suppress(OSError):
            await asyncio.to_thread(verify_jobs.stop_all, home.run_dir(run_id) / verify_jobs.JOBS_DIR)
        record = home.load_run(run_id) or orphan.get("record")  # as _close_abandoned_runs left it
        if control.get("held"):
            what = "kept: the hub still holds the run for this worker, which cannot go on with it; its lease runs out"
        elif control.get("park"):
            what = "kept for the run that resumes it: the hub parked the run"
            if record is not None:
                record["state"] = "parked"
                with contextlib.suppress(OSError):
                    home.save_run(record)
        else:
            what = "removed with its evo-run branch: the hub no longer holds the run for this worker"
            if record is not None:
                await self.daemon.cleaner.remove_worktree(record)
        with contextlib.suppress(OSError):
            home.remove_agent(run_id)
        log.warning(
            "the agent a previous daemon left was dealt with",
            extra={
                "run_id": run_id,
                "agent": stopped,
                "worktree": what,
                "hub_state": control.get("state"),
                "pgid": orphan["agent"].get("pgid"),
            },
        )

    def _close_abandoned_runs(self) -> None:
        """Runs a previous daemon left without an end: their leases ran out on the hub; their worktrees age now, and
        a terminal UI left running in their tmux session is closed."""
        home = self.daemon.home
        for record in home.load_runs():
            if record.get("finished_at") is None:
                with contextlib.suppress(OSError):  # its leases went with that daemon; the hub takes them back
                    socket_path(home, record["id"]).unlink(missing_ok=True)
                record["finished_at"] = datetime.now(UTC).isoformat()
                record["state"] = f"{record.get('state')} when the worker stopped"
                with contextlib.suppress(OSError):
                    home.save_run(record)
                if record.get("tmux_session"):
                    socket = record.get("tmux_socket")
                    tmux = interactive.Tmux.from_env(self.daemon.env, socket=socket) if socket else self.daemon.tmux
                    tmux.kill_sync(str(record["tmux_session"]))

    async def send_events(self) -> None:
        """Send the events a previous daemon left in the spool, of each run this daemon does not hold."""
        home = self.daemon.home
        for run_id in leftover_runs(home.spool_dir):
            if run_id in self.daemon.runs:
                continue
            spool = Spool(home.spool_dir, run_id, self.daemon.budget)
            if spool.pending:
                log.info("sending events a previous daemon left", extra={"run_id": run_id, "events": spool.pending})
                sender = Sender(self.daemon, spool)
                sender.close()
                await sender.run()
                if not sender.settled:
                    continue
            spool.remove()
