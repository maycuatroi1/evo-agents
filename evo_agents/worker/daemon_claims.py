"""The claims of a worker's daemon (``daemon``): a run whenever it has a free slot and is neither draining nor
stopping, one long poll of up to 25 seconds at a time; each run it gets (``run.Run``) runs next to the others.

It claims no new run for a while after the machine slept (``power.WakeWatch``), nor while another daemon of its
install updates it, nor once it changed under it (``selfupdate``), nor while the drive of its worktrees stays under
``disk_reserve_gb`` once the cleanup made what room it may (``daemon_cleanup``). A claimed run it cannot even start
is failed at once, so it does not wait for its lease to run out.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from evo_agents.worker.background import spawn
from evo_agents.worker.hubapi import Backoff, HubProblem, Outdated, Refused, Unreachable
from evo_agents.worker.run import run_class

if TYPE_CHECKING:
    from evo_agents.worker.daemon import Daemon

log = logging.getLogger("evo_agents.worker")


class Claims:
    """The claim loop of ``daemon``, and the start and end of each run it claims."""

    def __init__(self, daemon: Daemon):
        self.daemon = daemon
        self.claiming = False
        self._held_for_wake = False
        self._held_for_disk = False

    async def loop(self) -> None:
        daemon = self.daemon
        await daemon.beat_ok.wait()
        backoff = Backoff()
        while not daemon.stopping.is_set():
            if await self._held():
                continue
            self.claiming = True
            try:
                spec, delay = await self._claim(backoff)
                if spec:
                    try:
                        self._start(spec)
                    except Exception:
                        log.exception("the claimed run could not start", extra={"run_id": spec.get("id")})
                        await self._refuse(spec)
            finally:
                self.claiming = False
                self.install_idle()
            if delay:
                await asyncio.sleep(delay)

    async def _held(self) -> bool:
        """Whether this turn claims nothing, after it waited: the machine slept, the daemon drains or holds as many
        runs as it has slots, or its install updates."""
        daemon = self.daemon
        daemon.wake.turn("claim")
        left = daemon.wake.hold_left()
        if left > 0:  # the machine slept: no new run until it has been awake WAKE_HOLD_SECONDS
            if not self._held_for_wake:
                self._held_for_wake = True
                log.info("no claims for a while after a sleep", extra={"claims_in_s": round(left, 1)})
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(daemon.stopping.wait(), min(left, daemon.heartbeat_s))
            return True
        if self._held_for_wake:
            self._held_for_wake = False
            log.info("claiming again after the sleep")
        if daemon.draining or daemon.free_slots == 0:
            daemon.slot_free.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(daemon.slot_free.wait(), daemon.heartbeat_s)
            return True
        if not daemon.updater.may_claim():  # another daemon of the install updates it, or this one restarts on it
            self.install_idle()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(daemon.stopping.wait(), daemon.heartbeat_s)
            return True
        if await self._short_of_room():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(daemon.stopping.wait(), daemon.heartbeat_s)
            return True
        return False

    async def _short_of_room(self) -> bool:
        """Whether the drive of the worktrees stays under ``disk_reserve_gb`` once the cleanup removed the worktrees
        of the ended runs it may (``Cleanup.make_room``, run under the reserve, and over it short of its headroom
        unless it stalled there): no run is claimed then, so none fails at its checkout. Said in the log once each
        time it starts and ends."""
        cleaner = self.daemon.cleaner
        if not cleaner.short_of_room() and (cleaner.stalled or not cleaner.under_target()):
            short = False
        else:
            try:
                await cleaner.make_room()
            except Exception:
                log.exception("making room on the worktrees' drive failed")
            short = cleaner.short_of_room()
        if short != self._held_for_disk:
            self._held_for_disk = short
            reserve = cleaner.reserve_gb
            if short:
                log.warning(
                    "no claims: the worktrees' drive is under its reserve",
                    extra={"free_gb": cleaner.free_gb(), "reserve_gb": reserve},
                )
            else:
                log.info("claiming again: the worktrees' drive is over its reserve", extra={"reserve_gb": reserve})
        return short

    async def _claim(self, backoff: Backoff) -> tuple[dict | None, float]:
        """One claim: the run it got, if any, and how long to wait before the next one."""
        assert self.daemon.hub is not None  # made as the daemon starts
        try:
            spec = await self.daemon.hub.claim()
        except Unreachable as exc:
            delay = exc.retry_after if exc.retry_after is not None else backoff.next()
            log.warning("claim failed", extra={"retry_in_s": round(delay, 1), "error": str(exc)})
            return None, delay
        except Refused as exc:
            if isinstance(exc, Outdated) or exc.status in (401, 403):
                self.daemon.fatal(exc)  # sets stopping, which ends the claim loop
                return None, 0.0
            delay = backoff.next()
            log.error("claim refused", extra={"status": exc.status, "error": str(exc)})
            return None, delay
        except Exception:  # a bug here must not end the claims for the daemon's life
            log.exception("claim failed")
            return None, backoff.next()
        backoff.reset()
        return spec, 0.0

    def install_idle(self) -> None:
        """Let the other daemons of the install update it, once this one neither claims nor holds a run."""
        if not self.daemon.runs and not self.claiming:
            self.daemon.updater.idle()

    async def _refuse(self, spec: dict) -> None:
        """Fail a claimed run the daemon cannot even start, so it does not wait for its lease to run out."""
        assert self.daemon.hub is not None
        with contextlib.suppress(HubProblem, KeyError, TypeError, ValueError):
            error = f"the worker {self.daemon.config.name} could not start the run"
            await self.daemon.hub.report(int(spec["id"]), {"state": "failed", "error": error})

    def _start(self, spec: dict) -> None:
        daemon = self.daemon
        run = run_class(spec)(daemon, spec)
        log.info(
            "run claimed",
            extra={
                "run_id": run.id,
                "project": run.project,
                "plan_id": spec.get("plan_id"),
                "step": spec.get("step_key"),
                "kind": spec.get("kind") or "step",
                "runtime": run.runtime,
                "repo": run.repo,
                "repos": [entry.get("repo") for entry in spec.get("repos") or [] if isinstance(entry, dict)] or None,
                "branch": run.branch,
            },
        )
        daemon.runs[run.id] = run
        daemon.keep_awake.update(len(daemon.runs))
        task = spawn(run.main(), f"run-{run.id}")
        daemon.run_tasks[run.id] = task

        def ended(_task: asyncio.Task, run_id: int = run.id) -> None:
            daemon.runs.pop(run_id, None)
            daemon.run_tasks.pop(run_id, None)
            daemon.keep_awake.update(len(daemon.runs))
            self.install_idle()
            daemon.slot_free.set()

        task.add_done_callback(ended)
