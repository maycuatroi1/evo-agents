"""A run on this worker: what every kind of run shares (``Run``), and what a kind brings to it (``RunKind``).

The daemon holds a ``Run`` for each run it claimed. The run's kind (``kinds.KINDS``: step, plan, review, judge, author)
is a ``RunKind`` the Run composes, never a subclass: it gets the Run in its constructor and works through the parts
the Run holds, which every kind shares: its log (``event``, ``note``) and the events on their way to the hub
(``sender``), its leases (``credentials``), its agent (``agent``), the owner's messages (``inbox``), its reports
(``reports``) and its budget (``watchdog``). The git, the preflight, the verify commands and the commit are functions
of the modules ``origin``, ``access``, ``verify`` and ``push``, which take the Run.

A hub that does not answer holds nothing up: events wait in the spool and each report is sent again with the backoff
until the hub answers, or says the run is no longer this worker's. A cancel, the run's timeout (counted from the
agent's start), or the hub no longer holding the run for this worker stop it (``Stopped``); a check that does not
pass fails it (``RunFailed``); both end it with a report (``transitions.stop_ending``, ``transitions.failure_ending``).
Once it is over here, its leases go back, its events are sent, and its worktrees leave the plan's branches.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from evo_agents.hub import curator
from evo_agents.worker import credentials
from evo_agents.worker.adapter import Outcome
from evo_agents.worker.credentials import RunCredentials
from evo_agents.worker.runner.agent import Agent
from evo_agents.worker.runner.common import RunFailed, RunGone, Stopped, log, now
from evo_agents.worker.runner.inbox import Inbox
from evo_agents.worker.runner.kinds import kind_class
from evo_agents.worker.runner.reports import LOG_LIMIT, Reports
from evo_agents.worker.runner.sender import Sender
from evo_agents.worker.runner.transitions import failure_ending, run_record, stop_ending
from evo_agents.worker.runner.watchdog import Watchdog
from evo_agents.worker.spool import Spool, encode_event

if TYPE_CHECKING:
    from evo_agents.worker.daemon import Daemon

SENDER_GRACE = 30.0  # seconds the events left have to reach the hub once the run is over here


class RunKind(Protocol):
    """What a kind of run brings to the Run that holds it, which it gets in its constructor: its steps from the claim
    to the report that ends it, and what the parts every kind shares ask of it."""

    parks: bool  # it waits for its owner between turns, and the hub may park it: a plan run, an author run
    chat: bool  # its owner ends its chat: an author run

    async def steps(self) -> None:
        """From the claim to the report that ends the run; RunFailed or Stopped ends it otherwise."""

    def watched(self) -> list[tuple[str, Path, str]]:
        """(repo, worktree, the commit it started at) of each worktree of the run, for the watchdog."""

    async def diff(self) -> bytes:
        """The diff of the run's commits, uploaded once it ended; empty for none."""

    async def release_branch(self) -> None:
        """Leave the plan's branches once the run is over, so they can be checked out elsewhere."""


