"""The heartbeat of a worker's daemon (``daemon``): sent at once and then every 15 seconds.

It carries the runtimes, the checkouts, the free slots, the runs the daemon holds and its version. The answer says
whether to drain (``daemon_state``), and for each run whether to cancel it, whether the hub still holds it for this
worker, how many messages of the owner wait, and the controls of a person (takeover, handback, terminal_open, inbox,
park, finish). A call the hub does not answer is sent again with a backoff from 1 to 60 seconds; a token the hub no
longer takes stops the daemon (``Daemon.fatal``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING

from evo_agents import __version__
from evo_agents.hub import runs
from evo_agents.worker.hubapi import Backoff, Outdated, Refused, Unreachable
from evo_agents.worker.run import KINDS, Run

if TYPE_CHECKING:
    from evo_agents.worker.daemon import Daemon

log = logging.getLogger("evo_agents.worker")

HEARTBEAT_VARIABLE = "EVO_WORKER_HEARTBEAT_SECONDS"  # for tests: a shorter heartbeat than the protocol's 15 s
MAX_HEARTBEAT_RUNS = 64  # run ids in one heartbeat, as the hub takes them


def heartbeat_seconds(env: Mapping[str, str]) -> float:
    try:
        value = float(env.get(HEARTBEAT_VARIABLE) or runs.HEARTBEAT_SECONDS)
    except ValueError:
        value = float(runs.HEARTBEAT_SECONDS)
    return min(max(value, 0.2), float(runs.HEARTBEAT_SECONDS))


def take_control(run: Run, control: dict) -> None:
    """What a heartbeat answer says of a run this daemon holds: park it, stop it, or pass on what its owner did."""
    if control.get("park"):  # parked, or done because a new run resumes it: not held, and not cancelled
        run.request_park()
    elif not control.get("held"):
        run.request_stop("gone")
    elif control.get("cancel"):
        run.request_stop("cancel")
    else:
        decisions = control.get("decisions")
        run.open_decisions = decisions if isinstance(decisions, int) else 0
        requests = {
            "inbox": run.inbox_waits,
            "takeover": run.request_takeover,
            "handback": run.request_handback,
            "finish": run.request_finish,
            "terminal_open": run.open_terminal,
        }
        for key, request in requests.items():
            if control.get(key):
                request()
        if run.curator is not None:  # the watchdog of the Curator's runs, after each heartbeat
            run.spawn(run.watch())


class Heartbeat:
    """The heartbeats of ``daemon``, and what their answers say of its runs."""

    def __init__(self, daemon: Daemon):
        self.daemon = daemon
        self.failures = 0

    async def loop(self) -> None:
        backoff = Backoff()
        while True:
            self.daemon.wake.turn("heartbeat")  # a sleep holds the claims, never the heartbeats of the runs held
            try:
                ok = await self.beat()
            except Exception:  # a bug here must not end the heartbeats, and with them the leases
                log.exception("heartbeat failed")
                ok = False
            if ok:
                backoff.reset()
                delay = self.daemon.heartbeat_s
            else:
                delay = min(self.daemon.heartbeat_s, backoff.next())
            await asyncio.sleep(delay)

    def _held(self) -> list[int]:
        return sorted(run_id for run_id, run in self.daemon.runs.items() if not run.ended)

    async def beat(self) -> bool:
        daemon = self.daemon
        try:
            await daemon.look_at_machine()
        except Exception:
            log.exception("looking at the runtimes and checkouts failed")
        held = self._held()[:MAX_HEARTBEAT_RUNS]
        asked = sorted(daemon.orphans)[: MAX_HEARTBEAT_RUNS - len(held)]
        answer = await self._send(held + asked)
        if answer is None:
            return False
        listed = answer.get("runs") if isinstance(answer, dict) else None
        controls = [item for item in listed or [] if isinstance(item, dict)]
        if asked:  # before any claim, so a new run never starts next to an agent a dead daemon left
            await daemon.leftovers.settle_orphans(
                {item["id"]: item for item in controls if isinstance(item.get("id"), int) and item["id"] in asked}
            )
        daemon.beat_ok.set()
        if isinstance(answer, dict):
            daemon.state.take(answer)
        for control in controls:
            run_id = control.get("id")
            run = daemon.runs.get(run_id) if isinstance(run_id, int) else None
            if run is not None and not run.ended:
                take_control(run, control)
        return True

    async def _send(self, reported: list[int]):
        """The hub's answer to a heartbeat that names the runs ``reported``; None when it was not sent or refused."""
        assert self.daemon.hub is not None  # made as the daemon starts
        try:
            answer = await self.daemon.hub.heartbeat(self.body(reported))
        except Unreachable as exc:
            self.failures += 1
            if self.failures <= 3 or self.failures % 20 == 0:
                log.warning("heartbeat not sent", extra={"failures": self.failures, "error": str(exc)})
            return None
        except Refused as exc:
            if isinstance(exc, Outdated) or exc.status in (401, 403):
                self.daemon.fatal(exc)
            else:
                log.error("heartbeat refused", extra={"status": exc.status, "error": str(exc)})
            return None
        if self.failures:
            log.info("heartbeat sent again", extra={"after_failures": self.failures})
            self.failures = 0
        return answer

    def body(self, reported: list[int]) -> dict:
        """What a heartbeat sends: the machine, the runs ``reported``, the install, what the last looks at the graphs
        found."""
        daemon = self.daemon
        body = {
            "runtimes": daemon.runtimes,
            "checkouts": daemon.checkouts,
            "free_slots": min(daemon.free_slots, runs.MAX_SLOTS),
            "runs": reported,
            "agent_version": __version__,
            # the kinds of runs.RUN_KINDS this daemon has a class for (run.KINDS): the hub hands it no other
            "run_kinds": [kind for kind in runs.RUN_KINDS if kind in KINDS],
            # a plan run with a session for each step (runner.sessions), an author run that asks decisions
            "features": [name for name in runs.WORKER_FEATURES if daemon.says(name)],
            **daemon.updater.heartbeat_fields(),  # agent_commit and self_update, every time (0048)
        }
        report = daemon.graphs.report()
        if report and report["projects"]:
            body["kg_sync"] = report  # what the last looks found (0053)
        browser = daemon.browser
        if browser.report is not None:
            body["browser"] = browser.report  # its Chromium, and how many browsers it holds at once (0119)
        if browser.asked is not None:  # where it stands with its owner's Install browser (0146)
            body["browser_install"] = {key: value for key, value in browser.asked.items() if value is not None}
        return body
