"""A plan run with a session for each step (``spec["session_per_step"]``, ``runs.SESSION_PER_STEP``): the daemon gives
each step not done yet a new agent session, in the order depends_on allows, rather than one session for every step.

1. Before each session the daemon reads the plan as the hub holds it now and takes the next step
   (``runs.next_session_step``): the first pending or in_progress step whose depends_on are done and that had no
   session in this run yet. Its prompt is the step's (``runs.build_step_session_prompt``): the rules of a step's
   session, the run's repos, the plan's context, the step's what, verify and note, and the handoff of the plan for
   the step (``handoff.build_handoff``) in the room the rest leaves within ``runs.MAX_PROMPT_BYTES``, at most
   ``handoff.DEFAULT_BUDGET_TOKENS``.
2. The agent reports the step with ``evo-agents worker step`` as in a plan run in one session. A decision it asks is
   waited for and answered in the session of the step, the hub may park the run there, and a takeover drives that
   session (``conversation``, ``agent``).
3. After the session the daemon takes the step's summary from ``.evo-run/result.json`` (and removes the file, so the
   next session starts without it) and reads the plan again. A step left not done ends the sessions there, as a plan
   run's agent that cannot go on ends it; then the run pushes and ends as a plan run in one session does, its summary
   the summaries of its sessions.

The run's record (``runs/<run>/run.json``) lists its sessions under ``step_sessions``: the step, the session's id,
the run that ran it, when it started and ended, the step's status after it, and its usage. A run that resumes a parked
one takes over that list (``Conversation.adopt``) and goes on first with the step whose session was parked, in that
session, with its owner's messages. The usage the run reports adds the sessions' up (``sessions_usage``): their
numbers summed, the largest peak_context_tokens, and ``sessions``, each session with its step, id, run and usage, so
the hub's record of the run names the session of every step.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING

from evo_agents.hub import handoff, runs
from evo_agents.hub.plans import PlanProblem, step_index
from evo_agents.worker.hubapi import Backoff, HubProblem, Unreachable
from evo_agents.worker.runner.common import RunFailed, cut, now, wait_or
from evo_agents.worker.runner.transitions import MAX_SUMMARY_CHARS, summary_of
from evo_agents.worker.runner.verify import RESULT_MAX_BYTES

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run
    from evo_agents.worker.runner.plan import PlanRun

PEAK = "peak_context_tokens"
LARGEST = (PEAK, "context_window")  # of a run's sessions, and of a session's turns: the largest, not the sum
SESSION_TOTALS = ("total_cost_usd",)  # Claude Code's running total of a session: the largest of its turns
MAX_LISTED = 100  # sessions the usage lists, the latest; the record lists every one
PLAN_TRIES = 5  # reads of the plan before the run fails, while the hub does not answer
# What the agent of a step's session is told when its owner wrote while its turn was over: an answer to a decision.
STEP_ANSWER_PROMPT = (
    "The owner of this plan run wrote while your turn was over; a message that answers a decision names it. Go on "
    "with step {key} in this session from where you stopped: read the plan as the hub holds it now with "
    "`evo-agents worker plan` first."
)
# And when a run parked while it waited for its owner goes on, as a new run, in the session of the step it was on.
STEP_RESUME_PROMPT = (
    "This plan run was parked while it waited for its owner, and goes on now as run #{id}, in the session of step "
    "{key} and the same worktrees; EVO_RUN_ID names the new run, and the commands of `evo-agents worker` use it. Go on "
    "with step {key} from where you stopped: read the plan as the hub holds it now with `evo-agents worker plan` "
    "first. The steps after it get sessions of their own."
)


# Usage


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def add_usage(total: dict | None, usage, *, largest: tuple[str, ...]) -> dict | None:
    """``total`` with the numbers of ``usage`` added, key by key, but for the keys of ``largest``, whose larger value
    is kept. A key reported as null (peak_context_tokens a runtime cannot report) stays null until a number comes; other
    values are left out. None while neither has any number or null."""
    if not isinstance(usage, dict):
        return total
    merged = dict(total or {})
    for key, value in usage.items():
        old = merged.get(key)
        if value is None:
            merged.setdefault(key, None)
        elif not _number(value):
            continue
        elif not _number(old):
            merged[key] = value
        else:
            merged[key] = max(old, value) if key in largest else old + value
    return merged or total


def session_usage(usages: list) -> dict | None:
    """The usage of one session from the usages its turns reported, each turn an agent of its own: their numbers
    summed, but the largest peak, context window and running total of the session's cost."""
    total = None
    for usage in usages:
        total = add_usage(total, usage, largest=(*LARGEST, *SESSION_TOTALS))
    return total


