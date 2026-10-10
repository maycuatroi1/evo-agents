"""One run on this worker, from the claim to its last report.

1. ``leased``: the daemon takes the run's leases (``credentials``), so its git and its agent use them, then checks what
   the run needs before it fetches anything (``_preflight``, ``preflight``): the project lists an origin for the repo,
   git reads it and pushes to it with what the run holds, and each program the step's verify calls is on the run's
   PATH; a check that does not pass fails the run before the agent starts, with its cause (the report's
   ``failure_cause``: origin, credentials or missing_tool). It then fetches origin in the checkout of the run's repo
   and makes a worktree under
   ``~/.evo/worker/worktrees/<project>-<run>``, on the plan's branch for the repo, from ``origin/<branch>`` when the
   remote has it (else the local branch, else the remote's default branch). When that branch is checked out
   somewhere else, or has commits here that the start does not hold, the worktree is on ``evo-run/<run>`` instead,
   and the push still goes to the plan's branch. A plan without a branch for the repo, or whose branch is the
   default branch (the remote's HEAD, the hub's default_branch, main or master), fails the run before the agent
   starts: the daemon never pushes the default branch.
2. ``running``: the runtime's adapter starts the agent on the run's prompt; its events go to the spool and on to
   the hub. The owner's messages reach the agent through ``send``; a cancel, the run's timeout (counted from the
   agent's start), or the hub no longer holding the run for this worker interrupt it. ``interactive``: after a
   takeover, or from the start in interactive mode, a person drives the agent's session in its runtime's terminal UI
   in tmux (``interactive``), and a handback, or the person leaving the UI, lets a new adapter go on headless in the
   same session (``running`` again).
3. ``verifying``: the agent wrote ``.evo-run/result.json``; the daemon runs each of its ``verify_commands`` again in
   the worktree, with the run's time left, and records each exit code as a ``system`` event. A command that exits
   other than 0 fails the run, and nothing is pushed: the work stays in the worktree.
4. The daemon commits what the agent left uncommitted as ``run #N: <title>``, leaving out ``.evo-run/``, what hooks
   wrote (``gitops.RUN_COMMIT_EXCLUDES``) and copies of hub plans as the hub wrote them, which a ``system`` event
   names (``gitops.commit_run``); refuses to push a detached HEAD or a branch the agent switched to, pushes the plan's
   branch to origin (never forced, never merged), and reports ``done`` (approval ``auto``) or ``review`` with the
   commit, the diffstat, the verify results, the agent's summary and usage. Every event is sent before that report.
5. After the last report the log (the run's events) and the diff are uploaded as blobs when the hub has a blob store,
   and the worktree leaves the plan's branch, so the owner can check it out elsewhere; it is removed 7 days later.

A hub that does not answer holds nothing up: events wait in the spool and each report is sent again with the
backoff until the hub answers, or says the run is no longer this worker's.

A run the night shift queued has a budget (``spec["budget"]``, ``evo_agents.hub.curator``). Each agent the run starts
gets what the run spent before it (``RunContext.spent_usd`` and ``spent_seconds``): the cost of its session as the
last outcome reported it, or as the claim said for a run that resumes a parked one, and the agent time used. An agent
the budget stopped fails the run, and the log's last note names the cap (``cap``: cost, turns or time).

The leases stay in the daemon's memory (``credentials.RunCredentials``): the run's git gets them through
``credentials.ticketed``, its agent through ``agent_env``, each event goes through ``credentials.scrub`` before the
spool, and they are given back once the run ends here, whether it ended, was parked, or the daemon stops. A push of the
daemon that fails to authenticate on a leased origin takes the leases again once and pushes once more
(``RunCredentials.with_renewal``). What the runtime leaves out of the agent's environment because of the leases (Claude
Code drops the daemon's ANTHROPIC_API_KEY next to a leased CLAUDE_CODE_OAUTH_TOKEN) is noted once a run
(``_note_environment``).

Each time an agent starts, headless or in its terminal UI, the daemon notes its process group in
``runs/<run>/agent.json`` (``orphans``), and removes the file just before the run counts as ended here, so a daemon
that starts after this one died finds the agents it left.

A review run (kind ``review``, ``ReviewRun``) is the Curator's Reviewer of one night of a project: its worktrees are
detached at the commit origin's default branch has, the worker counts the open items of reports and the learned skills
waiting for review in them (``figures``) into ``.evo-run/worktree-figures.json``, the agent records findings and
proposals with ``evo-agents worker finding|propose``, and the run ends done with the agent's summary: nothing is
committed or pushed (``gitops.check_push`` refuses a review run), and the run's GitHub token reads only.

An author run (kind ``author``, ``AuthorRun``, ``evo_agents.hub.author``) writes an execution plan from its owner's
request: before anything else the worker downloads the skills its claim names (create-exec-plan, as the hub holds it in
the global scope) through their presigned GETs, checks each one's size, SHA-256 and contents, and fails the run when one
is missing or wrong; then makes a worktree of each of its repos it has a checkout of (the project's harness first,
which it needs; the others it leaves out, saying so), detached at the commit origin's default branch has, as a review
run does, and writes the skills under ``.claude/skills`` of the run's directory, the agent's working directory, so
nobody syncs skills on this machine. The agent works in EVO_RUN_KIND ``author`` on Claude Code alone, with Claude
Code's question tools turned off (``author.QUESTION_TOOLS``), and talks with its owner in the run's chat: after each
turn the worker posts the agent's last message to the chat (POST /v1/worker/runs/{id}/chat) and reports ``waiting``,
keeping the run, its slot and lease, the time it waits not counted toward its timeout, until the owner's reply comes
through the inbox and starts the next turn in the same session (``author.REPLY_PROMPT``). A run the hub parks (no reply
within a day) keeps its session, worktrees and skills for the run that resumes it, as a plan run does, which goes on
in them with the reply (``author.RESUME_PROMPT``). Once the owner ends the chat (the heartbeat's ``finish``) the run
ends done with its summary after the agent's turn: nothing is committed or pushed (``gitops.check_push`` refuses an
author run), and the run's GitHub token reads only.

A judge run (kind ``judge``, ``JudgeRun``) is the Curator's Judge of one change. Once it holds its leases, the worker
reads the plan's verify commands and the project's hidden checks from the hub (GET /v1/worker/runs/{id}/judge with the
run's own key, which the claim handed the daemon and only its memory holds; the checks are held in memory, never
written to a file, an event or a log line), and its preflight looks for the program of each on PATH: one missing fails
the run with missing_tool and no verdict, a hidden check named by its number alone, so the hub has the change judged
again. Its worktree is detached at the commit it judges (the pull request's head, else the tip of the change's branch
on origin), made and read with no hook of the checkout; the worker reads the diff from the merge base with origin's
default branch before any code of the change runs, finds the signs of score hacking in it
(``evo_agents.hub.judge.hack_signs``; a diff longer than the worker reads is a sign too), and runs the hidden checks,
then the verify commands, in the worktree as code it does not trust (``evo_agents.worker.untrusted``): each one on the
standard input of ``/bin/sh -s``, in a session of its own, with an environment that holds neither the worker's home nor
the run's id nor a credential, the worktree put back at the commit judged before it, and every process it left killed
after it. It then starts the Judge's agent, unless a sign already fails the change, on the worktree put back again,
the hub's prompt and what it ran (exit codes alone for the hidden checks). The agent's verdict is the JSON object that
ends its last message (``judge.verdict_from_message``), which reaches the worker from the agent's own output: no file
the code under test could write. The worker posts the verdict with the run's key and the paths the diff touches (POST
/v1/worker/runs/{id}/verdict) and ends the run done, committing and pushing nothing.

A run of the Curator (``spec["curator"]``: a review run, a judge run, or a Builder, the plan run of a plan the Curator
made) is watched: after each heartbeat the daemon compares each of its worktrees with the charter's protected paths, and
its agent time and cost with its caps (``Run.watch``); a protected file changed, or a cap passed, stops the run, which
fails naming why, and the hub tells its owner (notice run_failed). Its agent never holds a push credential
(``credentials.RunCredentials.guarded``): only the daemon's own git gets the run's leases, through a ticket of one
command. A Builder's branch ``curator/...`` is pushed by the daemon alone, never a default branch
(``gitops.check_push``, kind ``curator``), only with a leased credential, and on GitLab with the push options that open
a merge request into the default branch; when its agent reports a step done, ``evo-agents worker step`` asks the daemon
to push (``_push_for_agent``). On Claude Code a run of the Curator uses the Claude subscription login alone (plan
decision 14): ANTHROPIC_API_KEY is left out of its agent's environment, and a machine that has no subscription login
for it fails the run before the agent starts, saying so (``Adapter.login_refusal``).

A plan run (kind ``plan``, ``PlanRun``) does every step of a plan not done yet, in one session:

1. ``leased``: the daemon takes the run's leases for all its repos, then makes the directory
   ``~/.evo/worker/worktrees/<project>-<run>`` with a worktree of each repo of the run in it, each on the branch the
   plan names for that repo (``evo-run/<run>/<repo>`` when that branch is checked out elsewhere; the push still goes
   to the plan's branch), and writes the plan as claimed to ``.evo-run/plan.yaml`` there. A repo the plan names no
   branch for fails the run before the agent starts, and so does a default branch the plan does not name for the repo.
2. ``running``: the agent works in that directory, with EVO_RUN_ID, EVO_RUN_KIND and EVO_WORKER_HOME, and reports
   each step itself through ``evo-agents worker step`` (which runs the step's verify commands again, commits, pushes
   the repo's branch and sends the step report), asks its owner through ``evo-agents worker ask``, and notifies with
   ``evo-agents worker notify``.
3. ``waiting``: a turn that ends with a decision of the run open moves the run to waiting; the daemon keeps it, with
   its slot and lease, and the time it waits does not count toward its timeout. The owner's answer comes through the
   inbox and goes to a new turn of the agent in the same session (``running`` again). A run the hub parks (no answer
   within a day) stops at the end of its turn, keeps its session and worktrees, and frees its slot; the run that
   resumes it, claimed with ``resume_of_run_id``, goes on in them.
4. Once a turn ends with nothing to wait for, the daemon reports ``verifying`` and commits (leaving out what a run
   of one step leaves out) and pushes what each repo has left, never forced (a default branch only when the plan
   names it, with the notice ``push_default_branch``), and reports ``done`` with the agent's summary from
   ``.evo-run/result.json``: no verify commands run at the end, since ``evo-agents worker step`` ran each step's. The
   log and the diffs of every repo are uploaded as for a run of one step.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import signal
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from evo_agents.hub import author, curator, judge, runs, skill_sync, skills, tiers
from evo_agents.hub.client import HubError
from evo_agents.hub.credentials import normalize_origin
from evo_agents.worker import credentials, figures, gitops, interactive, orphans, preflight, untrusted
from evo_agents.worker.adapter import AgentEvent, AgentFinished, Outcome, RunContext
from evo_agents.worker.checkouts import default_branch_of
from evo_agents.worker.credentials import RunCredentials
from evo_agents.worker.hubapi import Backoff, HubProblem, Outdated, Refused, Unreachable
from evo_agents.worker.spool import Spool, encode_event

if TYPE_CHECKING:
    from evo_agents.worker.daemon import Daemon

log = logging.getLogger("evo_agents.worker")

API_KEY = "ANTHROPIC_API_KEY"  # left out of the agent of a run of the Curator on Claude Code (plan decision 14)
RUN_BRANCH = "evo-run/{id}"
RUN_REPO_BRANCH = "evo-run/{id}/{folder}"  # a plan run's worktree of a repo whose branch is checked out elsewhere
RESULT_MAX_BYTES = 1024 * 1024
MAX_VERIFY = 50
MAX_COMMAND_CHARS = 2000
MAX_ERROR_CHARS = 2000
MAX_SUMMARY_CHARS = 8000
MAX_USAGE_BYTES = 64 * 1024
OUTPUT_TAIL = 8 * 1024  # bytes of a verify command's output kept in its event
NOTE_GROUP_LOOKS = 20  # events after its start at which an adapter is asked again for its agent's process group
MAX_LEFT_OUT_NAMED = 20  # files a note of what a commit left out names in its text
MAX_LEFT_OUT_LISTED = 200  # and lists in its body
LOG_LIMIT = 64 * 1024 * 1024  # the run-log blob
DIFF_LIMIT = 8 * 1024 * 1024  # the run-diff blob
STOP_GRACE = 30.0  # seconds an interrupted agent has to end its events
HARD_STOP_TRIES = 3  # tries of a report once the daemon is stopping now
FLUSH_DEBOUNCE = 0.2  # seconds of events gathered into one batch
TERMINAL_POLL = 1.0  # seconds between looks at the tmux session while a person drives the agent
TERMINAL_LOG_GRACE = 5.0  # seconds the log of the terminal has, once its session is closed, to read the last records
CAP_CAUSES = {"cost": "cost_cap", "turns": "turn_cap", "time": "time_cap"}  # the failure_cause of a cap that stopped it
# What the agent is told when a person hands its session back.
HANDBACK_PROMPT = (
    "The owner of this run drove this session in a terminal and has handed it back to you. Go on with the task of "
    "the run from where the session and the working tree stand now, and finish it as the first message of this "
    f"session asks, writing {runs.RESULT_FILE} at the end."
)
# What a plan run's agent is told when the owner wrote while its turn was over: an answer to a decision, say.
ANSWER_PROMPT = (
    "The owner of this plan run wrote while your turn was over; a message that answers a decision names it. Go on "
    "with the plan in this session from where you stopped: read the plan as the hub holds it now with "
    "`evo-agents worker plan` first."
)
# And when a run parked while it waited for its owner goes on, as a new run, in the same session and worktrees.
RESUME_PROMPT = (
    "This plan run was parked while it waited for its owner, and goes on now as run #{id}, in the same session and "
    "the same worktrees; EVO_RUN_ID names the new run, and the commands of `evo-agents worker` use it. Go on with the "
    "plan from where you stopped: read the plan as the hub holds it now with `evo-agents worker plan` first."
)


class RunGone(Exception):
    """The hub no longer takes reports of this run from this worker: lost, cancelled, taken by another attempt."""


class ReportRefused(RunGone):
    """The hub refused a report with a 4xx other than 401 and 403; ``status`` says which."""

    def __init__(self, message: str, status: int | None):
        super().__init__(message)
        self.status = status


class Parked(Exception):
    """The hub parked the plan run, or a new run resumes it: the agent's turn is over, and its session and worktrees
    stay for the run that resumes it."""


class RunFailed(Exception):
    """The run fails with ``error``; ``fields`` go with the report: ``verify``, ``usage``, ``cap`` (the cap of the
    budget that stopped the agent) and ``cause`` (``runs.FAILURE_CAUSES``, the report's failure_cause)."""

    def __init__(self, error: str, **fields):
        super().__init__(error)
        self.error = error
        self.fields = fields


class Stopped(Exception):
    """The run was asked to stop: ``reason`` is cancel, timeout, gone or shutdown."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _now() -> datetime:
    return datetime.now(UTC)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _json_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


async def _wait_or(event: asyncio.Event, seconds: float) -> bool:
    """Wait ``seconds``, or less when ``event`` is set; whether it is."""
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(event.wait(), max(0.0, seconds))
    return event.is_set()


async def _first_of(events: Collection[asyncio.Event], seconds: float) -> None:
    """Wait ``seconds``, or less when one of ``events`` is set."""
    waits = {asyncio.create_task(event.wait()) for event in events}
    try:
        await asyncio.wait(waits, timeout=max(0.0, seconds), return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in waits:
            task.cancel()


class Sender:
    """Sends a spool's events to the hub in batches, from ``ack_seq + 1``, until it is closed and empty. A failed
    call is tried again with the backoff; a hub that will not take the run's events (404, the run's 20,000) ends
    the sending and drops what is left."""

    def __init__(self, daemon: Daemon, spool: Spool):
        self.daemon = daemon
        self.spool = spool
        self.wake = asyncio.Event()
        self.closed = False
        self.gave_up: str | None = None
        self._failures_logged = 0

    def poke(self) -> None:
        self.wake.set()

    def close(self) -> None:
        self.closed = True
        self.wake.set()

    @property
    def settled(self) -> bool:
        """Every event is acknowledged, or the sending gave up."""
        return self.gave_up is not None or not self.spool.pending

    async def run(self) -> None:
        try:
            await self._send()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a broken spool must not leave the run waiting for its events forever
            log.exception("sending events failed", extra={"run_id": self.spool.run_id})
            self.gave_up = f"{type(exc).__name__}: {exc}"

    async def _send(self) -> None:
        backoff = Backoff()
        run_id = self.spool.run_id
        while True:
            self.wake.clear()
            if not self.spool.pending:
                if self.closed:
                    return
                await self.wake.wait()
                if not self.closed:
                    await asyncio.sleep(FLUSH_DEBOUNCE)
                continue
            batch = self.spool.batch()
            try:
                answer = await self.daemon.hub.events(run_id, batch)
            except Unreachable as exc:
                delay = exc.retry_after if exc.retry_after is not None else backoff.next()
                if self._failures_logged < 3 or backoff.failures % 10 == 0:
                    log.warning(
                        "events not sent; they wait in the spool",
                        extra={
                            "run_id": run_id,
                            "pending": self.spool.pending,
                            "retry_in_s": round(delay, 1),
                            "error": str(exc),
                        },
                    )
                self._failures_logged += 1
                if self.daemon.hard_stop.is_set() and backoff.failures > HARD_STOP_TRIES:
                    self.gave_up = "the daemon stopped"
                    return
                await _wait_or(self.daemon.hard_stop, delay)
                continue
            except Refused as exc:
                if isinstance(exc, Outdated) or exc.status in (401, 403):
                    self.daemon.fatal(exc)
                self.gave_up = str(exc)
                log.warning(
                    "the hub does not take this run's events; dropping them",
                    extra={"run_id": run_id, "status": exc.status, "dropped": self.spool.pending, "error": str(exc)},
                )
                self.spool.drop_all()
                return
            if self._failures_logged:
                log.info("events sent again after the hub answered", extra={"run_id": run_id})
                self._failures_logged = 0
            backoff.reset()
            ack = answer.get("ack_seq") if isinstance(answer, dict) else None
            if not isinstance(ack, int):
                self.gave_up = "the hub answered events without ack_seq"
                self.spool.drop_all()
                return
            if ack < batch[0]["seq"] - 1:
                # The hub misses events before this spool's first: they are gone, and nothing after a gap is stored.
                self.gave_up = (
                    f"the hub lacks events {ack + 1} to {batch[0]['seq'] - 1}, which this worker no longer has"
                )
                log.error("event gap", extra={"run_id": run_id, "ack_seq": ack, "first": batch[0]["seq"]})
                self.spool.drop_all()
                return
            self.spool.acknowledge(ack)

    async def drained(self) -> bool:
        """Wait until every event is acknowledged or the sending gave up; whether everything was sent."""
        self.poke()
        while not self.settled:
            await asyncio.sleep(0.05)
        return self.gave_up is None


class Run:
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
        self.title = spec.get("title") or f"step {spec.get('step_key')}"
        self.state = "leased"
        self.ended = False
        self.stop_reason: str | None = None
        self._stop = asyncio.Event()
        self.adapter = None
        self.agent_running = False
        self.agent_started = False  # the agent (headless or in a terminal) has started once: the timeout counts
        self.group_noted = False  # agent.json names the process group of the agent that runs now
        self.session_reported: str | None = None
        self.phase = "prepare"  # prepare, headless, interactive (a person drives the agent), after
        self.takeover_asked = False
        self.handback_asked = asyncio.Event()
        self.tui = None  # the runtime's terminal UI while a person drives the agent
        self.terminal: interactive.WebTerminal | None = None
        self.terminal_wanted = False  # a browser asked for the terminal before the run was interactive
        self.terminal_failed: str | None = None  # why a terminal UI did not open; later takeovers are refused
        self._inbox_waits = False
        self.inbox_arrived = asyncio.Event()  # the heartbeat counted messages of the owner in the inbox
        self.finish_asked = asyncio.Event()  # the owner ended the chat of an author run: end it done after the turn
        self.open_decisions = 0  # the run's decisions still open, as the last heartbeat counted them
        self.answered: set[int] = set()  # the decisions whose answers the agent was handed
        self._delivered_upto = 0  # the inbox's messages up to this id were handed to the agent
        self._pending_ack: int | None = None  # handed to the agent in the prompt of the next start: ack once it runs
        self.loop = asyncio.get_running_loop()
        self.deadline = self.loop.time() + self.timeout_s  # from the claim until the agent starts, then from there
        self.worktree: Path | None = None
        self.checkout: Path | None = None
        self.local_branch: str | None = None
        self.base: str | None = None
        self.protected: set[str] = set()
        self.verify: list[dict] = []
        self.summary: str | None = None
        self.outcome: Outcome | None = None
        budget = curator.budget_of(spec) or {}
        self.budget = budget
        found = spec.get("curator")
        self.curator: dict | None = found if isinstance(found, dict) else None  # a run of the Curator: its role
        # A judge run's own key: in this object alone, out of the spec its adapter and records see.
        self._judge_key: str | None = self.curator.pop("judge_key", None) if self.curator is not None else None
        self.watchdog_reason: str | None = None  # why the watchdog stopped the run
        self.cost_seen = 0.0  # the session's cost so far, as the agent's usage events said it
        self.spent_usd = budget.get("spent_usd") or 0.0  # the cost of the agent's session so far
        self.spent_before = budget.get("spent_seconds") or 0.0  # agent time a parked run this one resumes used
        self._report_lock = asyncio.Lock()
        self._delivering = False
        self._unsupported_noted: set[str] = set()
        self._environment_noted: set[str] = set()  # the lines of environment_notes written already
        self._background: set[asyncio.Task] = set()
        home = daemon.home
        home.run_dir(self.id).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.log_path = home.run_dir(self.id) / "events.jsonl"
        self.log_bytes = 0
        self.spool = Spool(home.spool_dir, self.id, daemon.budget)
        self.sender = Sender(daemon, self.spool)
        self.credentials = RunCredentials(self.id, home, daemon.env, self.note, guarded=self.curator is not None)
        self.record = {
            "id": self.id,
            "kind": spec.get("kind") or "step",
            "project": self.project,
            "plan_id": spec.get("plan_id"),
            "step_key": spec.get("step_key"),
            "repo": self.repo,
            "branch": self.branch,
            "runtime": self.runtime,
            "claimed_at": _now().isoformat(),
            "finished_at": None,
            "state": self.state,
        }
        if self.curator is not None:  # what `evo-agents worker step` pushes with: a branch of the Curator alone
            self.record["curator"] = {key: self.curator.get(key) for key in ("role", "branch", "forge", "change_id")}
        home.save_run(self.record)

    # Events

    def event(self, kind: str, body: dict, at: datetime | None = None) -> None:
        """Spool an event of the run and write it to its log, every lease value masked."""
        at = at or _now()
        if kind == "usage_update":  # the session's running cost, for the watchdog of a run of the Curator
            cost = body.get("cost") if isinstance(body.get("cost"), dict) else {}
            amount = cost.get("amount")
            if isinstance(amount, (int, float)) and not isinstance(amount, bool):
                self.cost_seen = max(self.cost_seen, float(amount))
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
        self._stop.set()

    def _check(self) -> None:
        if self.stop_reason is not None:
            raise Stopped(self.stop_reason)
        if self.loop.time() > self.deadline:
            self.stop_reason = "timeout"
            raise Stopped("timeout")

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def unsupported(self, what: str, why: str) -> None:
        """The owner asked for something this worker cannot do: say so once in the run's log."""
        if what in self._unsupported_noted:
            return
        self._unsupported_noted.add(what)
        self.note(f"The owner asked for a {what}; this worker cannot do it ({why}), so the run goes on.")

    def interactive_unsupported(self, cls=None) -> str | None:
        """Why this run's agent cannot be handed to a person in a terminal here, or None when it can."""
        cls = cls or self.daemon.adapters.get(self.runtime)
        if cls is None or not getattr(cls, "interactive", False):
            return f"its {self.runtime} adapter cannot hand the session to a terminal"
        if not self.daemon.tmux.available:
            return "tmux is not on PATH"
        if self.terminal_failed:
            return f"the terminal UI did not open earlier: {self.terminal_failed}"
        return None

    def request_takeover(self) -> None:
        """The heartbeat says the owner asked to drive the agent in a terminal; it says so until the run is
        interactive."""
        if self.ended or self.takeover_asked or self.phase in ("interactive", "after"):
            return
        why = self.interactive_unsupported()
        if why is not None:
            self.unsupported("takeover", why)
            return
        self.takeover_asked = True
        name = interactive.session_name(self.id)
        log.info("takeover asked", extra={"run_id": self.id, "phase": self.phase})
        if self.phase == "headless":
            self.note(
                "The owner asked for a takeover: the agent ends its turn, then its session opens in its terminal UI "
                f"in tmux session {name}."
            )
            if self.agent_running and self.adapter is not None:
                self._spawn(self._stop_at_boundary(self.adapter))
        else:
            self.note(f"The owner asked for a takeover: the agent starts in its terminal UI in tmux session {name}.")

    def request_park(self) -> None:
        """The heartbeat says the hub parked the run; only a plan run parks, so another run is no longer this
        worker's."""
        self.request_stop("gone")

    def _park_requested(self) -> bool:
        return False

    def request_finish(self) -> None:
        """The heartbeat says the owner ended the chat of an author run; no other run has one."""

    def inbox_waits(self) -> None:
        """The heartbeat counts messages of the owner in the inbox: hand them to the agent, and wake a run that waits
        for its owner's answer."""
        self.inbox_arrived.set()
        self._spawn(self.deliver_inbox())

    def request_handback(self) -> None:
        """The heartbeat says the owner asked to let the agent go on headless; it says so until the run is running."""
        if self.phase == "interactive" and not self.handback_asked.is_set():
            log.info("handback asked", extra={"run_id": self.id})
            self.handback_asked.set()

    def open_terminal(self) -> None:
        """The heartbeat says a browser waits for the run's terminal: connect the worker's end once the run is
        interactive, or say in the terminal that this worker does not allow it."""
        if self.ended:
            return
        if self.phase != "interactive" or self.tui is None or self.state != "interactive":
            self.terminal_wanted = True
            return
        self.terminal_wanted = False
        config = self.daemon.config
        if self.terminal is None:
            self.terminal = interactive.WebTerminal(
                self.id,
                self.daemon.tmux,
                allowed=config.allow_web_terminal,
                worker=config.name,
                config_path=self.daemon.home.config_path,
            )
        if not self.terminal.connected and not self.terminal.allowed and "web terminal" not in self._unsupported_noted:
            self._unsupported_noted.add("web terminal")
            self.note(
                "The owner opened the web terminal; this worker does not allow it (allow_web_terminal is off in its "
                "config.json), so the terminal says so and closes."
            )
        self.terminal.open(self.daemon.hub.session, self.daemon.hub.url, self.daemon.token)

    async def _stop_at_boundary(self, adapter) -> None:
        try:
            await asyncio.wait_for(adapter.stop_at_turn_boundary(), STOP_GRACE)
        except Exception:
            log.warning("the agent was not asked to end its turn", extra={"run_id": self.id}, exc_info=True)

    async def deliver_inbox(self) -> None:
        """Hand the owner's waiting messages to the agent, then mark them delivered on the hub. Messages the agent
        no longer takes (its last turn is over, or a person drives it) wait in the inbox for the next agent."""
        if self._delivering or not self.agent_running or self.adapter is None:
            return
        self._delivering = True
        adapter = self.adapter
        try:
            messages = await self.daemon.hub.inbox(self.id)
            while messages and self.agent_running and adapter is self.adapter:
                last = None
                try:
                    for message in messages:
                        message_id = message.get("id")
                        if isinstance(message_id, int) and message_id <= self._delivered_upto:
                            last = message_id  # in the prompt the agent started on already
                            continue
                        await adapter.send(str(message.get("text") or ""))
                        last = message_id
                        self._handed([message])
                except AgentFinished:
                    if last is not None:
                        await self.daemon.hub.inbox(self.id, ack=int(last))
                    if not self._inbox_waits:
                        self._inbox_waits = True
                        log.info("messages wait in the inbox: the agent takes no more input", extra={"run_id": self.id})
                    return
                if last is None:
                    break
                messages = await self.daemon.hub.inbox(self.id, ack=int(last))
        except HubProblem as exc:
            log.warning("messages not taken from the inbox", extra={"run_id": self.id, "error": str(exc)})
        except Exception:
            log.exception("messages not handed to the agent", extra={"run_id": self.id})
        finally:
            self._delivering = False

    def _handed(self, messages: list[dict]) -> None:
        """Note that these messages of the inbox went to the agent: the decisions they answer are answered."""
        for message in messages:
            message_id, decision_id = message.get("id"), message.get("decision_id")
            if isinstance(message_id, int):
                self._delivered_upto = max(self._delivered_upto, message_id)
            if isinstance(decision_id, int):
                self.answered.add(decision_id)
        self.record["answered"] = sorted(self.answered)

    async def _ack_inbox(self, ack: int) -> None:
        """Mark the inbox's messages up to ``ack`` delivered, trying a few times."""
        backoff = Backoff()
        for _ in range(HARD_STOP_TRIES + 1):
            try:
                await self.daemon.hub.inbox(self.id, ack=ack)
                return
            except Unreachable:
                await _wait_or(self.daemon.hard_stop, backoff.next())
            except HubProblem as exc:
                log.warning("inbox not acknowledged", extra={"run_id": self.id, "error": str(exc)})
                return

    # Reports

    async def _report(self, state: str, *, only_from: tuple[str, ...] | None = None, **fields) -> dict:
        """Report a move, sending it again until the hub answers. ``only_from``: send it only while the run is in one
        of these states, which a report queued behind another may no longer be."""
        body = {"state": state}
        for key, value in fields.items():
            if value is not None:
                body[key] = value
        async with self._report_lock:
            if only_from is not None and self.state not in only_from:
                return {}
            backoff = Backoff()
            stripped = False
            while True:
                try:
                    answer = await self.daemon.hub.report(self.id, body)
                except Unreachable as exc:
                    if self.daemon.hard_stop.is_set() and backoff.failures >= HARD_STOP_TRIES:
                        raise RunGone(f"the daemon stopped before the hub took the report of {state}") from None
                    delay = exc.retry_after if exc.retry_after is not None else backoff.next()
                    log.warning(
                        "report not sent; trying again",
                        extra={"run_id": self.id, "state": state, "retry_in_s": round(delay, 1), "error": str(exc)},
                    )
                    await _wait_or(self.daemon.hard_stop, delay)
                    continue
                except Refused as exc:
                    if isinstance(exc, Outdated) or exc.status in (401, 403):
                        self.daemon.fatal(exc)
                        raise RunGone(str(exc)) from None
                    if exc.status == 422 and not stripped and len(body) > 2:
                        # A field the hub does not take must not keep the run from ending: send the move alone.
                        log.error(
                            "report refused; sending it without its details",
                            extra={"run_id": self.id, "error": str(exc)},
                        )
                        body = {key: body[key] for key in ("state", "error") if key in body}
                        stripped = True
                        continue
                    raise ReportRefused(f"the hub refused the report of {state}: {exc}", exc.status) from None
                self.state = state
                self.record["state"] = state
                with contextlib.suppress(OSError):
                    self.daemon.home.save_run(self.record)
                return answer if isinstance(answer, dict) else {}

    async def _report_running(self, only_from: tuple[str, ...]) -> None:
        session_id = self.adapter.session_id if self.adapter is not None else None
        await self._report_session("running", only_from, session_id)

    async def _report_session(self, state: str, only_from: tuple[str, ...], session_id: str | None) -> None:
        try:
            await self._report(state, only_from=only_from, session_id=session_id)
            self.session_reported = session_id
        except RunGone as exc:
            log.warning("run is not this worker's any more", extra={"run_id": self.id, "error": str(exc)})
            self.request_stop("gone")

    async def _end(self, state: str, **fields) -> None:
        """Send every event, then the report that ends the run."""
        if not await self.sender.drained():
            log.warning("ending the run with events unsent", extra={"run_id": self.id, "why": self.sender.gave_up})
        if fields.get("error"):
            fields["error"] = _cut(str(fields["error"]), MAX_ERROR_CHARS)
        if fields.get("usage") is not None and _json_size(fields["usage"]) > MAX_USAGE_BYTES:
            fields["usage"] = None
        if self.adapter is not None and fields.get("session_id") is None:
            fields["session_id"] = self.adapter.session_id
        await self._report(state, **fields)
        self.ended = True
        log.info("run ended", extra={"run_id": self.id, "state": state, "error": fields.get("error")})

    # The run

    async def main(self) -> None:
        sender = asyncio.create_task(self.sender.run())
        try:
            await self._go()
            if self.ended:
                await self._upload()
        except RunGone as exc:
            log.warning(
                "run left: the hub no longer takes it from this worker", extra={"run_id": self.id, "error": str(exc)}
            )
            await self._interrupt_agent()
        except Exception:
            log.exception("the worker failed while running", extra={"run_id": self.id})
            await self._interrupt_agent()
            with contextlib.suppress(RunGone):
                error = f"the worker on {self.daemon.config.name} failed while running"
                await self._end("failed", error=error, failure_cause="worker_stopped")
        finally:
            for task in list(self._background):
                task.cancel()
            if self.terminal is not None:
                await self.terminal.close()
            try:  # the run ended here, was parked, or the daemon stops: its leases go back
                await self.credentials.release(stop=self.daemon.hard_stop)
            except Exception:
                log.exception("the run's leases were not given back", extra={"run_id": self.id})
            self.sender.close()
            if not self.daemon.hard_stop.is_set():
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(sender), STOP_GRACE)
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
            self.record["finished_at"] = _now().isoformat()
            self.record["state"] = self.state
            with contextlib.suppress(OSError):
                self.daemon.home.save_run(self.record)
            await self._release_branch()

    async def _go(self) -> None:
        try:
            await self._steps()
        except RunFailed as exc:
            cap = exc.fields.get("cap")
            cause = exc.fields.get("cause") or CAP_CAUSES.get(cap)
            self.note(f"Run failed: {exc.error}", **({"cap": cap} if cap else {}))
            await self._end(
                "failed",
                error=exc.error,
                verify=exc.fields.get("verify"),
                usage=exc.fields.get("usage"),
                failure_cause=cause,
            )
        except Stopped as exc:
            await self._stopped(exc.reason)

    async def _stopped(self, reason: str) -> None:
        await self._interrupt_agent()
        if reason == "gone":
            return
        if reason == "cancel":
            self.note("Cancelled by the owner.")
            await self._end("cancelled")
        elif reason == "timeout":
            error = f"it ran past its timeout of {self.timeout_s // 60} minutes"
            self.note(f"Run failed: {error}.")
            await self._end("failed", error=error, verify=self.verify or None, failure_cause="timeout")
        elif reason == "watchdog":
            error = f"the watchdog of the Curator's runs stopped it: {self.watchdog_reason}"
            self.note(f"Run failed: {error}.", watchdog=self.watchdog_reason)
            await self._end("failed", error=error)
        else:
            error = f"the worker {self.daemon.config.name} was stopped while the run was {self.state}"
            self.note(f"Run failed: {error}.")
            await self._end("failed", error=error, failure_cause="worker_stopped")

    async def _steps(self) -> None:
        spec = self.spec
        self.note(
            f"Run #{self.id} claimed by worker {self.daemon.config.name}: step {spec.get('step_key')} of plan "
            f"{spec.get('plan_id')}, {self.runtime}, {self.mode}, approval {self.approval}, timeout "
            f"{self.timeout_s // 60} min."
        )
        cls = self.daemon.adapters.get(self.runtime)
        if cls is None:
            raise RunFailed(f"this worker has no adapter for {self.runtime}", cause="runtime")
        if self.mode == "interactive":
            why = self.interactive_unsupported(cls)
            if why is not None:
                raise RunFailed(
                    f"this worker cannot run {self.runtime} interactive ({why}): run it headless", cause="runtime"
                )
        checkout = self.daemon.checkout_for(self.project, self.repo)
        if checkout is None:
            raise RunFailed(f"this worker has no checkout of {self.project}/{self.repo}")
        if not self.branch:
            raise RunFailed(
                f"the plan names no branch for {self.repo}, and the worker never works on the default branch"
            )
        await self._take_credentials([self.repo])
        self._check()
        await self._preflight([self.repo], pushes=[self.repo], verify=self._claimed_verify())
        self._check()
        await self._prepare(checkout)
        self._check()
        await self._run_agent(cls)
        commands = self._read_result()
        await self._report("verifying")
        self._check()
        self.verify = await self._run_verify(commands)
        bad = next((item for item in self.verify if item["exit_code"] != 0), None)
        if bad is not None:
            raise RunFailed(
                f"verify command `{_cut(bad['command'], 200)}` exited {bad['exit_code']}; nothing was pushed, the "
                f"work stays in {self.worktree}",
                verify=self.verify,
                usage=self.outcome.usage if self.outcome else None,
                cause="missing_tool" if bad["exit_code"] == 127 else "verify_failed",
            )
        self._check()
        commit_sha, diffstat = await self._commit_and_push()
        final = "done" if self.approval == "auto" else "review"
        await self._end(
            final,
            commit_sha=commit_sha,
            diffstat=diffstat,
            verify=self.verify,
            summary=self.summary,
            usage=self.outcome.usage if self.outcome else None,
        )

    # Checkout

    async def _prepare(self, checkout: Path) -> None:
        branch = self.branch
        async with self.daemon.repo_lock(checkout):
            if not await gitops.has_remote(checkout):
                raise RunFailed(f"the checkout at {checkout} has no remote named origin to fetch from and push to")
            await self._fetch(checkout)
            if not await gitops.check_branch_name(checkout, branch):
                raise RunFailed(f"the plan's branch {branch!r} is not a valid branch name")
            remote_head = await gitops.remote_default_branch(checkout)
            hub_default = default_branch_of(self.daemon.config, self.project, self.repo)
            self.protected = {name for name in (remote_head, hub_default, *gitops.PROTECTED) if name}
            if branch in self.protected:
                raise RunFailed(
                    f"the plan's branch for {self.repo} is {branch}, a default branch: the worker never pushes it; "
                    "give the repo a branch of its own in the plan"
                )
            start = None
            for ref in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}", "refs/remotes/origin/HEAD", "HEAD"):
                base = await gitops.rev(checkout, ref)
                if base is not None:
                    start = ref
                    break
            if start is None:
                raise RunFailed(f"the checkout at {checkout} has no commit to start from")
            path = self.daemon.home.worktree_path(self.project, self.id)
            if path.exists():
                raise RunFailed(f"{path} exists already; remove it and run the step again")
            in_use = await gitops.branches_in_worktrees(checkout)
            local = await gitops.rev(checkout, f"refs/heads/{branch}")
            if branch not in in_use and (local is None or await gitops.is_ancestor(checkout, local, base)):
                local_branch = branch
            else:
                local_branch = RUN_BRANCH.format(id=self.id)
                why = "is checked out in another worktree" if branch in in_use else "has commits here that it lacks"
                self.note(f"{branch} {why}; the run works on {local_branch} and pushes it to {branch}.")
            try:
                await gitops.add_worktree(checkout, path, local_branch, base, reset=True)
            except gitops.GitError as exc:
                raise RunFailed(f"cannot make the worktree {path}: {exc}") from None
        self.checkout, self.worktree, self.local_branch, self.base = checkout, path, local_branch, base
        self.record.update(
            {"checkout": str(checkout), "worktree": str(path), "local_branch": local_branch, "base": base}
        )
        self.daemon.home.save_run(self.record)
        self.note(f"Worktree {path} on {local_branch} at {base[:12]} ({start}).", worktree=str(path))

    async def _fetch(self, checkout: Path) -> None:
        """Fetch origin in ``checkout`` with the run's credentials; RunFailed when git cannot, with the cause
        credentials when the remote refused it for want of a credential, else checkout."""
        self.note(f"Fetching origin in {checkout}.")
        try:
            with self.credentials.ticketed(self.daemon.env) as env:
                await gitops.fetch(checkout, env=env)
        except gitops.GitError as exc:
            cause = "credentials" if isinstance(exc, gitops.GitAuthError) else "checkout"
            raise RunFailed(f"git fetch in {checkout} failed: {exc}", cause=cause) from None

    async def _release_branch(self) -> None:
        """Leave the plan's branch once the run is over, so it can be checked out elsewhere."""
        if self.worktree is None or self.local_branch != self.branch or not self.worktree.exists():
            return
        with contextlib.suppress(gitops.GitError, OSError):
            await gitops.detach(self.worktree)

    # Credentials

    async def _take_credentials(self, repos) -> None:
        """Take the run's leases for ``repos``, handed to the origins of their checkouts here."""
        origins: dict[str, list[str]] = {}
        for name in dict.fromkeys(repo for repo in repos if isinstance(repo, str) and repo):
            checkout = self.daemon.checkout_for(self.project, name)
            if checkout is not None:
                with contextlib.suppress(gitops.GitError, OSError):
                    origins[name] = await gitops.remote_urls(checkout)
        await self.credentials.take(self.daemon.hub, origins, stop=self.daemon.hard_stop)

    # Preflight

    def _claimed_verify(self) -> list[str]:
        """The verify of a run of one step as its claim names it (``RunSpec.verify``); none from an older hub."""
        return [item for item in self.spec.get("verify") or [] if isinstance(item, str) and item.strip()]

    def run_path(self) -> str | None:
        """The PATH the run's verify commands run with."""
        return self.agent_env().get("PATH")

    async def _preflight(
        self,
        repos,
        *,
        pushes: Collection[str] = (),
        verify: Collection[str] = (),
        hidden: list[str] | None = None,
    ) -> None:
        """Check, once the leases are taken and before anything is fetched or the agent starts, what the run needs
        (``preflight``): each of ``repos`` has an origin in the project and one git reads with what the run holds, and
        pushes to for those of ``pushes``; each program ``verify`` and ``hidden`` (a judge run's hidden checks) call is
        on the run's PATH. RunFailed with every problem found, the cause of the first: the agent does not start."""
        names = list(dict.fromkeys(repo for repo in repos if isinstance(repo, str) and repo))
        problems: list[preflight.Problem] = []
        unlisted = self.credentials.unlisted
        for name in names:
            if name in unlisted:
                problems.append(
                    preflight.Problem(
                        "origin",
                        f"project {self.project} lists no origin for {name}, so no credential is leased for it: add "
                        "the repo with its origin to the harness and run `evo-agents hub project register` again",
                    )
                )
        for name in names:
            if name not in unlisted:
                found = await self._reach(name, push=name in pushes)
                if found is not None:
                    problems.append(found)
        path = self.run_path()
        problems += preflight.verify_problems(verify, path)
        problems += preflight.hidden_problems(hidden or [], path)
        if problems:
            for problem in problems:
                self.note(f"Preflight: {problem.message}.", cause=problem.cause)
            raise RunFailed(_cut(preflight.summary(problems), MAX_ERROR_CHARS), cause=problems[0].cause)
        checked = [f"{len(names)} repo(s)"]
        if verify or hidden:
            checked.append(f"the programs of {len(verify) + len(hidden or [])} command(s)")
        self.note(f"Preflight passed: {' and '.join(checked)}.")

    async def _reach(self, name: str, *, push: bool) -> preflight.Problem | None:
        """The problem git has reading the origin of ``name`` (and pushing to it, with ``push``) with what the run
        holds, or None; a repo without a checkout or an origin here is left to the steps after, which say so."""
        checkout = self.daemon.checkout_for(self.project, name)
        urls = self.credentials.origins.get(name) or []
        if checkout is None or not urls or all(preflight.is_local(url) for url in urls):
            return None
        covered = self.credentials.covers(name)
        if push and self.curator is not None and not covered:
            return preflight.Problem(
                "credentials",
                f"{name}: a run of the Curator pushes only with the credential the hub leased it, and no lease covers "
                f"{urls[0]}: this machine's own credentials are not used",
            )
        with self.credentials.ticketed(self.daemon.env) as env:
            refused = await preflight.can_reach(checkout, self.id, push=push, env=env)
        if refused is None:
            return None
        what, said = refused
        verb = "read" if what == "read" else "push to"
        if covered:
            why = "with the credential the hub leased the run"
        else:
            why = f"with this machine's own credentials (the hub leased none: {self.credentials.unleased.get(name)})"
            if name not in self.credentials.unleased:
                why = "with this machine's own credentials (no lease of the run covers it)"
        return preflight.Problem("credentials", f"git cannot {verb} {name} at {urls[0]} {why}: {said}")

    # The agent

    def agent_env(self) -> dict[str, str]:
        env = dict(self.daemon.env)
        env.update(
            {
                "EVO_RUN_ID": str(self.id),
                "EVO_RUN_KIND": str(self.spec.get("kind") or "step"),
                "EVO_WORKER_HOME": str(self.daemon.home.root),  # the commands of `evo-agents worker` read the run here
                "EVO_RUN_PROJECT": str(self.project),
                "EVO_RUN_PLAN": str(self.spec.get("plan_id") or ""),
                "EVO_RUN_STEP": str(self.spec.get("step_key") or ""),
            }
        )
        env.update(self.credentials.agent_vars)  # env leases, and git's configuration for the leased origins
        if self.subscription_only:  # plan decision 14: the Claude subscription login alone
            env.pop(API_KEY, None)
        return env

    @property
    def subscription_only(self) -> bool:
        """Whether this run's agent is a run of the Curator on Claude Code, which uses the Claude subscription login
        alone, never ANTHROPIC_API_KEY (plan decision 14)."""
        return self.curator is not None and self.runtime == "claude-code"

    async def _check_login(self, cls, context: RunContext) -> None:
        """Fail the run before its agent starts when the runtime may not start it with this machine's login: a run of
        the Curator on Claude Code needs a Claude subscription login (``Adapter.login_refusal``)."""
        if not self.subscription_only:
            return
        has_key = API_KEY in self.daemon.env or API_KEY in self.credentials.agent_vars
        if has_key:
            note = (
                f"{API_KEY} is left out of the agent's environment: a run of the Curator on Claude Code uses the "
                "Claude subscription login alone (plan decision 14)."
            )
            if note not in self._environment_noted:
                self._environment_noted.add(note)
                self.note(note)
        why = await asyncio.to_thread(cls.login_refusal, context)
        if why:
            alone = f"; this machine has {API_KEY} alone, which it does not use" if has_key else ""
            raise RunFailed(why + alone, cause="runtime")

    async def _run_agent(self, cls) -> None:
        """Run the agent until its last turn is over (``_turns``), on the run's prompt."""
        self.outcome, _ = await self._turns(cls, self.spec["prompt"], None, terminal_first=self.mode == "interactive")
        if not self.outcome.completed:
            raise RunFailed(
                self.outcome.error or f"{self.runtime} ended before its turn completed",
                usage=self.outcome.usage,
                cap=self.outcome.cap,
            )

    async def _turns(
        self, cls, prompt: str, session_id: str | None, *, terminal_first: bool
    ) -> tuple[Outcome, str | None]:
        """Run the agent on ``prompt`` (in the session ``session_id``, or a new one) until its turn is over: headless,
        and in its terminal UI while a person drives it (``terminal_first``, or a takeover), each phase going on with
        the session of the one before. How the last headless phase ended, and the session's id."""
        first_prompt = prompt
        in_terminal = terminal_first or self.takeover_asked
        try:
            while True:
                if in_terminal:
                    session_id = await self._in_terminal(cls, session_id, prompt)
                    in_terminal = False
                    prompt = HANDBACK_PROMPT if session_id else first_prompt
                    continue
                outcome = await self._headless(cls, prompt, session_id)
                session_id = self.adapter.session_id or session_id
                self._check()
                if self.takeover_asked and session_id and self.interactive_unsupported(cls) is None:
                    in_terminal = True
                    continue
                return outcome, session_id
        finally:
            self.phase = "after"

    def _agent_starts(self) -> None:
        """The run's timeout counts from the first start of its agent."""
        if not self.agent_started:
            self.agent_started = True
            self.deadline = self.loop.time() + self.timeout_s

    def agent_seconds(self) -> float:
        """The agent time the run has used, as its timeout counts it: none before its agent first started."""
        if not self.agent_started:
            return 0.0
        return max(self.timeout_s - max(self.deadline - self.loop.time(), 0.0), 0.0)

    def _context(self, prompt: str, session_id: str | None) -> RunContext:
        return RunContext(
            run=dict(self.spec),
            worktree=self.worktree,
            prompt=prompt,
            env=self.agent_env(),
            resume_session=session_id,
            leased=self.credentials.withheld,
            spent_usd=self.spent_usd,
            spent_seconds=self.spent_before + self.agent_seconds(),
        )

    def _note_environment(self, cls, context: RunContext) -> None:
        """What the runtime leaves out of the agent's environment because of the run's leases
        (``Adapter.environment_notes``), each line once a run."""
        for line in cls.environment_notes(context):
            if line not in self._environment_noted:
                self._environment_noted.add(line)
                self.note(line)

    async def _headless(self, cls, prompt: str, session_id: str | None) -> Outcome:
        """The agent headless, on a new session or going on with ``session_id``, until its events end."""
        context = self._context(prompt, session_id)
        await self._check_login(cls, context)
        adapter = cls(context)
        self.adapter = adapter
        self.phase = "headless"
        self._inbox_waits = False
        try:
            await adapter.start()
        except Exception as exc:
            log.warning("agent did not start", extra={"run_id": self.id}, exc_info=True)
            raise RunFailed(f"{self.runtime} did not start: {type(exc).__name__}: {exc}") from None
        self.agent_running = True
        self._agent_starts()
        pid = self._group_of(adapter)
        await self._note_agent(pid)
        self.group_noted = pid is not None
        if self._pending_ack is not None:  # the messages its prompt carries are the agent's now
            ack, self._pending_ack = self._pending_ack, None
            self._spawn(self._ack_inbox(ack))
        if session_id:
            self.note(f"{self.runtime} goes on headless in session {session_id}.")
        else:
            self.note(f"{self.runtime} started in {self.worktree}.")
        self._note_environment(cls, context)
        self._spawn(self._report_running((self.state,) if self.state in ("interactive", "waiting") else ("leased",)))
        if self.takeover_asked or self._park_requested():  # asked while the agent was starting
            self._spawn(self._stop_at_boundary(adapter))
        try:
            outcome = await self._consume(adapter)
        finally:
            self.agent_running = False
        # Claude Code's total is the session's running total, which the next agent in the session starts from.
        self.spent_usd = max(self.spent_usd, curator.run_cost(outcome.usage))
        return outcome

    async def _in_terminal(self, cls, session_id: str | None, prompt: str) -> str | None:
        """Hand the agent's session (a new one on ``prompt`` when ``session_id`` is None) to a person: the runtime's
        terminal UI in tmux session evo-run-N, the run reported interactive, until a handback or the person leaves the
        UI. The session's id, for the agent that goes on headless."""
        self.phase = "interactive"
        self.takeover_asked = False
        self.handback_asked.clear()
        tmux = self.daemon.tmux
        name = interactive.session_name(self.id)
        context = self._context(prompt, session_id)
        await self._check_login(cls, context)
        tui = cls.tui(context, session_id)
        try:
            for line in await tui.prepare():
                self.note(line)
            command = tui.command(name)
            await tmux.start(
                name,
                command,
                tui.environment(),
                cwd=self.worktree,
                scratch=self.daemon.home.run_dir(self.id),
                drop=tui.drop_env,
                withheld=self.credentials.withheld,
                env_command=self.credentials.pane_command(),
            )
        except Exception as exc:
            with contextlib.suppress(Exception):
                await tui.close()
            with contextlib.suppress(Exception):
                await tmux.kill(name)
            reason = _cut(f"{type(exc).__name__}: {exc}", 300)
            log.warning("the terminal UI did not open", extra={"run_id": self.id, "error": reason})
            self.terminal_failed = reason
            if self.mode == "interactive" and not self.agent_started:
                raise RunFailed(f"the terminal UI of {self.runtime} did not open: {reason}") from None
            self.note(f"The terminal UI of {self.runtime} did not open ({reason}); the agent goes on headless.")
            return session_id
        self.tui = tui
        self._note_environment(cls, context)
        self._agent_starts()
        await self._note_agent(None)
        self.group_noted = False
        logs = asyncio.create_task(self._terminal_log(tui, name))  # before anyone is told the session is there
        try:
            self.record["tmux_session"] = name
            socket = await tmux.socket_path(name)
            if socket:
                self.record["tmux_socket"] = socket
            with contextlib.suppress(OSError):
                self.daemon.home.save_run(self.record)
            await self._report("interactive", only_from=("leased", "running"), session_id=tui.session_id)
            self.session_reported = tui.session_id
            shown = f", session {tui.session_id}" if tui.session_id else ""
            self.note(
                f"{self.runtime} runs in its terminal UI in tmux session {name}{shown}: `evo-agents worker attach "
                f"{self.id}` on {self.daemon.config.name} opens it, and so does the run's Terminal tab on the web."
            )
            if self.terminal_wanted:
                self.open_terminal()
            how = await self._wait_in_terminal(tui, name)
        finally:
            if self.terminal is not None:
                await self.terminal.close()
            await tmux.kill(name)
            await tui.close()  # sets logs_done: the log reads what came last, then ends
            try:
                await asyncio.wait_for(asyncio.shield(logs), TERMINAL_LOG_GRACE)
            except TimeoutError:
                logs.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await logs
            self.tui = None
        session_id = tui.session_id or session_id
        then = f"{self.runtime} goes on headless" + (f" in session {session_id}" if session_id else "")
        if how == "handback":
            self.note(f"The owner handed the run back: the terminal UI was closed, and {then}.")
        else:
            self.note(f"The terminal UI ended: {then}.")
        return session_id

    async def _wait_in_terminal(self, tui, name: str) -> str:
        """Wait while a person drives the agent: ``handback`` or ``exit`` (the UI ended); Stopped on a cancel, the
        run's timeout or the hub letting go of the run."""
        tmux = self.daemon.tmux
        while True:
            self._check()
            if self.handback_asked.is_set():
                return "handback"
            if not await tmux.alive(name):
                return "exit"
            if tui.session_id and tui.session_id != self.session_reported:  # a new session's id, found late
                self.session_reported = tui.session_id
                self._spawn(self._report_session("interactive", ("interactive",), tui.session_id))
            waits = {asyncio.create_task(self.handback_asked.wait()), asyncio.create_task(self._stop.wait())}
            try:
                await asyncio.wait(
                    waits,
                    timeout=max(0.0, min(TERMINAL_POLL, self.deadline - self.loop.time())),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in waits:
                    task.cancel()

    async def _terminal_log(self, tui, name: str) -> None:
        """The run's log while a person drives the agent: the runtime's record of the session, or what the terminal
        prints when the runtime keeps none this worker reads."""
        source = tui.logs()
        pane_log = None
        try:
            if source is None:
                pane_log = self.daemon.home.run_dir(self.id) / "terminal.log"
                await self.daemon.tmux.pipe(name, pane_log)
                self.note(
                    "The terminal UI keeps no record this worker reads: the log shows the text its terminal prints."
                )
                source = interactive.follow_pane(pane_log, tui.logs_done)
            async for event in source:
                self.event(event.kind, event.body, event.at)
        except Exception:
            log.warning("the log of the terminal failed", extra={"run_id": self.id}, exc_info=True)
        finally:
            if pane_log is not None:
                pane_log.unlink(missing_ok=True)

    def _group_of(self, adapter) -> int | None:
        try:
            pid = adapter.group_pid()
        except Exception:  # an adapter's bug must not stop the run
            log.warning("the adapter did not name the agent's process group", extra={"run_id": self.id}, exc_info=True)
            return None
        return pid if isinstance(pid, int) and pid > 1 else None

    async def _note_agent(self, pid: int | None) -> None:
        """Note the agent that runs now in runs/<run>/agent.json (``orphans``): the process group ``pid`` leads, or
        none (an agent in its terminal UI, whose tmux session a daemon that starts closes, or an adapter that does not
        name its group). The file stays until the run ends here."""
        agent = await asyncio.to_thread(orphans.describe, pid)
        agent.update({"runtime": self.runtime, "phase": self.phase})
        try:
            self.daemon.home.save_agent(self.id, agent)
        except OSError as exc:
            log.warning("agent.json not written", extra={"run_id": self.id, "error": str(exc)})

    async def _consume(self, adapter) -> Outcome:
        async def pump() -> None:
            looks = 0
            async for item in adapter.events():
                if isinstance(item, AgentEvent):
                    self.event(item.kind, item.body, item.at)
                if not self.group_noted and looks < NOTE_GROUP_LOOKS:  # a runtime that started after start returned
                    looks += 1
                    pid = self._group_of(adapter)
                    if pid is not None:
                        await self._note_agent(pid)
                        self.group_noted = True
                session_id = adapter.session_id
                if session_id and session_id != self.session_reported and self.state == "running":
                    self.session_reported = session_id
                    self._spawn(self._report_running(("running",)))

        pump_task = asyncio.create_task(pump())
        stop_task = asyncio.create_task(self._stop.wait())
        try:
            done, _ = await asyncio.wait(
                {pump_task, stop_task},
                timeout=max(0.0, self.deadline - self.loop.time()),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stop_task.cancel()
        if pump_task not in done:
            if self.stop_reason is None:
                self.stop_reason = "timeout"
            self.note(f"Stopping the agent: {self._why(self.stop_reason)}.")
            await self._interrupt_agent()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(pump_task), STOP_GRACE)
            if not pump_task.done():
                pump_task.cancel()
        await asyncio.wait({pump_task})  # done, or done cancelling; raises nothing of the task's
        if not pump_task.cancelled() and pump_task.exception() is not None:
            log.warning("the adapter's events failed", extra={"run_id": self.id}, exc_info=pump_task.exception())
        try:
            return await asyncio.wait_for(adapter.wait(), STOP_GRACE)
        except TimeoutError:
            return Outcome(False, f"{self.runtime} did not end within {STOP_GRACE:g}s of being stopped")
        except Exception as exc:
            return Outcome(False, f"{self.runtime} failed: {type(exc).__name__}: {exc}")

    @staticmethod
    def _why(reason: str) -> str:
        return {
            "cancel": "the owner cancelled the run",
            "timeout": "the run reached its timeout",
            "gone": "the hub no longer holds the run for this worker",
            "shutdown": "the worker is stopping",
            "watchdog": "the watchdog of the Curator's runs stopped it",
        }.get(reason, reason)

    # The watchdog of a run of the Curator

    def _watched(self) -> list[tuple[str, Path, str]]:
        """(repo, worktree, the commit it started at) of each worktree of the run."""
        if self.worktree is None or self.base is None or not self.repo:
            return []
        return [(self.repo, self.worktree, self.base)]

    async def watch(self) -> str | None:
        """For a run of the Curator, after a heartbeat: compare each worktree with the charter's protected paths, and
        the agent time and cost with the run's caps; stop the run when one is passed (``watchdog_reason`` says why).
        Why it stopped the run, or None."""
        if self.curator is None or self.ended or self.stop_reason is not None:
            return None
        protected = [glob for glob in self.curator.get("protected_paths") or [] if isinstance(glob, str)]
        why = None
        for repo, worktree, base in self._watched():
            if not protected or not worktree.is_dir():
                continue
            try:
                paths = await gitops.changed_paths(worktree, base)
            except gitops.GitError as exc:
                log.warning("the watchdog could not read a worktree", extra={"run_id": self.id, "error": str(exc)})
                continue
            hit = next(((path, glob) for path in paths for glob in protected if tiers.matches(glob, repo, path)), None)
            if hit is not None:
                why = f"{repo}:{hit[0]} is protected by the charter ({hit[1]})"
                break
        cap_seconds, cap_usd = self.budget.get("max_seconds"), self.budget.get("max_usd")
        if why is None and cap_seconds is not None and self.spent_before + self.agent_seconds() > cap_seconds:
            why = f"the run used more than its time cap of {int(cap_seconds) // 60} minutes of agent time"
        if why is None and cap_usd is not None and max(self.cost_seen, self.spent_usd) > cap_usd:
            why = f"the run cost more than its cost cap of {curator.money(cap_usd)}"
        if why is None or self.ended or self.stop_reason is not None:
            return None
        self.watchdog_reason = why
        log.warning("the watchdog stops a run of the Curator", extra={"run_id": self.id, "why": why})
        self.request_stop("watchdog")
        return why

    async def _interrupt_agent(self) -> None:
        if self.adapter is None or not self.agent_running:
            return
        try:
            await asyncio.wait_for(self.adapter.interrupt(), STOP_GRACE)
        except Exception:
            log.warning("interrupting the agent failed", extra={"run_id": self.id}, exc_info=True)

    # The agent's result and the verify commands

    def _read_result(self) -> list[str]:
        path = self.worktree / runs.RESULT_FILE
        if path.is_symlink() or not path.is_file():
            raise RunFailed(f"the agent did not write {runs.RESULT_FILE}", usage=self.outcome.usage)
        if path.stat().st_size > RESULT_MAX_BYTES:
            raise RunFailed(f"{runs.RESULT_FILE} is over {RESULT_MAX_BYTES} bytes")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RunFailed(f"{runs.RESULT_FILE} is not JSON: {exc}") from None
        commands = data.get("verify_commands") if isinstance(data, dict) else None
        if not isinstance(commands, list) or not commands:
            raise RunFailed(f"{runs.RESULT_FILE} lists no verify_commands, so the worker cannot check the step")
        if len(commands) > MAX_VERIFY:
            raise RunFailed(f"{runs.RESULT_FILE} lists {len(commands)} verify commands; at most {MAX_VERIFY} are run")
        for command in commands:
            if not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND_CHARS:
                raise RunFailed(
                    f"each verify command in {runs.RESULT_FILE} is a shell command of 1 to {MAX_COMMAND_CHARS} "
                    "characters"
                )
        summary = data.get("summary")
        self.summary = (
            _cut(summary.strip(), MAX_SUMMARY_CHARS) if isinstance(summary, str) and summary.strip() else None
        )
        return commands

    async def _run_verify(self, commands: list[str]) -> list[dict]:
        results = []
        self.note(f"Running the {len(commands)} verify command(s) of {runs.RESULT_FILE} again.")
        for command in commands:
            self._check()
            started = self.loop.time()
            code, output = await self._shell(command, self.deadline - started)
            duration_ms = int((self.loop.time() - started) * 1000)
            results.append({"command": command, "exit_code": code, "duration_ms": duration_ms})
            self.event(
                "system",
                {
                    "text": f"verify: `{_cut(command, 200)}` exited {code} after {duration_ms} ms",
                    "command": command,
                    "exit_code": code,
                    "duration_ms": duration_ms,
                    "output": output,
                },
            )
        return results

    async def _shell(self, command: str, seconds: float, cwd: Path | None = None) -> tuple[int, str]:
        """(exit code, the end of its output) of ``command`` in the worktree (or ``cwd``); Stopped when the run is
        asked to stop or runs out of time first, with the command's process group killed."""
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd or self.worktree),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self.agent_env(),
            start_new_session=True,
        )
        tail = bytearray()

        async def read() -> None:
            while chunk := await proc.stdout.read(65536):
                tail.extend(chunk)
                del tail[:-OUTPUT_TAIL]

        reader = asyncio.create_task(read())
        waiter = asyncio.create_task(proc.wait())
        stopper = asyncio.create_task(self._stop.wait())
        try:
            done, _ = await asyncio.wait(
                {waiter, stopper}, timeout=max(0.0, seconds), return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            stopper.cancel()
        if waiter not in done:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            await waiter
            reader.cancel()
            if self.stop_reason is None:
                self.stop_reason = "timeout"
            raise Stopped(self.stop_reason)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(reader, 5)
        return proc.returncode, tail.decode(errors="replace")

    # Commit and push

    async def _commit(self, path: Path, repo: str | None = None) -> bool:
        """Commit what the agent left in the work tree at ``path`` as ``run #N: <title>``, and say in the run's log
        which files the commit leaves out (``gitops.commit_run``); whether a commit was made."""
        message = f"run #{self.id}: {self.title}"
        committed = await gitops.commit_run(path, message)
        extra = {"repo": repo} if repo else {}
        where = f" in {repo}" if repo else ""
        if committed.left_out:
            left = committed.left_out
            shown = ", ".join(left[:MAX_LEFT_OUT_NAMED])
            more = f" and {len(left) - MAX_LEFT_OUT_NAMED} more" if len(left) > MAX_LEFT_OUT_NAMED else ""
            self.note(
                f"Left out of the commit{where}, as what a hook or a plan export wrote rather than the agent's work: "
                f"{shown}{more}.",
                left_out=list(left[:MAX_LEFT_OUT_LISTED]),
                **extra,
            )
        if committed.made:
            self.note(f"Committed what the agent left{where or ' uncommitted'} as {message}.", **extra)
        return committed.made

    async def _commit_and_push(self) -> tuple[str, dict]:
        wt = self.worktree
        try:
            await self._commit(wt)
        except gitops.GitError as exc:
            raise RunFailed(f"committing what the agent left failed: {exc}", verify=self.verify) from None
        head_branch = await gitops.current_branch(wt)
        if head_branch is None:
            raise RunFailed(
                "HEAD is detached in the worktree: the worker does not push a detached HEAD", verify=self.verify
            )
        if head_branch != self.local_branch:
            raise RunFailed(
                f"the worktree is on {head_branch}, not {self.local_branch}: the agent switched branches, and the "
                "worker does not push it",
                verify=self.verify,
            )
        commit_sha = await gitops.rev(wt, "HEAD")
        diffstat = await gitops.diffstat(wt, self.base)
        self._check()

        async def push() -> gitops.Pushed:
            with self.credentials.ticketed(self.daemon.env) as env:
                return await gitops.push(wt, self.branch, protected=self.protected, kind="step", env=env)

        try:  # a default branch was refused before the agent started; gitops refuses it again before every push
            pushed = await self.credentials.with_renewal(self.repo, push)
        except gitops.PushRefused as exc:
            raise RunFailed(str(exc), verify=self.verify) from None
        except gitops.GitError as exc:
            cause = "credentials" if isinstance(exc, gitops.GitAuthError) else None
            raise RunFailed(
                f"git push to {self.branch} on origin failed: {exc}", verify=self.verify, cause=cause
            ) from None
        if pushed.changed:
            self.note(
                f"Pushed {commit_sha[:12]} to {self.branch} on origin ({diffstat['files']} file(s), "
                f"+{diffstat['insertions']} -{diffstat['deletions']}).",
                commit_sha=commit_sha,
            )
        else:
            self.note(
                f"{self.branch} on origin has {commit_sha[:12]} already: nothing to push.",
                commit_sha=commit_sha,
            )
        self.record.update({"commit_sha": commit_sha, "pushed": True})
        return commit_sha, diffstat

    # Log and diff

    async def _diff(self) -> bytes:
        """The diff of the run's commits, empty when there is none."""
        if self.worktree is None or self.base is None:
            return b""
        with contextlib.suppress(gitops.GitError, OSError, asyncio.TimeoutError):
            return await gitops.diff(self.worktree, self.base)
        return b""

    async def _upload(self) -> None:
        """Upload the run's log and diff to the hub's blob store; a hub without one, or a failure, is logged only."""
        payloads: dict[str, bytes] = {}
        items = []
        with contextlib.suppress(OSError):
            if 0 < self.log_path.stat().st_size <= LOG_LIMIT:
                payloads["run-log"] = self.log_path.read_bytes()
        diff = await self._diff()
        if 0 < len(diff) <= DIFF_LIMIT:
            payloads["run-diff"] = diff
        if not payloads:
            return
        by_sha = {}
        for kind, data in payloads.items():
            sha = hashlib.sha256(data).hexdigest()
            by_sha[sha] = data
            items.append({"sha256": sha, "size": len(data), "kind": kind})
        try:
            answer = await self.daemon.hub.uploads(self.id, items)
            ids = []
            for ticket in answer.get("uploads") or []:
                await self.daemon.hub.put(ticket["url"], by_sha[ticket["sha256"]])
                ids.append(ticket["upload_id"])
            if ids:
                await self.daemon.hub.commit_blobs(self.id, ids)
            log.info("run log and diff uploaded", extra={"run_id": self.id, "kinds": sorted(payloads)})
        except (HubProblem, KeyError, TypeError) as exc:
            log.info("run log and diff not uploaded", extra={"run_id": self.id, "error": str(exc)})


def read_asked(home, run_id: int) -> set[int]:
    """The decisions the agent of plan run ``run_id`` asked, as ``evo-agents worker ask`` noted them."""
    asked: set[int] = set()
    with contextlib.suppress(OSError):
        for line in home.decisions_path(run_id).read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                decision_id = json.loads(line).get("id")
                if isinstance(decision_id, int):
                    asked.add(decision_id)
    return asked


class PlanRun(Run):
    """A plan run (kind ``plan``, see the module's docstring): a worktree of each of its repos in one directory, the
    agent in that directory doing every step not done yet, waiting for its owner's answers between its turns, and the
    repos pushed at its end."""

    def __init__(self, daemon: Daemon, spec: dict):
        super().__init__(daemon, spec)
        self.title = spec.get("title") or f"plan {spec.get('plan_id')}"
        self.directory: Path | None = None
        self.workspaces: dict[str, gitops.Workspace] = {}
        self.park_asked = asyncio.Event()
        self.parked = False
        self.resume_of = int(spec["resume_of_run_id"]) if spec.get("resume_of_run_id") else None
        self.session_id: str | None = spec.get("session_id") or None
        self.targets: dict[str, str] = {}  # a Builder's repo -> the default branch its merge request goes into
        self._push_lock = asyncio.Lock()  # one push of the run at a time, the daemon's own or one its agent asked for
        if self.builder:  # its agent pushes nothing itself: `evo-agents worker step` asks the daemon
            self.credentials.pusher = self._push_for_agent
        self.record.update({"dir": None, "repos": [], "resume_of_run_id": self.resume_of})
        daemon.home.save_run(self.record)

    # Control

    def request_park(self) -> None:
        """The heartbeat says the hub parked the run, or that a new run resumes it: the agent stops at the end of its
        turn, and its session and worktrees stay."""
        if self.ended or self.park_asked.is_set():
            return
        log.info("park asked", extra={"run_id": self.id, "phase": self.phase})
        self.park_asked.set()
        if self.agent_running and self.adapter is not None:
            self.note("The hub parked the run: the agent stops at the end of its turn.")
            self._spawn(self._stop_at_boundary(self.adapter))
        if self.phase == "interactive":
            self.handback_asked.set()

    def _park_requested(self) -> bool:
        return self.park_asked.is_set()

    # The run

    async def _go(self) -> None:
        try:
            await super()._go()
        except Parked:
            await self._park()

    async def _steps(self) -> None:
        spec = self.spec
        repos = [entry for entry in spec.get("repos") or [] if isinstance(entry, dict)]
        names = ", ".join(str(entry.get("repo")) for entry in repos) or "no repo"
        resumes = f", going on from parked run #{self.resume_of}" if self.resume_of else ""
        self.note(
            f"Run #{self.id} claimed by worker {self.daemon.config.name}: plan {spec.get('plan_id')} over {names}, "
            f"{self.runtime}, {self.mode}, timeout {self.timeout_s // 60} min of agent time{resumes}."
        )
        cls = self.daemon.adapters.get(self.runtime)
        if cls is None:
            raise RunFailed(f"this worker has no adapter for {self.runtime}", cause="runtime")
        if self.mode == "interactive":
            why = self.interactive_unsupported(cls)
            if why is not None:
                raise RunFailed(
                    f"this worker cannot run {self.runtime} interactive ({why}): run it headless", cause="runtime"
                )
        if not repos:
            raise RunFailed("the plan run names no repo to work in")
        names = [entry.get("repo") for entry in repos]
        await self._take_credentials(names)
        self._check()
        await self._preflight(names, pushes=names, verify=runs.open_verify(self._plan_body()))
        self._check()
        await self._prepare_plan(repos)
        self._check()
        prompt, session_id = await self._first_turn()
        await self._plan_turns(cls, prompt, session_id)
        self.summary = self._read_summary()
        await self._ensure_running()
        await self._report("verifying")
        self._check()
        diffstat = await self._push_repos()
        await self._end(
            "done", diffstat=diffstat, summary=self.summary, usage=self.outcome.usage if self.outcome else None
        )

    # The directory and its worktrees

    def _plan_body(self) -> dict:
        plan = self.spec.get("plan")
        body = plan.get("body") if isinstance(plan, dict) else None
        return body if isinstance(body, dict) else {}

    def _watched(self) -> list[tuple[str, Path, str]]:
        return [(name, workspace.worktree, workspace.base) for name, workspace in self.workspaces.items()]

    @property
    def builder(self) -> bool:
        """Whether this plan run is a Builder of the Curator, which pushes its branch curator/... alone."""
        return self.curator is not None and self.curator.get("role") == "builder"

    def _save_workspaces(self) -> None:
        self.record["dir"] = str(self.directory) if self.directory is not None else None
        self.record["repos"] = [workspace.to_record() for workspace in self.workspaces.values()]
        self.daemon.home.save_run(self.record)

    async def _prepare_plan(self, repos: list[dict]) -> None:
        """The run's directory with a worktree of each repo: the parked run's when this run resumes one and they are
        still here, else new ones; and the plan as claimed in .evo-run/plan.yaml."""
        if self.resume_of is not None:
            self._adopt(self.resume_of)
        if self.directory is None:
            self.directory = self.daemon.home.worktree_path(self.project, self.id)
            if self.directory.exists():
                raise RunFailed(f"{self.directory} exists already; remove it and run the plan again")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.worktree = self.directory  # the agent's directory
        named = gitops.plan_branches(self._plan_body())
        for name, workspace in list(self.workspaces.items()):  # a default branch is pushed only while the plan names it
            self.workspaces[name] = dataclasses.replace(workspace, plan_branch=named.get(name))
        taken = {workspace.worktree.name for workspace in self.workspaces.values()}
        for entry in repos:
            name = entry.get("repo")
            if not isinstance(name, str) or not name:
                raise RunFailed("the plan run names a repo without a name")
            if name in self.workspaces:
                continue
            branch = entry.get("branch")
            if not isinstance(branch, str) or not branch:
                raise RunFailed(
                    f"the plan names no branch for {name}: a plan run works only on the branch the plan names for "
                    "each repo; add it to the plan's repos"
                )
            checkout = self.daemon.checkout_for(self.project, name)
            if checkout is None:
                raise RunFailed(f"this worker has no checkout of {self.project}/{name}")
            folder = gitops.folder_name(name, taken)
            taken.add(folder)
            self.workspaces[name] = await self._make_worktree(name, branch, named.get(name), checkout, folder)
            self._save_workspaces()
        self._write_plan()
        self._save_workspaces()

    async def _make_worktree(
        self, name: str, branch: str, plan_branch: str | None, checkout: Path, folder: str
    ) -> gitops.Workspace:
        """A worktree of the checkout of ``name`` at ``<directory>/<folder>`` on the plan's branch for it, from
        origin's branch when there is one (see ``Run._prepare``), or on evo-run/<run>/<folder> when that branch is
        checked out elsewhere or has commits here that the start lacks."""
        path = self.directory / folder
        async with self.daemon.repo_lock(checkout):
            if not await gitops.has_remote(checkout):
                raise RunFailed(f"the checkout at {checkout} has no remote named origin to fetch from and push to")
            await self._fetch(checkout)
            if not await gitops.check_branch_name(checkout, branch):
                raise RunFailed(f"the plan's branch {branch!r} for {name} is not a valid branch name")
            remote_head = await gitops.remote_default_branch(checkout)
            hub_default = default_branch_of(self.daemon.config, self.project, name)
            protected = tuple(sorted({item for item in (remote_head, hub_default, *gitops.PROTECTED) if item}))
            kind = "curator" if self.builder else "plan"
            try:
                default = gitops.check_push(branch, protected, kind=kind, plan_branch=plan_branch)
            except gitops.PushRefused as exc:
                raise RunFailed(f"the run's branch for {name}: {exc}") from None
            if self.builder:
                self.targets[name] = remote_head or hub_default or "main"
                self.record.setdefault("curator", {})["targets"] = dict(self.targets)
            start = base = None
            for ref in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}", "refs/remotes/origin/HEAD", "HEAD"):
                base = await gitops.rev(checkout, ref)
                if base is not None:
                    start = ref
                    break
            if base is None:
                raise RunFailed(f"the checkout at {checkout} has no commit to start from")
            if path.exists():
                raise RunFailed(f"{path} exists already; remove it and run the plan again")
            in_use = await gitops.branches_in_worktrees(checkout)
            local = await gitops.rev(checkout, f"refs/heads/{branch}")
            if branch not in in_use and (local is None or await gitops.is_ancestor(checkout, local, base)):
                local_branch = branch
            else:
                local_branch = RUN_REPO_BRANCH.format(id=self.id, folder=folder)
                why = "is checked out in another worktree" if branch in in_use else "has commits here that it lacks"
                self.note(f"{branch} of {name} {why}; the run works on {local_branch} and pushes it to {branch}.")
            try:
                await gitops.add_worktree(checkout, path, local_branch, base, reset=True)
            except gitops.GitError as exc:
                raise RunFailed(f"cannot make the worktree {path}: {exc}") from None
        pushes = f"; pushes go to {branch}" + (", a default branch the plan names for it" if default else "")
        self.note(f"Worktree {path} of {name} on {local_branch} at {base[:12]} ({start}){pushes}.", worktree=str(path))
        return gitops.Workspace(
            repo=name,
            branch=branch,
            plan_branch=plan_branch,
            checkout=checkout,
            worktree=path,
            local_branch=local_branch,
            base=base,
            protected=protected,
        )

    def _adopt(self, old_id: int) -> None:
        """Take over the directory, worktrees, session and decisions of parked run ``old_id``, which this run resumes;
        its record no longer names them, so the cleanup of that run leaves them alone."""
        home = self.daemon.home
        record = home.load_run(old_id)
        resumable = record is not None and record.get("kind") in runs.RESUME_KINDS
        directory = Path(record["dir"]) if resumable and record.get("dir") else None
        if directory is None or not directory.is_dir():
            self.note(
                f"The worktrees of parked run #{old_id} are not on this worker any more: the run makes new ones from "
                "the branches on origin, and goes on in the session of the parked run."
            )
            return
        for item in record.get("repos") or []:
            try:
                workspace = gitops.Workspace.from_record(item)
            except ValueError:
                continue
            if workspace.worktree.is_dir():
                self.workspaces[workspace.repo] = workspace
        self.directory = directory
        self.session_id = self.session_id or record.get("session_id")
        self.answered |= {item for item in record.get("answered") or [] if isinstance(item, int)}
        still_open = sorted(read_asked(home, old_id) - self.answered)
        if still_open:
            lines = "".join(json.dumps({"id": decision_id}) + "\n" for decision_id in still_open)
            with open(home.decisions_path(self.id), "a", encoding="utf-8") as handle:
                handle.write(lines)
        record.update({"resumed_by": self.id, "dir": None, "repos": [], "moved_to": str(directory)})
        home.save_run(record)
        names = ", ".join(self.workspaces) or "no repo"
        self.note(f"Goes on from parked run #{old_id} in {directory}, with its worktrees of {names}.")

    def _write_plan(self) -> None:
        plan = self.spec.get("plan") if isinstance(self.spec.get("plan"), dict) else {}
        path = self.directory / runs.PLAN_FILE
        path.parent.mkdir(mode=0o700, exist_ok=True)
        header = (
            f"# The plan {self.spec.get('plan_id')} of project {self.project} at revision {plan.get('revision')}, as "
            f"run #{self.id} was claimed. `evo-agents worker plan` prints it as the hub holds it now.\n"
        )
        text = yaml.safe_dump(self._plan_body(), allow_unicode=True, sort_keys=False, width=120)
        path.write_text(header + text, encoding="utf-8")

    async def _release_branch(self) -> None:
        """Leave each plan branch once the run is over, so it can be checked out elsewhere; a parked run keeps its
        worktrees as they are, for the run that resumes it."""
        if self.parked:
            return
        for workspace in self.workspaces.values():
            if workspace.local_branch == workspace.branch and workspace.worktree.exists():
                with contextlib.suppress(gitops.GitError, OSError):
                    await gitops.detach(workspace.worktree)

    # The agent's turns

    def _prompt(self) -> str:
        """The run's prompt; built again here when a worktree's folder is not named as its repo."""
        folders = {name: workspace.worktree.name for name, workspace in self.workspaces.items()}
        if all(name == folder for name, folder in folders.items()) and self.spec.get("prompt"):
            return self.spec["prompt"]
        return runs.build_plan_prompt(self._plan_body(), self.spec.get("repos") or [], folders)

    async def _first_turn(self) -> tuple[str, str | None]:
        """The prompt and session of the agent's first turn: the run's prompt in a new session, or, for a run that
        resumes a parked one, the owner's answers in that run's session."""
        if self.resume_of is None:
            return self._prompt(), None
        messages = await self._unread()
        said = self._take(messages) if messages else ""
        if self.session_id is None:  # the parked run never told its session: start again, with the answers
            self.note("The parked run left no session to go on with: the agent starts a new one.")
            return runs.clip(self._prompt() + ("\n\n" + said if said else ""), runs.MAX_PROMPT_BYTES), None
        prompt = RESUME_PROMPT.format(id=self.id) + ("\n\n" + said if said else "")
        return runs.clip(prompt, runs.MAX_PROMPT_BYTES), self.session_id

    async def _plan_turns(self, cls, prompt: str, session_id: str | None) -> None:
        """Turns of the agent until one ends with nothing to wait for: after each, the owner's messages that came in
        the meantime, or the answer to a decision the run waits for, start the next in the same session."""
        terminal_first = self.mode == "interactive"
        while True:
            self.outcome, session_id = await self._turns(cls, prompt, session_id, terminal_first=terminal_first)
            terminal_first = False
            self.session_id = session_id or self.session_id
            self.record["session_id"] = self.session_id
            if self.park_asked.is_set():
                raise Parked()
            if not self.outcome.completed:
                raise RunFailed(
                    self.outcome.error or f"{self.runtime} ended before its turn completed",
                    usage=self.outcome.usage,
                    cap=self.outcome.cap,
                )
            messages = await self._after_turn()
            if not messages:
                return
            prompt = runs.clip(self._reply_prompt() + "\n\n" + self._take(messages), runs.MAX_PROMPT_BYTES)

    def _reply_prompt(self) -> str:
        """What the agent's next turn starts with, before the owner's messages that start it."""
        return ANSWER_PROMPT

    async def _unread(self) -> list[dict]:
        """The inbox's messages the agent has not had yet; none when the hub does not answer."""
        try:
            messages = await self.daemon.hub.inbox(self.id)
        except HubProblem as exc:
            log.warning("the inbox was not read", extra={"run_id": self.id, "error": str(exc)})
            return []
        return [
            message
            for message in messages
            if isinstance(message, dict) and isinstance(message.get("id"), int) and message["id"] > self._delivered_upto
        ]

    def _take(self, messages: list[dict]) -> str:
        """The text of these messages for the agent's next prompt; they are acknowledged once it starts."""
        self._handed(messages)
        self._pending_ack = self._delivered_upto
        return "\n\n".join(str(message.get("text") or "") for message in messages)

    def open_asked(self) -> set[int]:
        """The decisions the agent asked whose answers it has not had."""
        return read_asked(self.daemon.home, self.id) - self.answered

    async def _ensure_running(self) -> None:
        """Report running unless the hub has it so already (the report spawned when the agent started may still be
        on its way), so the next move starts from it."""
        if self.state in ("leased", "waiting"):
            await self._report("running", only_from=("leased", "waiting"), session_id=self.session_id)

    async def _after_turn(self) -> list[dict]:
        """What starts the agent's next turn: the owner's messages it has not had, at once; else, while a decision of
        the run is open, the messages that come once the owner answers (the run waits). None when nothing is left
        to wait for."""
        unread = await self._unread()
        if unread:
            self.note(f"{len(unread)} message(s) of the owner came while the agent worked: a new turn takes them.")
            return unread
        asked = self.open_asked()
        if not asked and self.open_decisions <= 0:
            return []
        await self._ensure_running()
        try:
            await self._report("waiting", only_from=("running",))
        except ReportRefused as exc:
            if exc.status != 409:
                raise
            self.note(f"The agent's turn ended, and the hub has no decision of the run to wait for ({exc}).")
            return []
        if self.state != "waiting":
            return []
        shown = ", ".join(f"#{decision_id}" for decision_id in sorted(asked)) or "of the run"
        self.note(
            f"The agent's turn ended with decision {shown} open: the run waits for its owner's answer, and the time it "
            "waits does not count toward its timeout."
        )
        return await self._wait_for_answer()

    async def _wait_for_answer(self) -> list[dict]:
        """Wait for the owner's messages, the answer to a decision among them, without the agent: the worker keeps the
        run and the heartbeat its lease. Stopped on a cancel or when the hub lets go of the run, Parked when it parks
        it, and nothing once the owner ended the chat of an author run. The time spent here moves the run's deadline
        back."""
        started = self.loop.time()
        try:
            while True:
                if self.stop_reason is not None:
                    raise Stopped(self.stop_reason)
                if self.park_asked.is_set():
                    raise Parked()
                if self.finish_asked.is_set():
                    return []
                self.inbox_arrived.clear()
                messages = await self._unread()
                if messages:
                    answers = sorted(m["decision_id"] for m in messages if isinstance(m.get("decision_id"), int))
                    which = f" answering decision {', '.join(f'#{item}' for item in answers)}" if answers else ""
                    self.note(f"The owner wrote{which}: the agent goes on in session {self.session_id or '(new)'}.")
                    return messages
                await _first_of(
                    (self.inbox_arrived, self._stop, self.park_asked, self.finish_asked), self.daemon.heartbeat_s
                )
        finally:
            self.deadline += self.loop.time() - started

    async def _park(self) -> None:
        """The hub parked the run: its slot is free; the session and the worktrees stay for the run that resumes
        it, and the hub hears nothing more of this one but its events."""
        await self._interrupt_agent()
        self.parked = True
        self.state = "parked"
        self.record.update({"state": "parked", "parked_at": _now().isoformat(), "session_id": self.session_id})
        self.note(
            f"The hub parked the run: the agent's turn is over, and its session {self.session_id or '(none)'} and the "
            f"worktrees in {self.directory} stay on this worker for the run that resumes it."
        )
        self.ended = True
        log.info("run parked", extra={"run_id": self.id, "session_id": self.session_id})

    # The end

    def _read_summary(self) -> str | None:
        """The agent's summary from .evo-run/result.json in the run's directory; a plan run lists no verify commands."""
        path = self.directory / runs.RESULT_FILE
        if path.is_symlink() or not path.is_file():
            self.note(f"The agent wrote no {runs.RESULT_FILE}: the run ends without its summary.")
            return None
        try:
            if path.stat().st_size > RESULT_MAX_BYTES:
                raise ValueError(f"it is over {RESULT_MAX_BYTES} bytes")
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.note(f"{runs.RESULT_FILE} was not read ({exc}): the run ends without its summary.")
            return None
        summary = data.get("summary") if isinstance(data, dict) else None
        return _cut(summary.strip(), MAX_SUMMARY_CHARS) if isinstance(summary, str) and summary.strip() else None

    async def _named_branches(self) -> dict[str, str | None]:
        """repo -> the branch the plan names for it as the hub holds it now, so a default branch is pushed only while
        the plan still names it; as the plan was claimed when the hub does not say."""
        try:
            view = await self.daemon.hub.plan(self.id)
        except HubProblem as exc:
            log.warning("the plan was not read again before the push", extra={"run_id": self.id, "error": str(exc)})
            view = None
        body = view.get("body") if isinstance(view, dict) else None
        if isinstance(body, dict) and "repos" in body:
            return gitops.plan_branches(body)
        return {name: workspace.plan_branch for name, workspace in self.workspaces.items()}

    async def _push_repos(self) -> dict:
        """Commit what the agent left in each repo, and push each repo's branch whose origin lacks its commits; the
        diffstat of the whole run."""
        total = {"files": 0, "insertions": 0, "deletions": 0}
        named = await self._named_branches()
        for name, workspace in self.workspaces.items():
            path = workspace.worktree
            if not path.is_dir():
                raise RunFailed(f"the worktree of {name} at {path} is gone")
            try:
                await self._commit(path, name)
            except gitops.GitError as exc:
                raise RunFailed(f"committing what the agent left in {name} failed: {exc}") from None
            head_branch = await gitops.current_branch(path)
            if head_branch is None:
                raise RunFailed(f"HEAD is detached in the worktree of {name}: the worker does not push a detached HEAD")
            if head_branch != workspace.local_branch:
                raise RunFailed(
                    f"the worktree of {name} is on {head_branch}, not {workspace.local_branch}: the agent switched "
                    "branches, and the worker does not push it"
                )
            self._check()
            options = self._push_options(name)
            if self.builder and self._forge_origin(name) and not self.credentials.covers(name):
                raise RunFailed(
                    f"{name}: a run of the Curator pushes only with the credential the hub leased it, and no lease "
                    "covers its origin: this machine's own credentials are not used"
                )

            async def push(path=path, workspace=workspace, name=name, options=options) -> gitops.Pushed:
                with self.credentials.ticketed(self.daemon.env) as env:
                    return await gitops.push(
                        path,
                        workspace.branch,
                        protected=workspace.protected,
                        kind="curator" if self.builder else "plan",
                        plan_branch=named.get(name),
                        env=env,
                        options=options,
                    )

            try:
                async with self._push_lock:
                    pushed = await self.credentials.with_renewal(name, push)
            except gitops.PushRefused as exc:
                raise RunFailed(f"{name}: {exc}") from None
            except gitops.GitError as exc:
                cause = "credentials" if isinstance(exc, gitops.GitAuthError) else None
                raise RunFailed(
                    f"git push of {name} to {workspace.branch} on origin failed: {exc}", cause=cause
                ) from None
            stat = await gitops.diffstat(path, workspace.base)
            for key in total:
                total[key] += stat[key]
            if pushed.changed:
                self.note(
                    f"Pushed {pushed.head[:12]} of {name} to {workspace.branch} on origin ({len(pushed.commits)} "
                    "commit(s)).",
                    commit_sha=pushed.head,
                    repo=name,
                )
                if pushed.default:
                    await self._notice(name, pushed)
            else:
                self.note(f"{workspace.branch} of {name} on origin has {pushed.head[:12]} already: nothing to push.")
        return total

    async def _push_for_agent(self, name: str, title: str | None) -> dict:
        """Push the run's branch of repo ``name`` for the Builder's agent, which reported a step done and holds no
        credential: what is committed in its worktree, to its branch curator/... alone, with the run's lease, and on
        GitLab with the push options that open the merge request. The answer the agent's command prints, or why not."""
        workspace = self.workspaces.get(name)
        if not self.builder or workspace is None:
            return {"error": f"run {self.id} pushes no repo {name} for its agent"}
        try:
            head_branch = await gitops.current_branch(workspace.worktree)
            if head_branch != workspace.local_branch:
                return {
                    "error": f"{workspace.worktree} is on {head_branch or 'a detached HEAD'}, not "
                    f"{workspace.local_branch}: the daemon pushes the run's branch alone"
                }
            if self._forge_origin(name) and not self.credentials.covers(name):
                return {
                    "error": f"{name}: a run of the Curator pushes only with the credential the hub leased it, and no "
                    "lease covers its origin"
                }
            options = (
                judge.gitlab_push_options(self.targets.get(name) or "main", title)
                if self.curator.get("forge") == "gitlab"
                else []
            )

            async def push() -> gitops.Pushed:
                with self.credentials.ticketed(self.daemon.env) as env:
                    return await gitops.push(
                        workspace.worktree,
                        workspace.branch,
                        protected=workspace.protected,
                        kind="curator",
                        plan_branch=None,
                        env=env,
                        options=options,
                    )

            async with self._push_lock:
                pushed = await self.credentials.with_renewal(name, push)
        except gitops.GitError as exc:  # PushRefused among them
            return {"error": f"git push of {name} to {workspace.branch} failed: {exc}"}
        if pushed.changed:
            self.note(
                f"Pushed {pushed.head[:12]} of {name} to {workspace.branch} on origin for the agent "
                f"({len(pushed.commits)} commit(s)).",
                commit_sha=pushed.head,
                repo=name,
            )
        return {
            "branch": pushed.branch,
            "head": pushed.head,
            "default": pushed.default,
            "changed": pushed.changed,
            "commits": list(pushed.commits),
        }

    def _forge_origin(self, name: str) -> bool:
        """Whether the origin of ``name`` is on a forge (https or SSH), which takes a credential; a path on this
        machine takes none."""
        urls = self.credentials.origins.get(name) or []
        return any(normalize_origin(url).startswith("https://") for url in urls)

    def _push_options(self, name: str) -> list[str]:
        """The push options of a Builder's push to GitLab, which open its merge request; none otherwise."""
        if not self.builder or self.curator.get("forge") != "gitlab":
            return []
        return judge.gitlab_push_options(self.targets.get(name) or "main", self.title)

    async def _notice(self, name: str, pushed: gitops.Pushed) -> None:
        """Tell the run's owner of a push to a default branch; a notice the hub does not take is logged."""
        body = gitops.push_notice(self.id, name, pushed)
        backoff = Backoff()
        for _ in range(HARD_STOP_TRIES + 1):
            try:
                await self.daemon.hub.notice(self.id, body)
                self.note(f"Notified the owner of the push to {pushed.branch} of {name}.")
                return
            except Unreachable:
                await _wait_or(self.daemon.hard_stop, backoff.next())
            except HubProblem as exc:
                log.warning("notice not sent", extra={"run_id": self.id, "error": str(exc)})
                break
        self.note(f"The notice of the push to {pushed.branch} of {name} was not sent.")

    async def _diff(self) -> bytes:
        """The diffs of every repo of the run, each under its folder (a/<repo>/...)."""
        parts = []
        for workspace in self.workspaces.values():
            if workspace.worktree.is_dir():
                with contextlib.suppress(gitops.GitError, OSError, asyncio.TimeoutError):
                    parts.append(await gitops.diff(workspace.worktree, workspace.base, prefix=workspace.worktree.name))
        return b"".join(parts)


