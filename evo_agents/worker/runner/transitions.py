"""The decisions of a run that need no I/O: how it ends, what it reports, which branch it works on, when the watchdog
stops it, what the inbox still holds for its agent, and what its result file says. Each one is a pure function of
its arguments, so a test calls it without a daemon, a hub or a mock; the parts of the run do the I/O around them."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field

from evo_agents.hub import curator, runs, tiers
from evo_agents.worker.runner.common import RunFailed, cut, json_size

MAX_ERROR_CHARS = 2000
MAX_SUMMARY_CHARS = 8000
MAX_USAGE_BYTES = 64 * 1024
MAX_VERIFY = 50
MAX_COMMAND_CHARS = 2000
CAP_CAUSES = {"cost": "cost_cap", "turns": "turn_cap", "time": "time_cap"}  # the failure_cause of a cap that stopped it
STOP_WHY = {
    "cancel": "the owner cancelled the run",
    "timeout": "the run reached its timeout",
    "gone": "the hub no longer holds the run for this worker",
    "shutdown": "the worker is stopping",
    "watchdog": "the watchdog of the Curator's runs stopped it",
}


@dataclass(frozen=True)
class Ending:
    """How a run ends: the ``system`` note in its log, with ``extra`` fields, then the report of ``state`` with
    ``fields``."""

    note: str
    state: str
    fields: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


def failure_ending(failed: RunFailed) -> Ending:
    """The end of a run that failed with ``failed``: its cause, else the cause of the cap that stopped its agent."""
    cap = failed.fields.get("cap")
    fields = {
        "error": failed.error,
        "verify": failed.fields.get("verify"),
        "usage": failed.fields.get("usage"),
        "failure_cause": failed.fields.get("cause") or CAP_CAUSES.get(cap),
    }
    return Ending(f"Run failed: {failed.error}", "failed", fields, {"cap": cap} if cap else {})


def stop_ending(
    reason: str, *, timeout_s: int, state: str, worker: str, watchdog: str | None, verify: list[dict]
) -> Ending | None:
    """The end of a run asked to stop for ``reason`` while it was ``state``; None when the hub let go of it (gone),
    which takes no report."""
    if reason == "gone":
        return None
    if reason == "cancel":
        return Ending("Cancelled by the owner.", "cancelled")
    if reason == "timeout":
        error = f"it ran past its timeout of {timeout_s // 60} minutes"
        fields = {"error": error, "verify": verify or None, "failure_cause": "timeout"}
        return Ending(f"Run failed: {error}.", "failed", fields)
    if reason == "watchdog":
        error = f"the watchdog of the Curator's runs stopped it: {watchdog}"
        return Ending(f"Run failed: {error}.", "failed", {"error": error}, {"watchdog": watchdog})
    error = f"the worker {worker} was stopped while the run was {state}"
    return Ending(f"Run failed: {error}.", "failed", {"error": error, "failure_cause": "worker_stopped"})


def why_stopped(reason: str) -> str:
    """Why the agent is being stopped, for the run's log."""
    return STOP_WHY.get(reason, reason)


def end_fields(fields: dict, session_id: str | None) -> dict:
    """The fields of the report that ends a run: the error cut to its limit, a usage too large for the hub left out,
    and the agent's session unless the fields name one."""
    fields = dict(fields)
    if fields.get("error"):
        fields["error"] = cut(str(fields["error"]), MAX_ERROR_CHARS)
    if fields.get("usage") is not None and json_size(fields["usage"]) > MAX_USAGE_BYTES:
        fields["usage"] = None
    if fields.get("session_id") is None:
        fields["session_id"] = session_id
    return fields


def report_body(state: str, fields: Mapping) -> dict:
    """The body of a report: the move, and each field that has a value."""
    body = {"state": state}
    body.update({key: value for key, value in fields.items() if value is not None})
    return body


def bare_report(body: Mapping) -> dict:
    """A report the hub refused for its details, as the move alone (and its error), which the hub takes."""
    return {key: body[key] for key in ("state", "error") if key in body}


def final_state(approval: str) -> str:
    """The state a run of one step ends in once it pushed: done when the plan approves it automatically, else
    review."""
    return "done" if approval == "auto" else "review"


def verify_failure(results: Iterable[dict], worktree: object) -> tuple[str, str] | None:
    """The error and failure_cause of the first verify command that did not exit 0, None when all did; a command
    not found (127) is a missing tool."""
    bad = next((item for item in results if item["exit_code"] != 0), None)
    if bad is None:
        return None
    error = (
        f"verify command `{cut(bad['command'], 200)}` exited {bad['exit_code']}; nothing was pushed, the work stays "
        f"in {worktree}"
    )
    return error, "missing_tool" if bad["exit_code"] == 127 else "verify_failed"


def running_from(state: str) -> tuple[str, ...]:
    """The states the report of running may leave once an agent starts: interactive or waiting when the run is in
    one (a handback, the owner's answer), else leased."""
    return (state,) if state in ("interactive", "waiting") else ("leased",)


# The inbox


def unread(messages: Iterable, delivered_upto: int) -> list[dict]:
    """The messages of the inbox the agent has not had yet."""
    return [
        message
        for message in messages
        if isinstance(message, dict) and isinstance(message.get("id"), int) and message["id"] > delivered_upto
    ]