def sessions_usage(sessions: list[dict]) -> dict | None:
    """The usage a plan run with a session per step reports, from the sessions of its record: their numbers summed
    (tokens, and the cost of each session), the largest peak_context_tokens (null when no runtime reported one), and
    ``sessions``, the latest MAX_LISTED of them, each its step, session id, run and usage, with
    ``sessions_left_out`` counting the others. None for a run that had no session."""
    if not sessions:
        return None
    total: dict = {}
    listed = []
    for entry in sessions:
        usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else {}
        total = add_usage(total, usage, largest=LARGEST) or total
        numbers = {key: value for key, value in usage.items() if value is None or _number(value)}
        listed.append({"step": entry.get("step"), "session_id": entry.get("session_id"), "run_id": entry.get("run_id")})
        listed[-1].update(numbers)
    result = {**total, PEAK: total.get(PEAK), "sessions": listed[-MAX_LISTED:]}
    if len(listed) > MAX_LISTED:
        result["sessions_left_out"] = len(listed) - MAX_LISTED
    return result


def status_in(body: dict, key: str) -> str | None:
    """The status of step ``key`` in the plan ``body``: pending when it has none; None when the plan has no such
    step any more."""
    try:
        step = body["steps"][step_index(body, key)]
    except (PlanProblem, KeyError, TypeError, IndexError):
        return None
    status = step.get("status") if isinstance(step, dict) else None
    return status if isinstance(status, str) and status else "pending"


def find_step(body: dict, key: str | None) -> dict | None:
    """Step ``key`` of the plan ``body`` as a mapping, the same object the body holds; None when it has none."""
    if key is None:
        return None
    try:
        step = body["steps"][step_index(body, key)]
    except (PlanProblem, KeyError, TypeError, IndexError):
        return None
    return step if isinstance(step, dict) else None


# The sessions