class ReviewRun(PlanRun):
    """A review run (kind ``review``, see the module's docstring): a worktree of each of its repos, detached at the
    commit origin's default branch has, the agent reading in their directory and writing findings and proposals
    through ``evo-agents worker finding|propose``, and nothing committed or pushed at its end."""

    def __init__(self, daemon: Daemon, spec: dict):
        super().__init__(daemon, spec)
        self.title = spec.get("title") or f"review of {spec.get('project')}"

    def request_park(self) -> None:
        """A review run asks no decision and never parks: a park means the hub no longer holds it for this worker."""
        self.request_stop("gone")

    def _park_requested(self) -> bool:
        return False

    async def _steps(self) -> None:
        spec = self.spec
        repos = [entry for entry in spec.get("repos") or [] if isinstance(entry, dict)]
        names = ", ".join(str(entry.get("repo")) for entry in repos) or "no repo"
        self.note(
            f"Run #{self.id} claimed by worker {self.daemon.config.name}: the review of project {self.project} over "
            f"{names}, {self.runtime}, {self.mode}, timeout {self.timeout_s // 60} min of agent time. It reads only: "
            "nothing is committed or pushed."
        )
        cls = self.daemon.adapters.get(self.runtime)
        if cls is None:
            raise RunFailed(f"this worker has no adapter for {self.runtime}", cause="runtime")
        if self.mode == "interactive":
            why = self.interactive_unsupported(cls)
            if why is not None:
                raise RunFailed(
                    f"this worker cannot run {self.runtime} interactive ({why}): run it headless", cause="runtime"
                )
        if not repos:
            raise RunFailed("the review run names no repo to read")
        names = [entry.get("repo") for entry in repos]
        await self._take_credentials(names)
        self._check()
        await self._preflight(names)
        self._check()
        await self._prepare_review(repos)
        self._check()
        await self._count_figures()
        await self._plan_turns(cls, self._prompt(), None)
        self.summary = self._read_summary()
        await self._ensure_running()
        await self._report("verifying")
        self._check()
        await self._end("done", summary=self.summary, usage=self.outcome.usage if self.outcome else None)

    async def _prepare_review(self, repos: list[dict]) -> None:
        """The run's directory, with a worktree of each repo detached at origin's default branch (else the default
        branch the hub names, else the checkout's HEAD), and .evo-run/ for the agent's result."""
        self.directory = self.daemon.home.worktree_path(self.project, self.id)
        if self.directory.exists():
            raise RunFailed(f"{self.directory} exists already; remove it and queue the review again")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.worktree = self.directory
        taken: set[str] = set()
        for entry in repos:
            name = entry.get("repo")
            if not isinstance(name, str) or not name:
                raise RunFailed("the review run names a repo without a name")
            checkout = self.daemon.checkout_for(self.project, name)
            if checkout is None:
                raise RunFailed(f"this worker has no checkout of {self.project}/{name}")
            folder = gitops.folder_name(name, taken)
            taken.add(folder)
            self.workspaces[name] = await self._detached_worktree(name, checkout, folder)
            self._save_workspaces()
        (self.directory / runs.RESULT_DIR).mkdir(mode=0o700, exist_ok=True)
        self._save_workspaces()

    async def _detached_worktree(self, name: str, checkout: Path, folder: str) -> gitops.Workspace:
        path = self.directory / folder
        async with self.daemon.repo_lock(checkout):
            if not await gitops.has_remote(checkout):
                raise RunFailed(f"the checkout at {checkout} has no remote named origin to fetch from")
            await self._fetch(checkout)
            remote_head = await gitops.remote_default_branch(checkout)
            hub_default = default_branch_of(self.daemon.config, self.project, name)
            refs = [f"refs/remotes/origin/{branch}" for branch in (remote_head, hub_default) if branch]
            start = base = None
            for ref in (*refs, "refs/remotes/origin/HEAD", "HEAD"):
                base = await gitops.rev(checkout, ref)
                if base is not None:
                    start = ref
                    break
            if base is None:
                raise RunFailed(f"the checkout at {checkout} has no commit to read")
            try:
                await gitops.add_detached_worktree(checkout, path, base)
            except gitops.GitError as exc:
                raise RunFailed(f"cannot make the worktree {path}: {exc}") from None
        self.note(f"Worktree {path} of {name}, detached at {base[:12]} ({start}), read-only.", worktree=str(path))
        return gitops.Workspace(
            repo=name,
            branch=remote_head or hub_default or "HEAD",
            plan_branch=None,
            checkout=checkout,
            worktree=path,
            local_branch="HEAD",
            base=base,
            protected=(),
        )

    async def _count_figures(self) -> None:
        """Count what lives in the worktrees' files (``figures.scan``) into .evo-run/worktree-figures.json, and say
        how much in the run's log."""
        worktrees = {name: workspace.worktree for name, workspace in self.workspaces.items()}
        try:
            found = await asyncio.to_thread(figures.scan, worktrees)
        except Exception:
            log.exception("the worktrees' figures were not counted", extra={"run_id": self.id})
            return
        path = self.directory / runs.RESULT_DIR / WORKTREE_FIGURES
        with contextlib.suppress(OSError):
            path.write_text(json.dumps(found, ensure_ascii=False, indent=1), encoding="utf-8")
        counts = {key: len(value) for key, value in found.items()}
        self.note(
            f"Counted in the worktrees: {counts['reports']} open items of reports, {counts['learned_skills']} learned "
            f"skills waiting for review ({runs.RESULT_DIR}/{WORKTREE_FIGURES}).",
            worktree_figures=counts,
        )

    def _prompt(self) -> str:
        """The run's prompt, with where each repo's worktree is when a folder is not named as its repo, and the file
        of the worktrees' figures."""
        prompt = self.spec.get("prompt") or ""
        moved = [f"- {name}: {ws.worktree.name}/" for name, ws in self.workspaces.items() if ws.worktree.name != name]
        extra = [
            "",
            f"The worker counted the open items of reports and the learned skills waiting for review in the worktrees: "
            f"{runs.RESULT_DIR}/{WORKTREE_FIGURES}.",
        ]
        if moved:
            extra += ["Worktree folders that are not named as their repo:", *moved]
        return runs.clip(prompt + "\n".join(extra) + "\n", runs.MAX_PROMPT_BYTES)

    async def _diff(self) -> bytes:
        """A review run changes nothing: no diff is uploaded."""
        return b""