def handed(messages: Iterable[dict], delivered_upto: int) -> tuple[int, set[int]]:
    """Once these messages went to the agent: the id of the inbox's messages it has had up to, and the decisions
    they answer."""
    answered = set()
    for message in messages:
        message_id, decision_id = message.get("id"), message.get("decision_id")
        if isinstance(message_id, int):
            delivered_upto = max(delivered_upto, message_id)
        if isinstance(decision_id, int):
            answered.add(decision_id)
    return delivered_upto, answered


def waits_for_owner(asked: Collection[int], open_decisions: int) -> bool:
    """Whether a plan run whose agent's turn ended waits for its owner: a decision its agent asked is unanswered, or
    the hub counts one of the run open."""
    return bool(asked) or open_decisions > 0


def answer_note(messages: Iterable[dict], session_id: str | None) -> str:
    """The note of the owner's messages that end a wait: the decisions they answer, and the session that goes on."""
    answers = sorted(m["decision_id"] for m in messages if isinstance(m.get("decision_id"), int))
    which = f" answering decision {', '.join(f'#{item}' for item in answers)}" if answers else ""
    return f"The owner wrote{which}: the agent goes on in session {session_id or '(new)'}."


# Branches


def start_refs(branch: str) -> tuple[str, ...]:
    """The refs a worktree on ``branch`` starts from, the first that exists: origin's branch, the local one, then
    origin's default branch and the checkout's HEAD."""
    return (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}", "refs/remotes/origin/HEAD", "HEAD")


def working_branch(branch: str, fits: bool, in_use: Collection[str], side: str) -> tuple[str, str | None]:
    """The local branch a worktree works on, and why it is not ``branch`` (None when it is). ``fits``: ``branch``
    is checked out in no worktree and has no commit here that the start lacks; otherwise the worktree is on the
    side branch ``side``, and the push still goes to ``branch``."""
    if fits:
        return branch, None
    return side, "is checked out in another worktree" if branch in in_use else "has commits here that it lacks"


# The watchdog of a run of the Curator


def protected_hit(repo: str, paths: Iterable[str], protected: Iterable[str]) -> str | None:
    """Why a worktree of ``repo`` whose changed files are ``paths`` stops the run: the first that a glob of the
    charter's protected paths matches; None when none does."""
    globs = list(protected)
    hit = next(((path, glob) for path in paths for glob in globs if tiers.matches(glob, repo, path)), None)
    return None if hit is None else f"{repo}:{hit[0]} is protected by the charter ({hit[1]})"


def cap_passed(budget: Mapping, agent_seconds: float, cost: float) -> str | None:
    """Why the run's budget stops it: the agent time it used (``agent_seconds``, with what a parked run it resumes
    used) passed its time cap, or its ``cost`` passed its cost cap; None when neither did."""
    cap_seconds, cap_usd = budget.get("max_seconds"), budget.get("max_usd")
    if cap_seconds is not None and agent_seconds > cap_seconds:
        return f"the run used more than its time cap of {int(cap_seconds) // 60} minutes of agent time"
    if cap_usd is not None and cost > cap_usd:
        return f"the run cost more than its cost cap of {curator.money(cap_usd)}"
    return None


# The agent's result


def result_commands(data: object) -> list[str]:
    """The verify commands of a run of one step's result file (``runs.RESULT_FILE``, read as JSON); RunFailed when
    it lists none, too many, or one that is not a shell command of the allowed length."""
    name = runs.RESULT_FILE
    commands = data.get("verify_commands") if isinstance(data, dict) else None
    if not isinstance(commands, list) or not commands:
        raise RunFailed(f"{name} lists no verify_commands, so the worker cannot check the step")
    if len(commands) > MAX_VERIFY:
        raise RunFailed(f"{name} lists {len(commands)} verify commands; at most {MAX_VERIFY} are run")
    for command in commands:
        if not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND_CHARS:
            raise RunFailed(f"each verify command in {name} is a shell command of 1 to {MAX_COMMAND_CHARS} characters")
    return commands


def summary_of(data: object) -> str | None:
    """The agent's summary in its result file, cut to its limit; None when there is none."""
    summary = data.get("summary") if isinstance(data, dict) else None
    return cut(summary.strip(), MAX_SUMMARY_CHARS) if isinstance(summary, str) and summary.strip() else None


def run_record(spec: Mapping, claimed_at: str, curator_role: Mapping | None) -> dict:
    """The record of a run just claimed (``runs/<run>/run.json``); a run of the Curator also notes what
    ``evo-agents worker step`` pushes with: a branch of the Curator alone."""
    record = {
        "id": int(spec["id"]),
        "kind": spec.get("kind") or "step",
        "project": spec["project"],
        "plan_id": spec.get("plan_id"),
        "step_key": spec.get("step_key"),
        "repo": spec["repo"],
        "branch": spec.get("branch"),
        "runtime": spec["runtime"],
        "claimed_at": claimed_at,
        "finished_at": None,
        "state": "leased",
    }
    if curator_role is not None:
        record["curator"] = {key: curator_role.get(key) for key in ("role", "branch", "forge", "change_id")}
    return record