class StepSessions:
    """The sessions of a plan run with a session for each step: the steps that had one in this run (``tried``), the
    summaries their agents wrote, and the list of them in the run's record (see the module's docstring)."""

    def __init__(self, plan_run: PlanRun):
        self.plan_run = plan_run
        self.run: Run = plan_run.run
        self.conversation = plan_run.conversation
        self.tried: set[str] = set()
        self.summaries: list[str] = []

    @property
    def entries(self) -> list[dict]:
        """The sessions of the run, as its record lists them: a parked run's it resumes first."""
        found = self.run.record.get("step_sessions")
        if not isinstance(found, list):
            found = self.run.record["step_sessions"] = []
        return found

    def usage(self) -> dict | None:
        return sessions_usage(self.entries)

    async def work(self, cls) -> None:
        """Every step's session, one after the other, until no step is left to start or a session leaves its step not
        done; the run's summary from theirs."""
        run = self.run
        try:
            body, revision = await self.read_plan()
            go_on = True
            if self.conversation.resume_of is not None:
                body, revision, go_on = await self.resume(cls, body, revision)
            while go_on:
                run.check()
                found = runs.next_session_step(body, self.tried)
                if found is None:
                    run.note("No step of the plan is left to start: the run's sessions are over.")
                    break
                key, step = found
                await self.session(cls, key, self.prompt(body, revision, step, key), None)
                body, revision = await self.read_plan()
                go_on = self.went_well(body, key)
        except RunFailed as exc:  # the run's usage is its sessions', not the last turn's
            exc.fields["usage"] = self.usage()
            raise
        run.summary = cut("\n\n".join(self.summaries), MAX_SUMMARY_CHARS) if self.summaries else None

    async def resume(self, cls, body: dict, revision: int | None) -> tuple[dict, int | None, bool]:
        """Go on with the parked run this run resumes: the step whose session was parked (the next step when the
        record names none) goes on in that session with its owner's messages, before the next steps get sessions of
        their own. The plan as the hub holds it after that session, and whether the next steps get theirs."""
        run, conversation = self.run, self.conversation
        parked = [entry for entry in self.entries if entry.get("run_id") != run.id]
        key = parked[-1].get("step") if parked else None
        if not isinstance(key, str):
            found = runs.next_session_step(body, ())
            key = found[0] if found else None
        step = find_step(body, key)
        prompt = self.prompt(body, revision, step, key) if step is not None else self.plan_run.prompt()
        conversation.resume_prompt = STEP_RESUME_PROMPT.replace("{key}", key or "?")
        prompt, session_id = await conversation.first_turn(prompt)
        await self.session(cls, key or "?", prompt, session_id)
        body, revision = await self.read_plan()
        return body, revision, key is None or self.went_well(body, key)

    def went_well(self, body: dict, key: str) -> bool:
        """Whether the run goes on after the session of step ``key``: the step is done, or the plan has it no more.
        The status is noted in the record's entry of the session."""
        run = self.run
        status = status_in(body, key)
        for entry in reversed(self.entries):
            if entry.get("step") == key and entry.get("run_id") == run.id:
                entry["status"] = status
                break
        self.save()
        if status in ("done", None):
            return True
        run.note(
            f"The session of step {key} ended with the step {status}: the run's sessions end here, as a plan run ends "
            "when its agent cannot go on; the summary says what is left."
        )
        return False

    def prompt(self, body: dict, revision: int | None, step: dict, key: str) -> str:
        """The prompt of the session of ``step`` (``key``): the step's, with the handoff of the plan in the room the
        rest of the prompt leaves, at most handoff.DEFAULT_BUDGET_TOKENS and at least handoff.MIN_BUDGET_TOKENS
        (the prompt's own limit cuts its end then)."""
        run = self.run
        repos = run.spec.get("repos") or []
        folders = {name: workspace.worktree.name for name, workspace in self.plan_run.directory.workspaces.items()}
        bare = runs.build_step_session_prompt(body, step, repos, folders)
        room = runs.handoff_room(bare) // handoff.BYTES_PER_TOKEN
        budget = max(handoff.MIN_BUDGET_TOKENS, min(handoff.DEFAULT_BUDGET_TOKENS, room))
        if revision is None:
            plan = run.spec.get("plan") if isinstance(run.spec.get("plan"), dict) else {}
            revision = plan.get("revision") if isinstance(plan.get("revision"), int) else 0
        built = handoff.build_handoff(body, project=run.project, revision=revision, step=key, budget_tokens=budget)
        cut_note = ", cut to fit" if built.cut else ""
        run.note(
            f"The prompt of step {key} carries the handoff of the plan at revision {revision}: about {built.tokens} of "
            f"{budget} tokens{cut_note}."
        )
        return runs.build_step_session_prompt(body, step, repos, folders, built.text)

    async def session(self, cls, key: str, prompt: str, session_id: str | None) -> None:
        """The agent's session of step ``key``, a new one or ``session_id``, turn after turn until it has nothing left
        to wait for; noted in the record, with its usage, whether it ends or fails."""
        run, conversation = self.run, self.conversation
        self.tried.add(key)
        entry = {"step": key, "session_id": session_id, "run_id": run.id, "started_at": now().isoformat()}
        entry.update({"ended_at": None, "status": None, "usage": None})
        self.entries.append(entry)
        self.save()
        conversation.session_id = session_id
        conversation.reply_prompt = STEP_ANSWER_PROMPT.replace("{key}", key)
        first = len(conversation.usages)
        how = f"goes on in session {session_id}" if session_id else "starts in a new session"
        run.note(f"Step {key} {how} of {run.runtime}.", step=key)
        try:
            await conversation.turns(cls, prompt, session_id)
        finally:  # parked, failed or stopped too: the record names the session and what it used
            self.close(entry, conversation.usages[first:])
        summary = self.take_summary()
        if summary:
            self.summaries.append(f"Step {key}: {summary}")

    def close(self, entry: dict, usages: list) -> None:
        entry.update(
            {"session_id": self.conversation.session_id, "ended_at": now().isoformat(), "usage": session_usage(usages)}
        )
        self.save()

    def save(self) -> None:
        with contextlib.suppress(OSError):
            self.run.daemon.home.save_run(self.run.record)

    def take_summary(self) -> str | None:
        """The summary the session's agent wrote in .evo-run/result.json, and the file removed for the next session;
        None when it wrote none that reads."""
        path = self.plan_run.directory.path / runs.RESULT_FILE
        if path.is_symlink() or not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.stat().st_size <= RESULT_MAX_BYTES else None
        except (OSError, ValueError):
            data = None
        with contextlib.suppress(OSError):
            path.unlink()
        return summary_of(data)

    async def read_plan(self) -> tuple[dict, int | None]:
        """The plan as the hub holds it now and its revision, read again with the backoff while the hub does not
        answer; RunFailed when it refuses, or after PLAN_TRIES reads without an answer."""
        run = self.run
        backoff = Backoff()
        problem: Exception | None = None
        for _ in range(PLAN_TRIES):
            try:
                view = await run.daemon.hub.plan(run.id)
            except Unreachable as exc:
                problem = exc
                await wait_or(run.daemon.hard_stop, backoff.next())
                run.check()
                continue
            except HubProblem as exc:
                raise RunFailed(f"the plan was not read from the hub before the next step's session: {exc}") from None
            body = view.get("body") if isinstance(view, dict) else None
            if not isinstance(body, dict):
                raise RunFailed("the hub answered the plan of the run without its body")
            revision = view.get("revision")
            return body, revision if isinstance(revision, int) else None
        raise RunFailed(
            f"the hub did not answer {PLAN_TRIES} reads of the plan before the next step's session: {problem}"
        )
