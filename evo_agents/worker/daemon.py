"""``evo-agents worker run``: the daemon in the foreground.

- It sends a heartbeat at once and then every 15 seconds: the runtimes (``adapter.detect_runtimes``, looked at again
  every 5 minutes), the checkouts (``checkouts.discover``, every minute), the free slots, the runs it holds and its
  version. The answer says whether to drain, and for each run whether to cancel it, whether the hub still holds it
  for this worker, and how many messages of the owner wait.
- The answer also carries the worker's settings as the hub holds them (slots, name, labels, plugins), which its owner
  may change from the web. When they differ from config.json, the daemon takes them in memory, writes config.json
  again (the other fields as they are on disk) and logs the old and new values: more slots are claimed at the next
  turn of the claim loop, and fewer stop no run, the daemon only claims nothing until it holds fewer runs than the new
  slots. An answer without settings (a hub before schema 0026) changes nothing, and one without plugins (a hub before
  0039) leaves them.
- The daemon says it runs the runtimes of plugins (``runs.PLUGIN_RUNTIMES``), so the answer carries the declarations
  of the runtimes of the plugins its owner accepted (``plugin_runtimes``): each one, of a plugin of config.json's
  plugins, gets its adapter of ACP (``acp.adapter_class``), and the next heartbeat looks for their programs and
  reports them beside the core's runtimes; the hub hands a run of one only to a worker that reports it available.
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
- A token the hub no longer takes (its own 401 or 403: the worker or its token was revoked; a proxy's 401 or 403 is
  an ``Unreachable`` sent again with the backoff) stops the daemon with exit status
  ``home.EXIT_REVOKED`` (3), or the one EVO_WORKER_REVOKED_EXIT names: starting it again changes nothing, and the
  service tells its manager not to (``service``). A protocol the hub no longer speaks (426) stops it with exit status
  1, which an upgrade can mend; once its runs ended, it first updates itself to the tag nightly when it may and the
  lock the hub sent last (``version_locked`` in config.json) says unlocked, and then exits ``home.EXIT_UPDATED``.
- Each heartbeat says the commit its install was built from (``agent_commit``) and what it found of updating itself
  to the tag nightly (``self_update``), and each turn of its loop of self-update (every heartbeat period) decides it
  (``selfupdate``): a daemon that launchd or systemd runs from a uv tool install of evo-ak, whose worker the hub says
  is not locked, installs the commit nightly points to once no daemon of the install holds a run or has processes in
  its systemd service that a run's line may still need (``selfupdate.detached``, which moves those of a parked line
  into a scope of their own), and exits ``home.EXIT_UPDATED`` for its manager to
  start the new one. It claims nothing while another daemon of its install updates it, nor once it changed under it. The lock the settings of the answer carry is kept in config.json. The
  answer's ``nightly`` (the commit the hub read the tag at, once a minute) spares the daemon its own hourly read, and
  its ``update_now`` (its owner's Update now) stops the claims of every daemon of the install until it updated, while
  the runs held go on to their end (``runs.UPDATE_NOW``).
- At the start, the events a previous daemon left in the spool are sent, and runs it left unfinished are marked
  ended, their tmux sessions closed and their credential sockets removed. At the start and every hour, the cleanup
  asks the hub which ended runs whose worktrees it keeps had their work merged (POST /v1/worker/merged: the plan marks
  their repos merged, the Curator's change or the merge request of the queue merged, or GitHub merged a task run's pull
  requests) and removes those at once, after it committed what each worktree has not to a ref
  ``refs/evo-recovery/run-<id>/<folder>`` of its checkout and noted it in recovery.jsonl; a run it holds, a parked
  run, one another run went on from and a directory a run not ended uses stay. The worktrees of runs that ended more
  than 7 days ago go whatever the hub says. Only the evo-run branch of a run is deleted with its worktree.
- Each run takes its leases from the hub after the claim and gives them back when it ends here (``credentials``),
  also when the daemon stops.
- The answer's ``kg_sync`` names the projects whose night shift this worker is on duty for: every
  ``kg_sync.KG_SYNC_EVERY`` seconds, and at once when the hub took a run of one since the daemon last looked, it
  syncs their git sources whose ref is origin/<branch> and pushes the runs the hub lacks with its own token
  (``kg_sync``), one project at a time in a thread, under the lock of each checkout it fetches; each heartbeat says what
  the last look at each project found (``kg_sync``). EVO_WORKER_KG_SYNC=off turns it off: the daemon then neither says
  the feature nor sends the report.
- When it starts and every 24 hours it runs the checks of ``worker doctor`` in a thread and sends the findings, each
  detail redacted, to the hub (``doctor_report``, feature doctor_report): a worker narrowed to units of the org chart
  needs a fresh report with no high finding.
- A daemon of a worker pool node (labels with ``pool=<name>``) writes the hub's global skills its owner reads into the
  skills directories of its runtimes when it starts and every 6 hours, with its worker token and no ``hub login``
  (``skill_sync_loop``, feature skill_sync), each with its ``.evo-hub.json`` marker.
- After each heartbeat, each run of the Curator the hub still holds for this worker is watched (``Run.watch``,
  ``runner.watchdog``): a file of the charter's protected paths changed in its worktrees, or its time or cost cap
  passed, stops it.
- A daemon whose machine ran `evo-agents worker browser install` (``browser.install``, read again with the runtimes)
  says the feature browser and its Chromium in each heartbeat, and the hub hands it the runs dispatched with browser,
  no more at once than the max it says. Each such run gets a Chromium of its own before its first agent
  (``browser.session``), which goes once the run ends here or is parked; at its start the daemon stops the browsers a
  daemon before it left, and removes their directories.
- The answer's ``browser_install`` (its owner's Install browser, feature browser_install) has the daemon install
  Chromium as that command does, in a thread next to the runs it holds, claims going on: once it is in, the next
  heartbeat says the feature browser, which ends the request on the hub; an install that fails says why in the
  heartbeat's ``browser_install`` until the hub ends the request. A request comes once per daemon: a new one, or the
  same one after a restart, installs again, which changes nothing once Chromium is there.
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
import signal
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from evo_agents import __version__
from evo_agents.guard_check import ENV as GUARD_ENV
from evo_agents.hub import runs
from evo_agents.plugins.runtimes import AcpRuntime
from evo_agents.worker import checkouts, doctor_report, guard, health, interactive, skill_sync_loop
from evo_agents.worker.adapter import Adapter, detect_adapters, detect_runtimes
from evo_agents.worker.background import spawn
from evo_agents.worker.browser import session as browser_session
from evo_agents.worker.daemon_browser import BrowserInstall
from evo_agents.worker.daemon_claims import Claims
from evo_agents.worker.daemon_cleanup import Cleanup
from evo_agents.worker.daemon_heartbeat import Heartbeat, heartbeat_seconds
from evo_agents.worker.daemon_kg import Graphs
from evo_agents.worker.daemon_leftovers import Leftovers
from evo_agents.worker.daemon_state import WorkerState
from evo_agents.worker.daemon_updates import Updates
from evo_agents.worker.home import WorkerConfig, WorkerHome, revoked_exit
from evo_agents.worker.hubapi import HubProblem, Outdated, WorkerHub, new_session
from evo_agents.worker.power import KeepAwake, WakeWatch
from evo_agents.worker.run import Run
from evo_agents.worker.selfupdate.updater import SelfUpdate
from evo_agents.worker.spool import SpoolBudget

log = logging.getLogger("evo_agents.worker")

EXIT_FAILED = 1
RUNTIMES_EVERY = 300.0  # seconds between looks at the runtimes
CHECKOUTS_EVERY = 60.0  # and at the checkouts
LEFTOVER_GRACE = 30.0  # seconds the daemon waits at its end for leftover events to go


class Daemon:
    """The daemon of a worker: the state its parts and its runs share, and the loop that runs those parts. Each part
    is a component of its own: the heartbeat (``daemon_heartbeat``) and what its answers say of the worker
    (``daemon_state``), the claims (``daemon_claims``), what an earlier daemon left (``daemon_leftovers``), the cleanup
    (``daemon_cleanup``), the night shift (``daemon_kg``), the browser install (``daemon_browser``) and the
    self-update (``daemon_updates``). A run sees the daemon through ``run_host.RunHost``."""

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
        self_update: SelfUpdate | None = None,
    ):
        self.home = home
        self.config = config
        self.token = token
        self.adapters = dict(adapters)
        self.env = dict(os.environ if env is None else env)
        self.env[GUARD_ENV] = str(guard.policy_path(home.root))  # the hub's private names, kept by guard.heard
        self.heartbeat_s = heartbeat_seconds(self.env)
        self.budget = SpoolBudget()
        self.runs: dict[int, Run] = {}
        self.run_tasks: dict[int, asyncio.Task] = {}
        self.orphans: dict[int, dict] = {}  # run id -> {"agent": agent.json, "record": run.json}, until the hub answers
        self.runtimes: dict[str, dict] = {}
        self.logins: dict[str, str] = {}  # each runtime's login, found with the runtimes (``health``)
        self.health: dict | None = None  # the heartbeat's health, from the last look; None before the first
        self.plugin_runtimes: dict[str, AcpRuntime] = {}  # the declarations the hub sent, by runtime
        self.plugin_adapters: dict[str, type[Adapter]] = {}  # and the adapter of ACP of each
        self.checkouts: dict[str, dict] = {}
        self._set_up: dict[str, dict] = {}  # the checkouts runs set up (add_checkout), kept by a look begun before
        self.draining = False
        self.exit_code = 0
        self.stopped_by_hub = False
        self.hub: WorkerHub | None = None
        self.tmux = interactive.Tmux.from_env(self.env)
        self._repo_locks: dict[str, asyncio.Lock] = {}
        self._runtimes_at = float("-inf")
        self._checkouts_at = float("-inf")
        self.keep_awake = keep_awake or KeepAwake(config.name)  # the power assertions, while it holds a run
        self.wake = wake or WakeWatch()  # notices a sleep from the turns of the heartbeat and claim loops
        self.updater = self_update or SelfUpdate.here(home, self.env)  # updating its install to the tag nightly
        self.outdated = False  # the hub answered 426
        self.browsers: dict[int, browser_session.BrowserSession] = {}  # run id -> its Chromium, while it runs
        self._events()
        # its parts
        self.heartbeat = Heartbeat(self)
        self.state = WorkerState(self)
        self.claims = Claims(self)
        self.leftovers = Leftovers(self)
        self.cleaner = Cleanup(self)
        self.graphs = Graphs(self)
        self.browser = BrowserInstall(self)
        self.updates = Updates(self)

    def _events(self) -> None:
        """The events of a turn of ``run``, made again each time it starts."""
        self.stopping = asyncio.Event()
        self.hard_stop = asyncio.Event()
        self.slot_free = asyncio.Event()
        self.beat_ok = asyncio.Event()

    # Shared with the runs

    def checkout_for(self, project: str, repo: str) -> Path | None:
        entry = self.checkouts.get(f"{project}/{repo}")
        return Path(entry["path"]) if entry else None

    def repo_lock(self, checkout: Path) -> asyncio.Lock:
        return self._repo_locks.setdefault(str(checkout), asyncio.Lock())

    def add_checkout(self, project: str, repo: str, path: Path) -> None:
        """A checkout of ``repo`` of ``project`` a run set up at ``path`` (``runner.clones``): in the checkouts the next
        heartbeat reports, and in ``checkouts`` of config.json, read again so a field set by hand since the start stays,
        so the next look at the machine finds it whatever registry or project names it."""
        key = f"{project}/{repo}"
        self.checkouts[key] = self._set_up[key] = {"path": str(path), "branch": checkouts.git_branch(path)}
        self.config.checkouts = {**(self.config.checkouts or {}), key: str(path)}
        try:
            on_disk = self.home.load_config()
        except Exception:  # an unreadable config.json: write the one in memory, which holds the new checkout
            on_disk = self.config
        on_disk.checkouts = {**(on_disk.checkouts or {}), key: str(path)}
        try:
            self.home.save(on_disk)
        except OSError as exc:
            log.error("config.json could not be written; the checkout holds until the daemon stops", exc_info=exc)
        log.info("checkout set up", extra={"checkout": key, "path": str(path)})

    def fatal(self, problem: HubProblem) -> None:
        """The hub no longer takes this worker (a revoked worker or token, or a protocol the hub no longer speaks):
        stop everything, without reports."""
        if not self.stopped_by_hub:
            self.stopped_by_hub = True
            outdated = self.outdated = isinstance(problem, Outdated)
            self.exit_code = EXIT_FAILED if outdated else revoked_exit(self.env)
            log.error(
                "the hub no longer speaks this daemon's protocol: it updates itself when it may, else upgrade evo-agents"
                if outdated
                else "the hub no longer takes this worker: it or its token was revoked; stopping",
                extra={"status": problem.status, "error": str(problem), "exit_code": self.exit_code},
            )
        for run in self.runs.values():
            run.request_stop("gone")
        self.stopping.set()
        self.hard_stop.set()

    # The machine

    async def look_at_machine(self, force: bool = False) -> None:
        now = asyncio.get_running_loop().time()
        if force or now - self._runtimes_at > RUNTIMES_EVERY:
            self.runtimes = await asyncio.to_thread(self._detect)
            self.logins = await asyncio.to_thread(health.logins, path=self.env.get("PATH"))
            await self.browser.look()
            self._runtimes_at = now
        self.health = await asyncio.to_thread(health.report, self.home.worktrees_dir, self.logins)
        if force or now - self._checkouts_at > CHECKOUTS_EVERY:
            found = await asyncio.to_thread(checkouts.discover, self.config)
            for key, entry in self._set_up.items():  # set up while the look ran, before config.json held it
                if key not in found and checkouts.is_work_tree(Path(entry["path"])):
                    found[key] = entry
            self.checkouts = found
            self._checkouts_at = now

    def look_again(self, *, runtimes: bool = False, checkouts: bool = False) -> None:
        """Have the next heartbeat look at the runtimes, or the checkouts, whatever the time since the last look."""
        if runtimes:
            self._runtimes_at = float("-inf")
        if checkouts:
            self._checkouts_at = float("-inf")

    def _detect(self) -> dict[str, dict]:
        """The runtimes of the core, then those of the plugins the hub declared, as the heartbeat reports them."""
        return {**detect_runtimes(self.adapters), **detect_adapters(self.plugin_adapters)}

    @property
    def free_slots(self) -> int:
        return max(0, self.config.slots - len(self.runs))

    def in_pool(self) -> bool:
        """Whether this daemon is a node of a worker pool, which syncs the hub's skills (``skill_sync_loop``)."""
        return skill_sync_loop.pool_of(self.config.labels or ()) is not None

    def says(self, feature: str) -> bool:
        """Whether the heartbeat names ``feature``: the night shift unless turned off, the browser once Chromium is
        installed, the skill sync on a pool node, every other one always."""
        if feature == runs.KG_SYNC:
            return self.graphs.on
        if feature == runs.SKILL_SYNC:
            return self.in_pool()
        return feature != runs.BROWSER or self.browser.report is not None

    # The loop

    async def run(self) -> int:
        self._events()
        self.graphs.wake = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._signal, sig)
        session = new_session()
        self.hub = WorkerHub(self.config.url, self.token, session)
        background: list[asyncio.Task] = []
        try:
            await self.leftovers.clear()
            await self.look_at_machine(force=True)
            self._log_start()
            self.updater.publish(held=0, orphans=len(self.orphans), worker=self.updates.who())
            background = self._start_parts()
            await self.stopping.wait()
            await self._wind_down(*background[:3])
        finally:
            await self._close(background, session)
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)
        log.info("worker stopped", extra={"exit_code": self.exit_code})
        return self.exit_code

    def _start_parts(self) -> list[asyncio.Task]:
        """The tasks of the daemon's parts, the events left, the claims and the self-update first."""
        assert self.hub is not None
        leftovers = spawn(self.leftovers.send_events(), "leftovers")
        heartbeat = spawn(self.heartbeat.loop(), "heartbeat")
        claims = spawn(self.claims.loop(), "claims")
        cleanup = spawn(self.cleaner.loop(), "cleanup")
        updates = spawn(self.updates.loop(), "self-update")
        doctoring = doctor_report.report_loop(
            self.hub, self.stopping, lambda: doctor_report.examine_here(self.env), secrets=(self.token,)
        )
        parts = [leftovers, claims, updates, heartbeat, cleanup, spawn(doctoring, "doctor-report")]
        if self.graphs.on:
            parts.append(spawn(self.graphs.loop(), "kg-sync"))
        if self.in_pool():
            syncing = skill_sync_loop.sync_loop(self.config.url, self.token, self.env, self.stopping)
            parts.append(spawn(syncing, "skill-sync"))
        return parts

    async def _wind_down(self, leftovers: asyncio.Task, claims: asyncio.Task, updates: asyncio.Task) -> None:
        """Once the daemon stops: no new claim, the runs held end, the update the hub needs, the events left go."""
        claims.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await claims
        if self.run_tasks:
            log.info("waiting for the runs held to end", extra={"runs": sorted(self.run_tasks)})
        while self.run_tasks:
            await asyncio.wait(list(self.run_tasks.values()))
        if self.outdated:
            await self.updates.for_hub(updates)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(leftovers), 0 if self.hard_stop.is_set() else LEFTOVER_GRACE)

    async def _close(self, background: list[asyncio.Task], session) -> None:
        self.graphs.stop()
        if self.browser.installing is not None:  # its thread ends on its own; the daemon does not wait for it here
            background.append(self.browser.installing)
        for task in background:
            task.cancel()
        for task in background:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self.updater.settle()  # an install in flight ends before the daemon does
        self.updater.close()
        await session.close()
        self.keep_awake.close()

    async def cleanup(self, now: datetime | None = None) -> list[int]:
        """The cleanup's sweep (``daemon_cleanup``): the ids of the runs whose worktrees and records went."""
        return await self.cleaner.sweep(now)

    def _log_start(self) -> None:
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
                "commit": self.updater.install.commit,
                "self_update": self.updater.report(),
                "tmux": self.tmux.binary,
            },
        )
        if not self.tmux.available:
            log.warning("tmux is not on PATH: this worker takes headless runs only, with no takeover or terminal")
        if not available:
            log.warning("no runtime can take runs here; the worker claims nothing until one can")
        if not self.checkouts:
            log.info(
                "no checkout of the worker's projects found: each run clones the repos it needs",
                extra={"projects": self.config.projects},
            )

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
