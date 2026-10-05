"""One run on this worker, from the claim to its last report.

1. ``leased``: the daemon fetches origin in the checkout of the run's repo and makes a worktree under
   ``~/.evo/worker/worktrees/<project>-<run>``, on the plan's branch for the repo, from ``origin/<branch>`` when the
   remote has it (else the local branch, else the remote's default branch). When that branch is checked out
   somewhere else, or has commits here that the start does not hold, the worktree is on ``evo-run/<run>`` instead,
   and the push still goes to the plan's branch. A plan without a branch for the repo, or whose branch is the
   default branch (the remote's HEAD, the hub's default_branch, main or master), fails the run before the agent
   starts: the daemon never pushes the default branch.
2. ``running``: the runtime's adapter starts the agent on the run's prompt; its events go to the spool and on to
   the hub. The owner's messages reach the agent through ``send``; a cancel, the run's timeout (counted from the
   agent's start), or the hub no longer holding the run for this worker interrupt it.
3. ``verifying``: the agent wrote ``.evo-run/result.json``; the daemon runs each of its ``verify_commands`` again in
   the worktree, with the run's time left, and records each exit code as a ``system`` event. A command that exits
   other than 0 fails the run, and nothing is pushed: the work stays in the worktree.
4. The daemon commits what the agent left uncommitted as ``run #N: <title>`` (``.evo-run/`` stays out), refuses to
   push a detached HEAD or a branch the agent switched to, pushes the plan's branch to origin (never forced, never
   merged), and reports ``done`` (approval ``auto``) or ``review`` with the commit, the diffstat, the verify results,
   the agent's summary and usage. Every event is sent before that report.
5. After the last report the log (the run's events) and the diff are uploaded as blobs when the hub has a blob store,
   and the worktree leaves the plan's branch, so the owner can check it out elsewhere; it is removed 7 days later.

A hub that does not answer holds nothing up: events wait in the spool and each report is sent again with the
backoff until the hub answers, or says the run is no longer this worker's.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.hub import runs
from evo_agents.worker import gitops
from evo_agents.worker.adapter import AgentEvent, Outcome, RunContext
from evo_agents.worker.checkouts import default_branch_of
from evo_agents.worker.hubapi import Backoff, HubProblem, Outdated, Refused, Unreachable
from evo_agents.worker.spool import Spool, encode_event

if TYPE_CHECKING:
    from evo_agents.worker.daemon import Daemon

log = logging.getLogger("evo_agents.worker")

RUN_BRANCH = "evo-run/{id}"
RESULT_MAX_BYTES = 1024 * 1024
MAX_VERIFY = 50
MAX_COMMAND_CHARS = 2000
MAX_ERROR_CHARS = 2000
MAX_SUMMARY_CHARS = 8000
MAX_USAGE_BYTES = 64 * 1024
OUTPUT_TAIL = 8 * 1024  # bytes of a verify command's output kept in its event
LOG_LIMIT = 64 * 1024 * 1024  # the run-log blob
DIFF_LIMIT = 8 * 1024 * 1024  # the run-diff blob
STOP_GRACE = 30.0  # seconds an interrupted agent has to end its events
HARD_STOP_TRIES = 3  # tries of a report once the daemon is stopping now
FLUSH_DEBOUNCE = 0.2  # seconds of events gathered into one batch


class RunGone(Exception):
    """The hub no longer takes reports of this run from this worker: lost, cancelled, taken by another attempt."""


class RunFailed(Exception):
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
    return datetime.now(timezone.utc)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _json_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


async def _wait_or(event: asyncio.Event, seconds: float) -> bool:
    """Wait ``seconds``, or less when ``event`` is set; whether it is."""
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(event.wait(), max(0.0, seconds))
    return event.is_set()


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
        self.session_reported: str | None = None
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
        self._report_lock = asyncio.Lock()
        self._delivering = False
        self._unsupported_noted: set[str] = set()
        self._background: set[asyncio.Task] = set()
        home = daemon.home
        home.run_dir(self.id).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.log_path = home.run_dir(self.id) / "events.jsonl"
        self.log_bytes = 0
        self.spool = Spool(home.spool_dir, self.id, daemon.budget)
        self.sender = Sender(daemon, self.spool)
        self.record = {
            "id": self.id,
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
        home.save_run(self.record)

    # Events

    def event(self, kind: str, body: dict, at: datetime | None = None) -> None:
        at = at or _now()
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

    def unsupported(self, what: str) -> None:
        """The owner asked for something this daemon cannot do yet: say so once in the run's log."""
        if what in self._unsupported_noted:
            return
        self._unsupported_noted.add(what)
        self.note(f"The owner asked for a {what}; this worker cannot hand an agent to a terminal yet, so it goes on.")

    async def deliver_inbox(self) -> None:
        """Hand the owner's waiting messages to the agent, then mark them delivered on the hub."""
        if self._delivering or not self.agent_running or self.adapter is None:
            return
        self._delivering = True
        try:
            messages = await self.daemon.hub.inbox(self.id)
            while messages and self.agent_running:
                last = None
                for message in messages:
                    await self.adapter.send(str(message.get("text") or ""))
                    last = message.get("id")
                if last is None:
                    break
                messages = await self.daemon.hub.inbox(self.id, ack=int(last))
        except HubProblem as exc:
            log.warning("messages not taken from the inbox", extra={"run_id": self.id, "error": str(exc)})
        except Exception:
            log.exception("messages not handed to the agent", extra={"run_id": self.id})
        finally:
            self._delivering = False

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
                    raise RunGone(f"the hub refused the report of {state}: {exc}") from None
                self.state = state
                self.record["state"] = state
                with contextlib.suppress(OSError):
                    self.daemon.home.save_run(self.record)
                return answer if isinstance(answer, dict) else {}

    async def _report_running(self, only_from: tuple[str, ...]) -> None:
        session_id = self.adapter.session_id if self.adapter is not None else None
        try:
            await self._report("running", only_from=only_from, session_id=session_id)
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
                await self._end("failed", error=f"the worker on {self.daemon.config.name} failed while running")
        finally:
            for task in list(self._background):
                task.cancel()
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
            self.record["finished_at"] = _now().isoformat()
            self.record["state"] = self.state
            with contextlib.suppress(OSError):
                self.daemon.home.save_run(self.record)
            await self._release_branch()

    async def _go(self) -> None:
        try:
            await self._steps()
        except RunFailed as exc:
            self.note(f"Run failed: {exc.error}")
            await self._end("failed", error=exc.error, verify=exc.fields.get("verify"), usage=exc.fields.get("usage"))
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
            await self._end("failed", error=error, verify=self.verify or None)
        else:
            error = f"the worker {self.daemon.config.name} was stopped while the run was {self.state}"
            self.note(f"Run failed: {error}.")
            await self._end("failed", error=error)

    async def _steps(self) -> None:
        spec = self.spec
        self.note(
            f"Run #{self.id} claimed by worker {self.daemon.config.name}: step {spec.get('step_key')} of plan "
            f"{spec.get('plan_id')}, {self.runtime}, {self.mode}, approval {self.approval}, timeout "
            f"{self.timeout_s // 60} min."
        )
        cls = self.daemon.adapters.get(self.runtime)
        if cls is None:
            raise RunFailed(f"this worker has no adapter for {self.runtime}")
        if self.mode == "interactive" and not cls.interactive:
            raise RunFailed(f"this worker cannot hand a {self.runtime} agent to a terminal yet: run it headless")
        checkout = self.daemon.checkout_for(self.project, self.repo)
        if checkout is None:
            raise RunFailed(f"this worker has no checkout of {self.project}/{self.repo}")
        if not self.branch:
            raise RunFailed(
                f"the plan names no branch for {self.repo}, and the worker never works on the default branch"
            )
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
            self.note(f"Fetching origin in {checkout}.")
            try:
                await gitops.fetch(checkout)
            except gitops.GitError as exc:
                raise RunFailed(f"git fetch in {checkout} failed: {exc}") from None
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

    async def _release_branch(self) -> None:
        """Leave the plan's branch once the run is over, so it can be checked out elsewhere."""
        if self.worktree is None or self.local_branch != self.branch or not self.worktree.exists():
            return
        with contextlib.suppress(gitops.GitError, OSError):
            await gitops.detach(self.worktree)

    # The agent

    def agent_env(self) -> dict[str, str]:
        env = dict(self.daemon.env)
        env.update(
            {
                "EVO_RUN_ID": str(self.id),
                "EVO_RUN_PROJECT": str(self.project),
                "EVO_RUN_PLAN": str(self.spec.get("plan_id") or ""),
                "EVO_RUN_STEP": str(self.spec.get("step_key") or ""),
            }
        )
        return env

    async def _run_agent(self, cls) -> None:
        context = RunContext(
            run=dict(self.spec), worktree=self.worktree, prompt=self.spec["prompt"], env=self.agent_env()
        )
        adapter = cls(context)
        self.adapter = adapter
        try:
            await adapter.start()
        except Exception as exc:
            log.warning("agent did not start", extra={"run_id": self.id}, exc_info=True)
            raise RunFailed(f"{self.runtime} did not start: {type(exc).__name__}: {exc}") from None
        self.agent_running = True
        self.deadline = self.loop.time() + self.timeout_s
        self.note(f"{self.runtime} started in {self.worktree}.")
        self._spawn(self._report_running(("leased",)))
        try:
            self.outcome = await self._consume(adapter)
        finally:
            self.agent_running = False
        self._check()
        if not self.outcome.completed:
            raise RunFailed(
                self.outcome.error or f"{self.runtime} ended before its turn completed", usage=self.outcome.usage
            )

    async def _consume(self, adapter) -> Outcome:
        async def pump() -> None:
            async for item in adapter.events():
                if isinstance(item, AgentEvent):
                    self.event(item.kind, item.body, item.at)
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
        except asyncio.TimeoutError:
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
        }.get(reason, reason)

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

    async def _shell(self, command: str, seconds: float) -> tuple[int, str]:
        """(exit code, the end of its output) of ``command`` in the worktree; Stopped when the run is asked to stop
        or runs out of time first, with the command's process group killed."""
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(self.worktree),
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

    async def _commit_and_push(self) -> tuple[str, dict]:
        wt = self.worktree
        try:
            if await gitops.commit_all(wt, f"run #{self.id}: {self.title}"):
                self.note(f"Committed what the agent left uncommitted as run #{self.id}: {self.title}.")
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
        if self.branch in self.protected:  # checked before the agent started; checked again before every push
            raise RunFailed(f"{self.branch} is a default branch: the worker never pushes it", verify=self.verify)
        commit_sha = await gitops.rev(wt, "HEAD")
        diffstat = await gitops.diffstat(wt, self.base)
        self._check()
        try:
            await gitops.push(wt, self.branch)
        except gitops.GitError as exc:
            raise RunFailed(f"git push to {self.branch} on origin failed: {exc}", verify=self.verify) from None
        self.note(
            f"Pushed {commit_sha[:12]} to {self.branch} on origin ({diffstat['files']} file(s), "
            f"+{diffstat['insertions']} -{diffstat['deletions']}).",
            commit_sha=commit_sha,
        )
        self.record.update({"commit_sha": commit_sha, "pushed": True})
        return commit_sha, diffstat

    # Log and diff

    async def _upload(self) -> None:
        """Upload the run's log and diff to the hub's blob store; a hub without one, or a failure, is logged only."""
        payloads: dict[str, bytes] = {}
        items = []
        with contextlib.suppress(OSError):
            if 0 < self.log_path.stat().st_size <= LOG_LIMIT:
                payloads["run-log"] = self.log_path.read_bytes()
        if self.worktree is not None and self.base is not None:
            with contextlib.suppress(gitops.GitError, OSError, asyncio.TimeoutError):
                diff = await gitops.diff(self.worktree, self.base)
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
