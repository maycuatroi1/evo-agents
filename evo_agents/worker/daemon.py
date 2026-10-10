"""``evo-agents worker run``: the daemon in the foreground.

- It sends a heartbeat at once and then every 15 seconds: the runtimes (``adapter.detect_runtimes``, looked at again
  every 5 minutes), the checkouts (``checkouts.discover``, every minute), the free slots, the runs it holds and its
  version. The answer says whether to drain, and for each run whether to cancel it, whether the hub still holds it
  for this worker, and how many messages of the owner wait.
- It claims a run whenever it has a free slot and is neither draining nor stopping, one long poll of up to 25
  seconds at a time, and runs each run it gets (``run.Run``) next to the others.
- The answer's ``takeover`` and ``handback`` hand a run's agent to a person in tmux and back (``interactive``), and
  ``terminal_open`` connects the worker's end of the run's web terminal. For a plan run, ``inbox`` also wakes a run
  that waits for its owner's answer, and ``park`` lets go of a run the hub parked, its session and worktrees kept;
  the same goes for an author run waiting for its owner's reply, and ``finish`` ends one whose chat the owner ended.
- While it holds at least one run, the daemon keeps the machine awake with power assertions on macOS (``power``:
  no idle sleep, and no system sleep on AC power), and lets them go when it holds none, stops or dies; elsewhere it
  logs once that it cannot and goes on.
- Each turn of the heartbeat and claim loops asks ``power.WakeWatch`` whether the machine slept since that loop's
  previous turn (the wall clock passed the monotonic one by more than 30 seconds): it logs "wake detected" with the
  gap, and the daemon claims no new run for 120 seconds after it, while the heartbeats of the runs held go on.
- A call the hub does not answer is sent again with a backoff from 1 to 60 seconds; runs go on meanwhile.
- SIGTERM or SIGINT: no new claim; the runs held go on until they end or reach their timeout, then the daemon exits
  0. A second signal stops the agents now and fails their runs.
- A token the hub no longer takes (401, 403: the worker or its token was revoked) stops the daemon with exit status
  ``home.EXIT_REVOKED`` (3), or the one EVO_WORKER_REVOKED_EXIT names: starting it again changes nothing, and the
  service tells its manager not to (``service``). A protocol the hub no longer speaks (426) stops it with exit status
  1, which an upgrade can mend.
- At the start, the events a previous daemon left in the spool are sent, and runs it left unfinished are marked
  ended, their tmux sessions closed and their credential sockets removed. At the start and every hour, the worktrees
  of runs that ended more than 7 days ago are removed.
- Each run takes its leases from the hub after the claim and gives them back when it ends here (``credentials``),
  also when the daemon stops.
- After each heartbeat, each run of the Curator the hub still holds for this worker is watched (``Run.watch``,
  ``runner.watchdog``): a file of the charter's protected paths changed in its worktrees, or its time or cost cap
  passed, stops it.
- A previous daemon that died with an agent started (``runs/<run>/agent.json``, ``orphans``) left it running. The
  first heartbeat that gets an answer also names those runs, and before any claim the daemon stops the process group
  of each one's agent, SIGTERM then SIGKILL. The run's worktree and its evo-run branch are removed when the hub no
  longer holds the run for this worker; a run parked, or done because a new run resumes it, keeps its worktree for
  that run. A run the hub still holds keeps it too: this daemon cannot go on with it, its lease runs out (a heartbeat
  after the first no longer names it), and the hub tries it again or fails it; its worktree ages as any other.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from evo_agents import __version__
from evo_agents.hub import runs
from evo_agents.worker import checkouts, gitops, interactive, orphans
from evo_agents.worker.adapter import Adapter, detect_runtimes
from evo_agents.worker.credentials import socket_path
from evo_agents.worker.home import WorkerConfig, WorkerHome, revoked_exit
from evo_agents.worker.hubapi import Backoff, HubProblem, Outdated, Refused, Unreachable, WorkerHub, new_session
from evo_agents.worker.power import KeepAwake, WakeWatch
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
MAX_HEARTBEAT_RUNS = 64  # run ids in one heartbeat, as the hub takes them


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
        keep_awake: KeepAwake | None = None,
        wake: WakeWatch | None = None,
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
        self.orphans: dict[int, dict] = {}  # run id -> {"agent": agent.json, "record": run.json}, until the hub answers
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
        self.keep_awake = keep_awake or KeepAwake(config.name)  # the power assertions, while it holds a run
        self.wake = wake or WakeWatch()  # notices a sleep from the turns of the heartbeat and claim loops

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
            self.orphans = self._find_orphans()
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
            self.keep_awake.close()
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
            self.wake.turn("heartbeat")  # a sleep holds the claims, never the heartbeats of the runs held
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
        held = self._held()[:MAX_HEARTBEAT_RUNS]
        asked = sorted(self.orphans)[: MAX_HEARTBEAT_RUNS - len(held)]
        body = {
            "runtimes": self.runtimes,
            "checkouts": self.checkouts,
            "free_slots": min(self.free_slots, 8),
            "runs": held + asked,
            "agent_version": __version__,
            "run_kinds": list(runs.RUN_KINDS),  # every kind run.KINDS has a class for
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
        listed = answer.get("runs") if isinstance(answer, dict) else None
        controls = [item for item in listed or [] if isinstance(item, dict)]
        if asked:  # before any claim, so a new run never starts next to an agent a dead daemon left
            await self._settle_orphans({item.get("id"): item for item in controls if item.get("id") in asked})
        self.beat_ok.set()
        drain = bool(answer.get("drain")) if isinstance(answer, dict) else False
        if drain != self.draining:
            log.info("draining: no new claims" if drain else "drain ended: claiming again")
            self.draining = drain
            self.slot_free.set()
        for control in controls:
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
                if control.get("finish"):
                    run.request_finish()
                if control.get("terminal_open"):
                    run.open_terminal()
                if run.curator is not None:  # the watchdog of the Curator's runs, after each heartbeat
                    run.spawn(run.watch())
        return True

    # Claims

    async def _claim_loop(self) -> None:
        await self.beat_ok.wait()
        backoff = Backoff()
        held_for_wake = False
        while not self.stopping.is_set():
            self.wake.turn("claim")
            left = self.wake.hold_left()
            if left > 0:  # the machine slept: no new run until it has been awake WAKE_HOLD_SECONDS
                if not held_for_wake:
                    held_for_wake = True
                    log.info("no claims for a while after a sleep", extra={"claims_in_s": round(left, 1)})
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.stopping.wait(), min(left, self.heartbeat_s))
                continue
            if held_for_wake:
                held_for_wake = False
                log.info("claiming again after the sleep")
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
        self.keep_awake.update(len(self.runs))
        task = asyncio.create_task(run.main())
        self.run_tasks[run.id] = task

        def ended(_task: asyncio.Task, run_id: int = run.id) -> None:
            self.runs.pop(run_id, None)
            self.run_tasks.pop(run_id, None)
            self.keep_awake.update(len(self.runs))
            self.slot_free.set()

        task.add_done_callback(ended)

    # What earlier daemons left

    def _find_orphans(self) -> dict[int, dict]:
        """The runs a previous daemon left with an agent started (agent.json), and their records; before
        ``_close_abandoned_runs`` marks those records ended. A file of a run whose record says it ended is a leftover
        of a crash at the very end, and goes."""
        found: dict[int, dict] = {}
        for run_id, agent in self.home.load_agents().items():
            record = self.home.load_run(run_id)
            if record is not None and record.get("finished_at"):
                with contextlib.suppress(OSError):
                    self.home.remove_agent(run_id)
                continue
            found[run_id] = {"agent": agent, "record": record}
        if found:
            log.warning(
                "a previous daemon left runs with their agents: the first heartbeat asks the hub about them",
                extra={"runs": sorted(found)},
            )
        return found

    async def _settle_orphans(self, controls: Mapping[int, dict]) -> None:
        """Deal with the orphans the hub answered for (``controls``, by run id), each at once; the others are asked
        again at the next heartbeat."""
        jobs = [self._settle_orphan(run_id, self.orphans.pop(run_id), control) for run_id, control in controls.items()]
        for outcome in await asyncio.gather(*jobs, return_exceptions=True):
            if isinstance(outcome, BaseException):
                log.error("an orphan of a previous daemon was not dealt with", exc_info=outcome)

    async def _settle_orphan(self, run_id: int, orphan: dict, control: dict) -> None:
        """Stop the agent of an orphan run, then remove its worktree and evo-run branch unless the hub parked the run
        (it keeps its session and worktree for the run that resumes it) or still holds it for this worker (its lease
        runs out, and its worktree ages)."""
        stopped = await orphans.stop(orphan["agent"])
        record = self.home.load_run(run_id) or orphan.get("record")  # as _close_abandoned_runs left it
        if control.get("held"):
            what = "kept: the hub still holds the run for this worker, which cannot go on with it; its lease runs out"
        elif control.get("park"):
            what = "kept for the run that resumes it: the hub parked the run"
            if record is not None:
                record["state"] = "parked"
                with contextlib.suppress(OSError):
                    self.home.save_run(record)
        else:
            what = "removed with its evo-run branch: the hub no longer holds the run for this worker"
            if record is not None:
                await self._remove_worktree(record)
        with contextlib.suppress(OSError):
            self.home.remove_agent(run_id)
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
        for record in self.home.load_runs():
            if record.get("finished_at") is None:
                with contextlib.suppress(OSError):  # its leases went with that daemon; the hub takes them back
                    socket_path(self.home, record["id"]).unlink(missing_ok=True)
                record["finished_at"] = datetime.now(UTC).isoformat()
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
        now = now or datetime.now(UTC)
        removed = []
        for record in self.home.load_runs():
            run_id = record["id"]
            if run_id in self.runs or not record.get("finished_at"):
                continue
            try:
                finished = datetime.fromisoformat(str(record["finished_at"]))
            except ValueError:
                continue
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=UTC)
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
        if record.get("kind") in ("plan", "review", "judge", "author"):
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
