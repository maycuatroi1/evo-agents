"""``evo-agents worker run``: the daemon in the foreground.

- It sends a heartbeat at once and then every 15 seconds: the runtimes (``adapter.detect_runtimes``, looked at again
  every 5 minutes), the checkouts (``checkouts.discover``, every minute), the free slots, the runs it holds and its
  version. The answer says whether to drain, and for each run whether to cancel it, whether the hub still holds it
  for this worker, and how many messages of the owner wait.
- It claims a run whenever it has a free slot and is neither draining nor stopping, one long poll of up to 25
  seconds at a time, and runs each run it gets (``run.Run``) next to the others.
- The answer's ``takeover`` and ``handback`` hand a run's agent to a person in tmux and back (``interactive``), and
  ``terminal_open`` connects the worker's end of the run's web terminal. For a plan run, ``inbox`` also wakes a run
  that waits for its owner's answer, and ``park`` lets go of a run the hub parked, its session and worktrees kept.
- A call the hub does not answer is sent again with a backoff from 1 to 60 seconds; runs go on meanwhile.
- SIGTERM or SIGINT: no new claim; the runs held go on until they end or reach their timeout, then the daemon exits
  0. A second signal stops the agents now and fails their runs.
- A token the hub no longer takes (401, 403: the worker or its token was revoked) stops the daemon with exit status
  ``home.EXIT_REVOKED`` (3), or the one EVO_WORKER_REVOKED_EXIT names: starting it again changes nothing, and the
  service tells its manager not to (``service``). A protocol the hub no longer speaks (426) stops it with exit status
  1, which an upgrade can mend.
- At the start, the events a previous daemon left in the spool are sent, and runs it left unfinished are marked
  ended, their tmux sessions closed. At the start and every hour, the worktrees of runs that ended more than 7 days
  ago are removed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evo_agents import __version__
from evo_agents.hub import runs
from evo_agents.isotime import parse_iso
from evo_agents.worker import checkouts, gitops, interactive
from evo_agents.worker.adapter import Adapter, detect_runtimes
from evo_agents.worker.home import WorkerConfig, WorkerHome, revoked_exit
from evo_agents.worker.hubapi import Backoff, HubProblem, Outdated, Refused, Unreachable, WorkerHub, new_session
from evo_agents.worker.run import Run, Sender, run_class
from evo_agents.worker.spool import Spool, SpoolBudget, leftover_runs

log = logging.getLogger("evo_agents.worker")

HEARTBEAT_VARIABLE = "EVO_WORKER_HEARTBEAT_SECONDS"  # for tests: a shorter heartbeat than the protocol's 15 s
EXIT_FAILED = 1
RUNTIMES_EVERY = 300.0  # seconds between looks at the runtimes
CHECKOUTS_EVERY = 60.0  # and at the checkouts
CLEANUP_EVERY = 3600.0
KEEP_WORKTREES = timedelta(days=7)
LEFTOVER_GRACE = 30.0  # seconds the daemon waits at its end for leftover events to go


def heartbeat_seconds(env: Mapping[str, str]) -> float:
    try:
        value = float(env.get(HEARTBEAT_VARIABLE) or runs.HEARTBEAT_SECONDS)
    except ValueError:
        value = float(runs.HEARTBEAT_SECONDS)
    return min(max(value, 0.2), float(runs.HEARTBEAT_SECONDS))


class Daemon:
    def __init__(
        self,
        home: WorkerHome,
        config: WorkerConfig,
        token: str,
        *,
        adapters: Mapping[str, type[Adapter]],
        env: Mapping[str, str] | None = None,
    ):
        self.home = home
        self.config = config
        self.token = token
        self.adapters = dict(adapters)
        self.env = dict(os.environ if env is None else env)
        self.heartbeat_s = heartbeat_seconds(self.env)
        self.budget = SpoolBudget()
        self.runs: dict[int, Run] = {}
        self.run_tasks: dict[int, asyncio.Task] = {}
        self.runtimes: dict[str, dict] = {}
        self.checkouts: dict[str, dict] = {}
        self.draining = False
        self.exit_code = 0
        self.stopped_by_hub = False
        self.hub: WorkerHub | None = None
        self.tmux = interactive.Tmux.from_env(self.env)
        self._repo_locks: dict[str, asyncio.Lock] = {}
        self._runtimes_at = float("-inf")
        self._checkouts_at = float("-inf")
        self._beat_failures = 0

    # Shared with the runs

    def checkout_for(self, project: str, repo: str) -> Path | None:
        entry = self.checkouts.get(f"{project}/{repo}")
        return Path(entry["path"]) if entry else None

    def repo_lock(self, checkout: Path) -> asyncio.Lock:
        return self._repo_locks.setdefault(str(checkout), asyncio.Lock())

    def fatal(self, problem: HubProblem) -> None:
        """The hub no longer takes this worker (a revoked worker or token, or a protocol the hub no longer speaks):
        stop everything, without reports."""
        if not self.stopped_by_hub:
            self.stopped_by_hub = True
            outdated = isinstance(problem, Outdated)
            self.exit_code = EXIT_FAILED if outdated else revoked_exit(self.env)
            log.error(
                "the hub no longer speaks this daemon's protocol; upgrade evo-agents"
                if outdated
                else "the hub no longer takes this worker: it or its token was revoked; stopping",
                extra={"status": problem.status, "error": str(problem), "exit_code": self.exit_code},
            )
        for run in self.runs.values():
            run.request_stop("gone")
        self.stopping.set()
        self.hard_stop.set()

    # The machine

    async def _look_at_machine(self, force: bool = False) -> None:
        now = asyncio.get_running_loop().time()
        if force or now - self._runtimes_at > RUNTIMES_EVERY:
            self.runtimes = await asyncio.to_thread(detect_runtimes, self.adapters)
            self._runtimes_at = now
        if force or now - self._checkouts_at > CHECKOUTS_EVERY:
            self.checkouts = await asyncio.to_thread(checkouts.discover, self.config)
            self._checkouts_at = now

    @property
    def free_slots(self) -> int:
        return max(0, self.config.slots - len(self.runs))

    # The loop

    async def run(self) -> int:
        self.stopping = asyncio.Event()
        self.hard_stop = asyncio.Event()
        self.slot_free = asyncio.Event()
        self.beat_ok = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._signal, sig)
        session = new_session()
        self.hub = WorkerHub(self.config.url, self.token, session)
        background: list[asyncio.Task] = []
        try:
            self._close_abandoned_runs()
            await self._look_at_machine(force=True)
            available = sorted(name for name, report in self.runtimes.items() if report.get("available"))
            log.info(
                "worker started",
                extra={
                    "hub": self.config.url,
                    "worker_id": self.config.worker_id,
                    "worker": self.config.name,
                    "slots": self.config.slots,
                    "runtimes": available,
                    "checkouts": sorted(self.checkouts),
                    "version": __version__,
                    "tmux": self.tmux.binary,
                },
            )
            if not self.tmux.available:
                log.warning("tmux is not on PATH: this worker takes headless runs only, with no takeover or terminal")
            if not available:
                log.warning("no runtime can take runs here; the worker claims nothing until one can")
            if not self.checkouts:
                log.warning(
                    "no checkout of the worker's projects found: add their repos to the harness registry "
                    "(`evo-agents hub registry pull`) or set checkouts in config.json",
                    extra={"projects": self.config.projects},
                )
            leftovers = asyncio.create_task(self._send_leftovers())
            heartbeat = asyncio.create_task(self._heartbeat_loop())
            claims = asyncio.create_task(self._claim_loop())
            cleanup = asyncio.create_task(self._cleanup_loop())
            background = [leftovers, heartbeat, cleanup]
            await self.stopping.wait()
            claims.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await claims
            if self.run_tasks:
                log.info("waiting for the runs held to end", extra={"runs": sorted(self.run_tasks)})
            while self.run_tasks:
                await asyncio.wait(list(self.run_tasks.values()))
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(leftovers), 0 if self.hard_stop.is_set() else LEFTOVER_GRACE)
        finally:
            for task in background:
                task.cancel()
            for task in background:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            await session.close()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)
        log.info("worker stopped", extra={"exit_code": self.exit_code})
        return self.exit_code

    def _signal(self, sig: int) -> None:
        name = signal.Signals(sig).name
        if not self.stopping.is_set():
            log.info(
                "stopping: no new claims; the runs held go on until they end or time out",
                extra={"signal": name, "runs": sorted(self.runs)},
            )
            self.stopping.set()
            return
        if not self.hard_stop.is_set():
            log.warning("stopping now: the agents are stopped and their runs fail", extra={"signal": name})
            self.hard_stop.set()
            for run in self.runs.values():
                run.request_stop("shutdown")

    # Heartbeat

    async def _heartbeat_loop(self) -> None:
        backoff = Backoff()
        while True:
            try:
                ok = await self._beat()
            except Exception:  # a bug here must not end the heartbeats, and with them the leases
                log.exception("heartbeat failed")
                ok = False
            if ok:
                backoff.reset()
                delay = self.heartbeat_s
            else:
                delay = min(self.heartbeat_s, backoff.next())
            await asyncio.sleep(delay)

    def _held(self) -> list[int]:
        return sorted(run_id for run_id, run in self.runs.items() if not run.ended)

    async def _beat(self) -> bool:
        try:
            await self._look_at_machine()
        except Exception:
            log.exception("looking at the runtimes and checkouts failed")
        body = {
            "runtimes": self.runtimes,
            "checkouts": self.checkouts,
            "free_slots": min(self.free_slots, 8),
            "runs": self._held()[:64],
            "agent_version": __version__,
        }
        try:
            answer = await self.hub.heartbeat(body)
        except Unreachable as exc:
            self._beat_failures += 1
            if self._beat_failures <= 3 or self._beat_failures % 20 == 0:
                log.warning("heartbeat not sent", extra={"failures": self._beat_failures, "error": str(exc)})
            return False
        except Refused as exc:
            if isinstance(exc, Outdated) or exc.status in (401, 403):
                self.fatal(exc)
            else:
                log.error("heartbeat refused", extra={"status": exc.status, "error": str(exc)})
            return False
        if self._beat_failures:
            log.info("heartbeat sent again", extra={"after_failures": self._beat_failures})
            self._beat_failures = 0
        self.beat_ok.set()
        drain = bool(answer.get("drain")) if isinstance(answer, dict) else False
        if drain != self.draining:
            log.info("draining: no new claims" if drain else "drain ended: claiming again")
            self.draining = drain
            self.slot_free.set()
        for control in (answer.get("runs") if isinstance(answer, dict) else None) or []:
            run = self.runs.get(control.get("id"))
            if run is None or run.ended:
                continue
            if control.get("park"):  # parked, or done because a new run resumes it: not held, and not cancelled
                run.request_park()
            elif not control.get("held"):
                run.request_stop("gone")
            elif control.get("cancel"):
                run.request_stop("cancel")
            else:
                decisions = control.get("decisions")
                run.open_decisions = decisions if isinstance(decisions, int) else 0
                if control.get("inbox"):
                    run.inbox_waits()
                if control.get("takeover"):
                    run.request_takeover()
                if control.get("handback"):
                    run.request_handback()
                if control.get("terminal_open"):
                    run.open_terminal()
        return True

    # Claims

    async def _claim_loop(self) -> None:
        await self.beat_ok.wait()
        backoff = Backoff()
        while not self.stopping.is_set():
            if self.draining or self.free_slots == 0:
                self.slot_free.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.slot_free.wait(), self.heartbeat_s)
                continue
            try:
                spec = await self.hub.claim()
            except Unreachable as exc:
                delay = exc.retry_after if exc.retry_after is not None else backoff.next()
                log.warning("claim failed", extra={"retry_in_s": round(delay, 1), "error": str(exc)})
                await asyncio.sleep(delay)
                continue
            except Refused as exc:
                if isinstance(exc, Outdated) or exc.status in (401, 403):
                    self.fatal(exc)
                    return
                delay = backoff.next()
                log.error("claim refused", extra={"status": exc.status, "error": str(exc)})
                await asyncio.sleep(delay)
                continue
            except Exception:  # a bug here must not end the claims for the daemon's life
                log.exception("claim failed")
                await asyncio.sleep(backoff.next())
                continue
            backoff.reset()
            if spec:
                try:
                    self._start(spec)
                except Exception:
                    log.exception("the claimed run could not start", extra={"run_id": spec.get("id")})
                    await self._refuse(spec)

    async def _refuse(self, spec: dict) -> None:
        """Fail a claimed run the daemon cannot even start, so it does not wait for its lease to run out."""
        with contextlib.suppress(HubProblem, KeyError, TypeError, ValueError):
            error = f"the worker {self.config.name} could not start the run"
            await self.hub.report(int(spec["id"]), {"state": "failed", "error": error})

    def _start(self, spec: dict) -> None:
        run = run_class(spec)(self, spec)
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
        self.runs[run.id] = run
        task = asyncio.create_task(run.main())
        self.run_tasks[run.id] = task

        def ended(_task: asyncio.Task, run_id: int = run.id) -> None:
            self.runs.pop(run_id, None)
            self.run_tasks.pop(run_id, None)
            self.slot_free.set()

        task.add_done_callback(ended)

    # What earlier daemons left

    def _close_abandoned_runs(self) -> None:
        """Runs a previous daemon left without an end: their leases ran out on the hub; their worktrees age now, and
        a terminal UI left running in their tmux session is closed."""
        for record in self.home.load_runs():
            if record.get("finished_at") is None:
                record["finished_at"] = datetime.now(timezone.utc).isoformat()
                record["state"] = f"{record.get('state')} when the worker stopped"
                with contextlib.suppress(OSError):
                    self.home.save_run(record)
                if record.get("tmux_session"):
                    socket = record.get("tmux_socket")
                    tmux = interactive.Tmux.from_env(self.env, socket=socket) if socket else self.tmux
                    tmux.kill_sync(str(record["tmux_session"]))

    async def _send_leftovers(self) -> None:
        for run_id in leftover_runs(self.home.spool_dir):
            if run_id in self.runs:
                continue
            spool = Spool(self.home.spool_dir, run_id, self.budget)
            if spool.pending:
                log.info("sending events a previous daemon left", extra={"run_id": run_id, "events": spool.pending})
                sender = Sender(self, spool)
                sender.close()
                await sender.run()
                if not sender.settled:
                    continue
            spool.remove()

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                await self.cleanup()
            except Exception:
                log.exception("cleaning up old worktrees failed")
            await asyncio.sleep(CLEANUP_EVERY)

    async def cleanup(self, now: datetime | None = None) -> list[int]:
        """Remove the worktrees and records of runs that ended more than 7 days ago; their ids."""
        now = now or datetime.now(timezone.utc)
        removed = []
        for record in self.home.load_runs():
            run_id = record["id"]
            if run_id in self.runs or not record.get("finished_at"):
                continue
            try:
                finished = parse_iso(str(record["finished_at"]))
            except ValueError:
                continue
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=timezone.utc)
            if now - finished < KEEP_WORKTREES:
                continue
            await self._remove_worktree(record)
            self.home.remove_run(run_id)
            removed.append(run_id)
            log.info("old worktree removed", extra={"run_id": run_id, "worktree": record.get("worktree")})
        return removed

    async def _remove_worktree(self, record: dict) -> None:
        """Remove a run's worktree and its evo-run branch; for a plan run, each repo's, then the run's directory. A
        plan run whose worktrees a resumed run took over names none of them any more."""
        if record.get("kind") == "plan":
            for item in record.get("repos") or []:
                if isinstance(item, dict):
                    await self._remove_one(item)
            await self._remove_one({"worktree": record.get("dir")})
        else:
            await self._remove_one(record)

    async def _remove_one(self, item: dict) -> None:
        path = Path(item["worktree"]) if item.get("worktree") else None
        checkout = Path(item["checkout"]) if item.get("checkout") else None
        if checkout is not None and checkout.is_dir():
            async with self.repo_lock(checkout):
                if path is not None and path.exists():
                    with contextlib.suppress(gitops.GitError):
                        await gitops.remove_worktree(checkout, path)
                await gitops.prune_worktrees(checkout)
                branch = item.get("local_branch")
                if isinstance(branch, str) and branch.startswith("evo-run/"):
                    await gitops.delete_branch(checkout, branch)
        if path is not None and path.exists() and path.resolve().is_relative_to(self.home.worktrees_dir.resolve()):
            shutil.rmtree(path, ignore_errors=True)