class Run:
    """One run on this worker, from the claim to its last report: its state and log, what the heartbeat asks of it,
    the parts every kind shares, and its kind (``kind``)."""

    def __init__(self, daemon: Daemon, spec: dict):
        self.daemon = daemon
        self.spec = spec
        self.id = int(spec["id"])
        self.project = spec["project"]
        self.repo = spec["repo"]
        self.branch = spec.get("branch")
        self.runtime = spec["runtime"]
        self.mode = spec.get("mode") or "headless"
        self.approval = spec.get("approval") or "review"
        self.timeout_s = int(spec.get("timeout_min") or 60) * 60
        self.title = ""  # its kind names it
        self.state = "leased"
        self.ended = False
        self.stop_reason: str | None = None
        self.stop_event = asyncio.Event()
        self.loop = asyncio.get_running_loop()
        self.deadline = self.loop.time() + self.timeout_s  # from the claim until the agent starts, then from there
        self.agent_started = False  # the agent (headless or in a terminal) has started once: the timeout counts
        self.park_asked = asyncio.Event()  # the hub parked a run of a kind that parks, or a new run resumes it
        self.parked = False
        self.inbox_arrived = asyncio.Event()  # the heartbeat counted messages of the owner in the inbox
        self.finish_asked = asyncio.Event()  # the owner ended the chat of an author run: end it done after the turn
        self.open_decisions = 0  # the run's decisions still open, as the last heartbeat counted them
        self.worktree: Path | None = None  # the agent's directory
        self.verify: list[dict] = []
        self.summary: str | None = None
        self.outcome: Outcome | None = None
        found = spec.get("curator")
        self.curator: dict | None = found if isinstance(found, dict) else None  # a run of the Curator: its role
        # A judge run's own key: in this object alone, out of the spec its adapter and records see.
        self.judge_key: str | None = self.curator.pop("judge_key", None) if self.curator is not None else None
        self.unsupported_noted: set[str] = set()
        self._background: set[asyncio.Task] = set()
        home = daemon.home
        home.run_dir(self.id).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.log_path = home.run_dir(self.id) / "events.jsonl"
        self.log_bytes = 0
        self.spool = Spool(home.spool_dir, self.id, daemon.budget)
        self.sender = Sender(daemon, self.spool)
        self.watchdog = Watchdog(self, curator.budget_of(spec) or {})
        self.credentials = RunCredentials(self.id, home, daemon.env, self.note, guarded=self.curator is not None)
        self.agent = Agent(self)
        self.inbox = Inbox(self)
        self.reports = Reports(self)
        self.record = run_record(spec, now().isoformat(), self.curator)
        home.save_run(self.record)
        self.kind: RunKind = kind_class(spec)(self)

    # Events

    def event(self, kind: str, body: dict, at: datetime | None = None) -> None:
        """Spool an event of the run and write it to its log, every lease value masked."""
        at = at or now()
        self.watchdog.saw(kind, body)  # the session's running cost, for the watchdog of a run of the Curator
        body = credentials.scrub(body)
        seq = self.spool.append(kind, body, at)
        if seq is not None:
            self.sender.poke()
        if self.log_bytes < LOG_LIMIT:
            line = encode_event(seq or 0, kind, body, at)
            with contextlib.suppress(OSError), open(self.log_path, "ab") as handle:
                handle.write(line)
            self.log_bytes += len(line)

    def note(self, text: str, **extra) -> None:
        """A ``system`` event: what the daemon does with the run."""
        self.event("system", {"text": text, **extra})

    # Control from the heartbeat and the daemon

    def request_stop(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason
            log.info("run asked to stop", extra={"run_id": self.id, "reason": reason})
        self.stop_event.set()

    def check(self) -> None:
        """Stopped when the run was asked to stop or ran out of time."""
        if self.stop_reason is not None:
            raise Stopped(self.stop_reason)
        if self.loop.time() > self.deadline:
            self.stop_reason = "timeout"
            raise Stopped("timeout")

    def spawn(self, coro) -> None:
        """Run ``coro`` next to the run, cancelled once the run is over here."""
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def unsupported(self, what: str, why: str) -> None:
        """The owner asked for something this worker cannot do: say so once in the run's log."""
        if what in self.unsupported_noted:
            return
        self.unsupported_noted.add(what)
        self.note(f"The owner asked for a {what}; this worker cannot do it ({why}), so the run goes on.")

    def request_takeover(self) -> None:
        """The heartbeat says the owner asked to drive the agent in a terminal (``Agent.request_takeover``)."""
        self.agent.request_takeover()

    def request_handback(self) -> None:
        """The heartbeat says the owner asked to let the agent go on headless (``Agent.request_handback``)."""
        self.agent.request_handback()

    def open_terminal(self) -> None:
        """The heartbeat says a browser waits for the run's terminal (``Terminal.open_web``)."""
        self.agent.terminal.open_web()

    def inbox_waits(self) -> None:
        """The heartbeat counts messages of the owner in the inbox: hand them to the agent, and wake a run that waits
        for its owner's answer."""
        self.inbox_arrived.set()
        self.spawn(self.inbox.deliver())

    def request_park(self) -> None:
        """The heartbeat says the hub parked the run, or that a new run resumes it. A run of a kind that parks stops
        its agent at the end of its turn, and keeps its session and worktrees; another run is no longer this
        worker's."""
        if not self.kind.parks:
            self.request_stop("gone")
            return
        if self.ended or self.park_asked.is_set():
            return
        agent = self.agent
        log.info("park asked", extra={"run_id": self.id, "phase": agent.phase})
        self.park_asked.set()
        if agent.running and agent.adapter is not None:
            self.note("The hub parked the run: the agent stops at the end of its turn.")
            self.spawn(agent.stop_at_boundary(agent.adapter))
        if agent.phase == "interactive":
            agent.handback_asked.set()

    def park_requested(self) -> bool:
        return self.kind.parks and self.park_asked.is_set()

    def request_finish(self) -> None:
        """The heartbeat says the owner ended the chat of an author run: it ends done once the agent's turn is over.
        No other run has a chat."""
        if not self.kind.chat or self.ended or self.finish_asked.is_set():
            return
        log.info("finish asked", extra={"run_id": self.id, "phase": self.agent.phase})
        self.finish_asked.set()
        self.note("The owner ended the chat: the run ends done once the agent's turn is over.")

    async def watch(self) -> str | None:
        """For a run of the Curator, after a heartbeat: the watchdog (``Watchdog.watch``) over its worktrees."""
        return await self.watchdog.watch(self.kind.watched())

    # Time

    def agent_starts(self) -> None:
        """The run's timeout counts from the first start of its agent."""
        if not self.agent_started:
            self.agent_started = True
            self.deadline = self.loop.time() + self.timeout_s

    def agent_seconds(self) -> float:
        """The agent time the run has used, as its timeout counts it: none before its agent first started."""
        if not self.agent_started:
            return 0.0
        return max(self.timeout_s - max(self.deadline - self.loop.time(), 0.0), 0.0)

    # The run

    async def main(self) -> None:
        sender = asyncio.create_task(self.sender.run())
        try:
            await self._go()
            if self.ended:
                await self.reports.upload(await self.kind.diff())
        except RunGone as exc:
            log.warning(
                "run left: the hub no longer takes it from this worker", extra={"run_id": self.id, "error": str(exc)}
            )
            await self.agent.interrupt()
        except Exception:
            log.exception("the worker failed while running", extra={"run_id": self.id})
            await self.agent.interrupt()
            with contextlib.suppress(RunGone):
                error = f"the worker on {self.daemon.config.name} failed while running"
                await self.reports.end("failed", error=error, failure_cause="worker_stopped")
        finally:
            await self._wind_down(sender)

    async def _go(self) -> None:
        try:
            await self.kind.steps()
        except RunFailed as exc:
            ending = failure_ending(exc)
            self.note(ending.note, **ending.extra)
            await self.reports.end(ending.state, **ending.fields)
        except Stopped as exc:
            await self._stopped(exc.reason)

    async def _stopped(self, reason: str) -> None:
        await self.agent.interrupt()
        ending = stop_ending(
            reason,
            timeout_s=self.timeout_s,
            state=self.state,
            worker=self.daemon.config.name,
            watchdog=self.watchdog.reason,
            verify=self.verify,
        )
        if ending is not None:
            self.note(ending.note, **ending.extra)
            await self.reports.end(ending.state, **ending.fields)

    async def _wind_down(self, sender: asyncio.Task) -> None:
        """The run is over here: it ended, was parked, or the daemon stops. Its leases go back, its events go to the
        hub, and its worktrees leave the plan's branches."""
        for task in list(self._background):
            task.cancel()
        await self.agent.terminal.close_web()
        try:
            await self.credentials.release(stop=self.daemon.hard_stop)
        except Exception:
            log.exception("the run's leases were not given back", extra={"run_id": self.id})
        self.sender.close()
        if not self.daemon.hard_stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(sender), SENDER_GRACE)
        if not sender.done():
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender
        if self.sender.settled:
            self.spool.remove()
        # The agent is over: before the run counts as ended here, so a daemon that dies in between leaves an
        # abandoned run rather than an orphan whose worktree the next one would remove.
        with contextlib.suppress(OSError):
            self.daemon.home.remove_agent(self.id)
        self.record["finished_at"] = now().isoformat()
        self.record["state"] = self.state
        with contextlib.suppress(OSError):
            self.daemon.home.save_run(self.record)
        await self.kind.release_branch()