class JudgeRun(ReviewRun):
    """A judge run (kind ``judge``, see the module's docstring): the change at the commit it judges, its diff read for
    signs of score hacking, the project's hidden checks and the plan's verify commands run in its worktree as code the
    worker does not trust, the Judge's agent on what they found, and the verdict, which ends the agent's last message,
    posted; nothing committed or pushed."""

    def __init__(self, daemon: Daemon, spec: dict):
        super().__init__(daemon, spec)
        change = self.curator or {}
        self.title = spec.get("title") or f"judge of change #{change.get('change_id')}"
        self.head: str | None = None
        self.merge_base: str | None = None
        self._git_dir: str | None = None  # the worktree's git directory, as it was made

    async def _steps(self) -> None:
        spec, change = self.spec, self.curator or {}
        repos = [entry for entry in spec.get("repos") or [] if isinstance(entry, dict)]
        self.note(
            f"Run #{self.id} claimed by worker {self.daemon.config.name}: the Judge of the Curator's change "
            f"#{change.get('change_id')} of project {self.project}, {self.runtime}, timeout {self.timeout_s // 60} min "
            "of agent time. It reads and judges: nothing is committed or pushed, and the Builder's transcript is not "
            "read."
        )
        cls = self.daemon.adapters.get(self.runtime)
        if cls is None:
            raise RunFailed(f"this worker has no adapter for {self.runtime}", cause="runtime")
        if len(repos) != 1 or not repos[0].get("repo"):
            raise RunFailed("a judge run judges a change of one repo")
        name = repos[0]["repo"]
        await self._take_credentials([name])
        self._check()
        inputs = await self._inputs()
        hidden = [item for item in inputs.get("hidden_checks") or [] if isinstance(item, str)]
        verify_commands = [item for item in inputs.get("verify") or [] if isinstance(item, str)]
        protected = [item for item in inputs.get("protected_paths") or [] if isinstance(item, str)]
        try:  # a program missing fails the run, not the change: it is judged again elsewhere (JUDGE_ATTEMPTS)
            await self._preflight([name], verify=verify_commands, hidden=hidden)
        except RunFailed:
            hidden.clear()
            inputs.clear()
            raise
        self._check()
        await self._prepare_judge(name, change)
        self._check()
        workspace = self.workspaces[name]
        # The diff is read before any code of the change runs, with no textconv of the repository.
        diff, cut = await gitops.diff_text(
            workspace.worktree, self.merge_base, self.head, env=gitops.without_hooks(self.daemon.env)
        )
        files = judge.parse_diff(diff)
        signs = judge.hack_signs(files, repo=name, protected=protected, verify_commands=verify_commands)
        if cut:  # first, so the cap on signs never drops it
            cut_sign = {
                "kind": "diff_unreadable",
                "path": "(the diff)",
                "line": None,
                "text": f"the diff is longer than the {gitops.DIFF_TEXT_LIMIT} characters the worker reads",
            }
            signs = [cut_sign, *signs][: judge.MAX_SIGNS]
        if signs:
            kinds = ", ".join(sorted({item["kind"] for item in signs}))
            self.note(
                f"The diff shows signs of score hacking ({kinds}): the change fails, and its Judge does not start."
            )
        results = await self._run_hidden(workspace.worktree, hidden)
        hidden.clear()  # the commands go: only their exit codes stay
        inputs.clear()
        verify = await self._run_checks(workspace.worktree, verify_commands)
        verdict, reasons = None, None
        if not signs:
            await self._restore(workspace.worktree)
            prompt = runs.clip(self._prompt() + judge.results_text(verify, results, signs), runs.MAX_PROMPT_BYTES)
            await self._plan_turns(cls, prompt, None)
            verdict, reasons = self._read_verdict()
        body = {
            "verdict": verdict,
            "reasons": reasons,
            "head_sha": self.head,
            "base_sha": self.merge_base,
            "verify": verify,
            "hidden": results,
            "signs": signs,
            "paths": judge.changed_paths(files)[: judge.MAX_PATHS],
        }
        try:
            answer = await self.daemon.hub.verdict(self.id, body, self._judge_key)
        except HubProblem as exc:
            raise RunFailed(f"the hub did not take the Judge's verdict: {exc}") from None
        passed = answer.get("passed") if isinstance(answer, dict) else None
        self.summary = f"The Judge {'passed' if passed else 'failed'} the change" + (f": {reasons}" if reasons else ".")
        self.note(self.summary, passed=passed)
        await self._ensure_running()
        await self._report("verifying")
        self._check()
        usage = self.outcome.usage if self.outcome else None
        await self._end("done", summary=_cut(self.summary, MAX_SUMMARY_CHARS), usage=usage)

    def run_path(self) -> str | None:
        """The PATH the change's verify commands and hidden checks run with: the one of the environment they get
        (``untrusted.scrubbed_env``)."""
        return untrusted.scrubbed_env(self.daemon.env).get("PATH")

    async def _inputs(self) -> dict:
        """What the hub gives the Judge to read, asked with the run's own key; held in memory alone."""
        try:
            found = await self.daemon.hub.judge_inputs(self.id, self._judge_key)
        except HubProblem as exc:
            raise RunFailed(f"the hub did not give the Judge its inputs: {exc}") from None
        return found if isinstance(found, dict) else {}

    async def _prepare_judge(self, name: str, change: dict) -> None:
        """The run's directory with a worktree of ``name`` detached at the commit to judge, made with no hook of the
        checkout, and the merge base with origin's default branch, which the diff starts at."""
        self.directory = self.daemon.home.worktree_path(self.project, self.id)
        if self.directory.exists():
            raise RunFailed(f"{self.directory} exists already; remove it and queue the judge run again")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.worktree = self.directory
        checkout = self.daemon.checkout_for(self.project, name)
        if checkout is None:
            raise RunFailed(f"this worker has no checkout of {self.project}/{name}")
        folder = gitops.folder_name(name)
        path = self.directory / folder
        async with self.daemon.repo_lock(checkout):
            if not await gitops.has_remote(checkout):
                raise RunFailed(f"the checkout at {checkout} has no remote named origin to fetch from")
            await self._fetch(checkout)
            wanted = change.get("head_sha")
            branch = change.get("branch")
            head = await gitops.rev(checkout, wanted) if judge.is_sha(wanted) else None
            if head is None and wanted:
                raise RunFailed(f"origin of {name} has no commit {wanted[:12]}: the pull request's head is not there")
            if head is None and branch:
                head = await gitops.rev(checkout, f"refs/remotes/origin/{branch}")
            if head is None:
                raise RunFailed(f"origin of {name} has no branch {branch}: there is nothing to judge")
            remote_head = await gitops.remote_default_branch(checkout)
            base_branch = (
                change.get("base_branch")
                or remote_head
                or default_branch_of(self.daemon.config, self.project, name)
                or "main"
            )
            target = await gitops.rev(checkout, f"refs/remotes/origin/{base_branch}")
            base = await gitops.merge_base(checkout, target, head) if target else None
            if base is None:
                raise RunFailed(f"{name}: no merge base of {head[:12]} with origin's {base_branch}")
            try:
                await gitops.add_detached_worktree(checkout, path, head, env=gitops.without_hooks(self.daemon.env))
            except gitops.GitError as exc:
                raise RunFailed(f"cannot make the worktree {path}: {exc}") from None
            self._git_dir = await gitops.git_dir(path)
            if self._git_dir is None:
                raise RunFailed(f"the worktree {path} has no git directory of its own")
        self.head, self.merge_base = head, base
        self.workspaces[name] = gitops.Workspace(
            repo=name,
            branch=branch or "HEAD",
            plan_branch=None,
            checkout=checkout,
            worktree=path,
            local_branch="HEAD",
            base=head,
            protected=(),
        )
        (self.directory / runs.RESULT_DIR).mkdir(mode=0o700, exist_ok=True)
        self._save_workspaces()
        self.note(
            f"Worktree {path} of {name}, detached at {head[:12]}, the commit judged; its diff starts at {base[:12]}, "
            f"the merge base with origin's {base_branch}.",
            worktree=str(path),
        )

    async def _restore(self, cwd: Path) -> None:
        """Put the worktree back at the commit judged: what a command of the change wrote in it before goes."""
        try:
            await gitops.restore(
                cwd, self.head, expected_git_dir=self._git_dir, env=gitops.without_hooks(self.daemon.env)
            )
        except gitops.GitError as exc:
            raise RunFailed(f"the worktree {cwd} could not be put back at {self.head[:12]}: {exc}") from None

    async def _untrusted(self, command: str, cwd: Path) -> tuple[int, str]:
        """(exit code, the end of its output) of ``command``, code of the change the worker does not trust
        (``evo_agents.worker.untrusted``): in the worktree put back at the commit judged, on the standard input of
        ``/bin/sh -s``, in a session of its own, with an environment that leads to no credential, and every process
        it left killed once it ends; Stopped when the run is asked to stop or runs out of time first."""
        await self._restore(cwd)
        marker = untrusted.new_marker()
        env = untrusted.scrubbed_env(self.daemon.env, credentials.held_values())
        proc = await untrusted.start(command, cwd=cwd, env=env, marker=marker)
        tail = bytearray()

        async def read() -> None:
            while chunk := await proc.stdout.read(65536):
                tail.extend(chunk)
                del tail[:-OUTPUT_TAIL]

        reader = asyncio.create_task(read())
        waiter = asyncio.create_task(proc.wait())
        stopper = asyncio.create_task(self._stop.wait())
        try:
            done, _ = await asyncio.wait(
                {waiter, stopper},
                timeout=max(0.0, self.deadline - self.loop.time()),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stopper.cancel()
            killed = await untrusted.kill_leftovers(proc.pid, marker)
            if not waiter.done():
                await waiter
        if killed:
            log.info("processes a judged command left were killed", extra={"run_id": self.id, "killed": killed})
        if waiter not in done:
            reader.cancel()
            if self.stop_reason is None:
                self.stop_reason = "timeout"
            raise Stopped(self.stop_reason)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(reader, 5)
        reader.cancel()
        return proc.returncode, tail.decode(errors="replace")

    async def _run_checks(self, cwd: Path, commands: list[str]) -> list[dict]:
        """Run each verify command of the plan in the worktree, as code the worker does not trust: each one's exit
        code."""
        results = []
        if commands:
            self.note(f"Running the {len(commands)} verify command(s) of the plan.")
        for command in commands:
            self._check()
            started = self.loop.time()
            code, output = await self._untrusted(command, cwd)
            duration_ms = int((self.loop.time() - started) * 1000)
            results.append({"command": command, "exit_code": code, "duration_ms": duration_ms})
            self.event(
                "system",
                {
                    "text": f"verify: `{_cut(command, 200)}` exited {code} after {duration_ms} ms",
                    "command": command,
                    "exit_code": code,
                    "duration_ms": duration_ms,
                    "output": output,
                },
            )
        return results

    async def _run_hidden(self, cwd: Path, checks: list[str]) -> list[dict]:
        """Run each hidden check of the project in the worktree, first, as code the worker does not trust: each one's
        place and exit code alone. Neither the command nor its output reaches an event, a log line, a file or an
        argument of a process."""
        results = []
        for index, command in enumerate(checks, start=1):
            self._check()
            started = self.loop.time()
            code, _ = await self._untrusted(command, cwd)
            duration_ms = int((self.loop.time() - started) * 1000)
            results.append({"index": index, "exit_code": code, "duration_ms": duration_ms})
            self.note(f"hidden check {index} of {len(checks)} exited {code} after {duration_ms} ms")
        return results

    def _read_verdict(self) -> tuple[str | None, str | None]:
        """The verdict that ends the Judge agent's last message, from its own output (``judge.verdict_from_message``);
        a file in the run's directory, which the code under test can write, counts for nothing."""
        message = self.outcome.summary if self.outcome is not None else None
        verdict, reasons = judge.verdict_from_message(message)
        if verdict is None:
            self.note(f"{reasons}: no verdict, which fails the change.")
            return None, None
        return verdict, reasons or None

    def _prompt(self) -> str:
        """The hub's prompt, with the worktree's folder when it is not named as its repo."""
        prompt = self.spec.get("prompt") or ""
        moved = [f"- {name}: {ws.worktree.name}/" for name, ws in self.workspaces.items() if ws.worktree.name != name]
        if moved:
            prompt += "\nWorktree folders that are not named as their repo:\n" + "\n".join(moved) + "\n"
        return prompt


WORKTREE_FIGURES = "worktree-figures.json"  # in .evo-run/ of a review run's directory


class AuthorRun(ReviewRun):
    """An author run (kind ``author``, see the module's docstring): the skills its claim names downloaded and checked,
    a worktree of each of its repos this worker has a checkout of, detached at origin's default branch, the skills
    written for the agent in the run's directory, the agent writing a plan from its owner's request and talking with
    the owner in the run's chat, waiting for the owner's reply after each turn (parked when it waits too long, and
    resumed in the same session and directory), and nothing committed or pushed at its end, once the owner ended the
    chat."""

    def __init__(self, daemon: Daemon, spec: dict):
        super().__init__(daemon, spec)
        self.title = spec.get("title") or f"author run of {spec.get('project')}"
        self.left_out: list[str] = []  # repos of the run this worker has no checkout of
        self.last_said: str | None = None  # the agent's last message in the chat

    # Control: an author run parks as a plan run does, and its owner ends its chat

    def request_park(self) -> None:
        PlanRun.request_park(self)

    def _park_requested(self) -> bool:
        return self.park_asked.is_set()

    def request_finish(self) -> None:
        """The heartbeat says the owner ended the chat: the run ends done once the agent's turn is over."""
        if self.ended or self.finish_asked.is_set():
            return
        log.info("finish asked", extra={"run_id": self.id, "phase": self.phase})
        self.finish_asked.set()
        self.note("The owner ended the chat: the run ends done once the agent's turn is over.")

    async def _steps(self) -> None:
        spec = self.spec
        repos = [entry for entry in spec.get("repos") or [] if isinstance(entry, dict)]
        names = ", ".join(str(entry.get("repo")) for entry in repos) or "no repo"
        resumes = f", going on from parked run #{self.resume_of}" if self.resume_of else ""
        self.note(
            f"Run #{self.id} claimed by worker {self.daemon.config.name}: an author run of project {self.project} over "
            f"{names}, {self.runtime}, timeout {self.timeout_s // 60} min of agent time{resumes}. It reads only: "
            "nothing is committed or pushed."
        )
        if self.runtime not in author.AUTHOR_RUNTIMES:
            raise RunFailed(f"an author run runs on {', '.join(author.AUTHOR_RUNTIMES)} only, not {self.runtime}")
        cls = self.daemon.adapters.get(self.runtime)
        if cls is None:
            raise RunFailed(f"this worker has no adapter for {self.runtime}", cause="runtime")
        if self.mode == "interactive":
            raise RunFailed("an author run runs headless only")
        if not repos:
            raise RunFailed("the author run names no repo, not even the project's harness")
        if self.resume_of is not None:  # the parked run's directory, worktrees and skills, when they are still here
            self._adopt(self.resume_of)
        bundles = await self._fetch_skills() if self.directory is None else []
        held = self._held_repos(repos)
        names = [entry.get("repo") for entry in held]
        await self._take_credentials(names)
        self._check()
        await self._preflight(names)
        self._check()
        if self.directory is None:
            await self._prepare_review(held)
            self._check()
            self._write_skills(bundles)
        else:
            self.worktree = self.directory
            (self.directory / runs.RESULT_DIR).mkdir(mode=0o700, exist_ok=True)
            self._save_workspaces()
        prompt, session_id = await self._first_turn()
        await self._plan_turns(cls, prompt, session_id)
        self.summary = self._read_summary() or (_cut(self.last_said, MAX_SUMMARY_CHARS) if self.last_said else None)
        await self._ensure_running()
        await self._report("verifying")
        self._check()
        await self._end("done", summary=self.summary, usage=self.outcome.usage if self.outcome else None)

    # The chat

    async def _first_turn(self) -> tuple[str, str | None]:
        """The prompt and session of the agent's first turn: the run's prompt in a new session, or, for a run that
        resumes a parked one, the owner's reply in that run's session."""
        if self.resume_of is None:
            return self._prompt(), None
        messages = await self._unread()
        said = self._take(messages) if messages else ""
        if self.session_id is None:  # the parked run never told its session: start again, with the reply
            self.note("The parked run left no session to go on with: the agent starts a new one.")
            return runs.clip(self._prompt() + ("\n\n" + said if said else ""), runs.MAX_PROMPT_BYTES), None
        prompt = author.RESUME_PROMPT.format(id=self.id) + ("\n\n" + said if said else "")
        return runs.clip(prompt, runs.MAX_PROMPT_BYTES), self.session_id

    def _reply_prompt(self) -> str:
        return author.REPLY_PROMPT

    async def _post_chat(self) -> None:
        """Post the agent's last message of the turn to the run's chat, trying a few times; a message the hub does
        not take is noted, and the run goes on."""
        text = author.chat_text(self.outcome.summary if self.outcome else None)
        if text is None:
            self.note("The agent's turn ended without a message for the chat.")
            return
        self.last_said = text
        backoff = Backoff()
        for _ in range(HARD_STOP_TRIES + 1):
            try:
                await self.daemon.hub.chat(self.id, text)
                return
            except Unreachable:
                await _wait_or(self.daemon.hard_stop, backoff.next())
            except HubProblem as exc:
                self.note(f"The hub did not take the agent's message for the chat: {exc}")
                return
        self.note("The hub did not answer: the agent's message did not reach the chat.")

    async def _after_turn(self) -> list[dict]:
        """After each turn of the agent: its last message to the chat, then what starts its next turn: the owner's
        messages it has not had, at once; else, unless the owner ended the chat, the reply the run waits for. None
        when the run ends."""
        await self._post_chat()
        unread = await self._unread()
        if unread:
            self.note(f"{len(unread)} message(s) of the owner came while the agent worked: a new turn takes them.")
            return unread
        if self.finish_asked.is_set():
            self.note("The agent's turn ended, and the owner ended the chat: the run ends done.")
            return []
        await self._ensure_running()
        try:
            await self._report("waiting", only_from=("running",))
        except ReportRefused as exc:
            if exc.status != 409:
                raise
            self.note(f"The agent's turn ended, and the hub takes no wait for a reply ({exc}): the run ends done.")
            return []
        if self.state != "waiting":
            return []
        self.note(
            "The agent's turn ended: the run waits for its owner's reply in the chat, and the time it waits does not "
            "count toward its timeout."
        )
        messages = await self._wait_for_answer()
        if not messages:
            self.note("The owner ended the chat: the run ends done.")
        return messages

    def _held_repos(self, repos: list[dict]) -> list[dict]:
        """The repos of the run this worker has a checkout of; RunFailed without one of the first, the harness."""
        harness = repos[0].get("repo")
        if not isinstance(harness, str) or self.daemon.checkout_for(self.project, harness) is None:
            raise RunFailed(
                f"this worker has no checkout of {self.project}/{harness}, the project's harness, which an author run "
                "reads the project's plans in: clone it where the harness registry places it, then dispatch again"
            )
        held = []
        for entry in repos:
            name = entry.get("repo")
            if isinstance(name, str) and self.daemon.checkout_for(self.project, name) is not None:
                held.append(entry)
            else:
                self.left_out.append(str(name))
        if self.left_out:
            self.note(f"No checkout of {', '.join(self.left_out)} on this worker: the agent works without them.")
        return held

    async def _fetch_skills(self) -> list[tuple[dict, skills.Contents]]:
        """Each skill the claim names, downloaded through its presigned GET and checked (size, SHA-256, contents) as
        ``hub skills sync`` checks it; RunFailed when one is wrong or author.AUTHOR_SKILL is not among them."""
        wanted = author.AUTHOR_SKILL
        items = [item for item in self.spec.get("skills") or [] if isinstance(item, dict)]
        if not any(str(item.get("name") or "").lower() == wanted.lower() for item in items):
            raise RunFailed(
                f"the claim handed this author run no skill {wanted}: the hub holds none in the global scope, or is "
                "older than this worker; publish it with `evo-agents hub skills publish`, then dispatch again"
            )
        found = []
        for item in items:
            name, version = str(item.get("name") or ""), item.get("version")
            problem = skills.name_problem(name)
            if problem:
                raise RunFailed(f"the claim names a skill this worker cannot write: {problem}")
            try:
                size = int(item["size"])
                data = await asyncio.to_thread(skill_sync.get_bundle, str(item["url"]), size)
            except (KeyError, TypeError, ValueError):
                raise RunFailed(f"the claim names skill {name} without its bundle's URL and size") from None
            except HubError as exc:
                raise RunFailed(f"the bundle of skill {name} (version {version}) did not download: {exc}") from None
            if len(data) != size or hashlib.sha256(data).hexdigest() != item.get("sha256"):
                raise RunFailed(
                    f"the bundle of skill {name} (version {version}) is not the one the hub recorded: another size or "
                    "SHA-256; nothing was written"
                )
            try:
                contents = skills.read_bundle(data, name)
            except skills.BundleError as exc:
                raise RunFailed(f"the bundle of skill {name} (version {version}) is not a skill: {exc}") from None
            found.append((item, contents))
        self.note(
            "Skills from the hub for the agent: "
            + ", ".join(f"{item.get('name')} version {item.get('version')}" for item, _ in found)
            + "."
        )
        return found

    def _write_skills(self, bundles: list[tuple[dict, skills.Contents]]) -> None:
        """Write each skill under author.SKILLS_DIR of the run's directory, where the agent's runtime finds it."""
        base = self.directory.joinpath(*author.SKILLS_DIR.split("/"))
        try:
            base.mkdir(mode=0o700, parents=True, exist_ok=True)
            for item, contents in bundles:
                target = base / str(item["name"])
                target.mkdir(mode=0o700)
                skills.write_tree(contents, target)
        except (OSError, skills.BundleError) as exc:
            raise RunFailed(f"cannot write the skills under {base}: {exc}") from None
        written = ", ".join(f"{author.SKILLS_DIR}/{item['name']}" for item, _ in bundles)
        self.note(f"Wrote {written} in {self.directory}.")

    def _prompt(self) -> str:
        """The run's prompt, with where each repo's worktree is when a folder is not named as its repo, and the repos
        this worker has no checkout of."""
        prompt = self.spec.get("prompt") or ""
        moved = [f"- {name}: {ws.worktree.name}/" for name, ws in self.workspaces.items() if ws.worktree.name != name]
        extra = []
        if moved:
            extra += ["", "Worktree folders that are not named as their repo:", *moved]
        if self.left_out:
            extra += ["", f"Repos of the project this worker has no checkout of: {', '.join(self.left_out)}."]
        return runs.clip(prompt + "\n".join(extra) + ("\n" if extra else ""), runs.MAX_PROMPT_BYTES)


def run_class(spec: dict) -> type[Run]:
    """The class of the run ``spec`` claims: PlanRun for kind plan, ReviewRun for kind review, JudgeRun for kind
    judge, AuthorRun for kind author, Run otherwise."""
    kind = spec.get("kind")
    return {"plan": PlanRun, "review": ReviewRun, "judge": JudgeRun, "author": AuthorRun}.get(kind, Run)
