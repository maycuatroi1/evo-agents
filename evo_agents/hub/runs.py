"""Runs and workers as the hub models them: the states of a run and who may move a run between them, a worker's
status as its heartbeats show it, the kinds of a run's events, which steps of a plan are ready to run, and the prompt
a run hands its agent. Pure functions over JSON values and datetimes, standard library only, so the api and the
worker daemon share them. ``docs/workers.md`` describes the protocol built on them.

A run is one attempt at one plan step on one worker. It starts ``queued``; a worker claims it (``leased``), starts
the agent (``running``, or ``interactive`` when a person drives the agent in a terminal), re-runs the agent's
verify commands (``verifying``), and ends ``done``, ``failed`` or ``cancelled``, or in ``review`` until the owner
approves it. A run whose worker stops extending its lease is ``lost``, and the hub queues a new run for the same
step (attempt + 1), or ``failed`` when it was the last of MAX_ATTEMPTS. TRANSITIONS names, for each move, who may do it:
the ``worker`` that holds the run, the ``owner`` (the member who dispatched it, who is the owner of the worker), or
the ``reaper``, the hub itself acting on an expired lease, a revoked worker or a run nobody can claim any more.

A step is ready when its status is ``pending`` and every id in its ``depends_on`` names a step whose status is
``done``, the rule execute-plan uses for its frontier. A step without a status counts as pending, as the knowledge
graph reads plans; ``blocked`` is not pending, so a blocked step is never ready, and neither is a step that depends
on one. ``blocking`` holds no step back, here or in execute-plan from its 0.4.0: it marks a step the plan cannot
finish without, not a gate on the steps after it. Whether a ready step already has an active run is a question for
the database, not for this module.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta

from evo_agents.hub.plans import step_key

PROTOCOL_HEADER = "X-Evo-Worker-Protocol"  # on every /v1/worker/* request; any other value is answered with 426
PROTOCOL_VERSION = "1"

HEARTBEAT_SECONDS = 15  # how often the daemon sends a heartbeat
OFFLINE_AFTER_SECONDS = 300  # a worker with no heartbeat for longer than this is offline
LEASE_SECONDS = 300  # a claim and each heartbeat set a held run's lease to expire this long after now
CLAIM_WAIT_SECONDS = 25  # the longest a claim waits for a run before answering that there is none
MAX_ATTEMPTS = 3  # runs of one dispatch, the first included; the last one's expired lease fails it

MAX_BATCH_EVENTS = 500  # events in one POST of a run's events
MAX_BATCH_BYTES = 1024 * 1024  # body of that POST
MAX_EVENT_BODY_BYTES = 64 * 1024  # one event's body as JSON; a longer one is cut and marked truncated
MAX_RUN_EVENTS = 20_000  # events the hub keeps for one run
MAX_MESSAGE_BYTES = 8 * 1024  # one message from the owner to a running agent
MAX_PROMPT_BYTES = 32 * 1024  # the prompt build_prompt returns, as UTF-8

RUNTIMES = ("claude-code", "opencode", "codex")
MODES = ("headless", "interactive")
APPROVALS = ("auto", "review")
CONTROLS = ("cancel", "takeover", "handback", "terminal_open", "inbox", "drain")  # what a heartbeat answer carries

HELD_STATES = ("leased", "running", "interactive", "verifying")  # a worker holds the run and extends its lease
ACTIVE_STATES = ("queued", *HELD_STATES, "review")  # at most one run of a step is in one of these
TERMINAL_STATES = ("done", "failed", "lost", "cancelled")
RUN_STATES = ACTIVE_STATES + TERMINAL_STATES
TAKEOVER_STATES = ("leased", "running")  # the owner may ask for a takeover while the agent is not driven by a person
HANDBACK_STATES = ("interactive",)  # and for a handback while it is
MESSAGE_STATES = ("queued", *HELD_STATES)  # a message waits in the inbox only while an agent may still read it
ACTORS = ("worker", "owner", "reaper")

_W, _O, _R = frozenset({"worker"}), frozenset({"owner"}), frozenset({"reaper"})
_HELD_EXITS = {"failed": _W | _R, "cancelled": _W | _R, "lost": _R}  # the worker gives up or stops; the lease ends

TRANSITIONS: dict[str, dict[str, frozenset[str]]] = {
    "queued": {"leased": _W, "cancelled": _O, "failed": _R},
    "leased": {"running": _W, "interactive": _W, **_HELD_EXITS},
    "running": {"interactive": _W, "verifying": _W, **_HELD_EXITS},
    "interactive": {"running": _W, "verifying": _W, **_HELD_EXITS},
    "verifying": {"done": _W, "review": _W, **_HELD_EXITS},
    "review": {"done": _O, "cancelled": _O},
    **{state: {} for state in TERMINAL_STATES},
}

WORKER_STATUSES = ("online", "offline", "draining", "revoked")

ACP_EVENT_KINDS = (  # the session/update kinds of the Agent Client Protocol
    "agent_message_chunk",
    "agent_thought_chunk",
    "tool_call",
    "tool_call_update",
    "plan",
    "usage_update",
)
HUB_EVENT_KINDS = ("user_message", "state", "system", "output")  # output: a runtime event no adapter knows, raw
EVENT_KINDS = ACP_EVENT_KINDS + HUB_EVENT_KINDS
WORKER_EVENT_KINDS = ACP_EVENT_KINDS + ("system", "output")  # the hub alone writes user_message and state

EVENT_CUT_MARK = "\n[cut by the hub: the event was over {limit} bytes]"
MAX_CUT_PASSES = 64  # strings of one body fit_event_body cuts before it keeps the start of the body's JSON instead

RESULT_DIR = ".evo-run"  # in the run's checkout, never committed
RESULT_FILE = f"{RESULT_DIR}/result.json"  # {"verify_commands": [str, ...], "summary": str}, written by the agent

TRUNCATION_MARK = "\n[cut by the hub: {omitted} of {total} bytes left out; the plan on the hub has the full text]"
SHORT_BYTES = 200  # a plan id, step key, title, repo or branch in the prompt
GOAL_BYTES = 2 * 1024
CONTEXT_BYTES = 4 * 1024
WHAT_BYTES = 10 * 1024
VERIFY_BYTES = 3 * 1024
NOTE_BYTES = 2 * 1024
EVIDENCE_BYTES = 1536  # the evidence of one step this one depends on
DEPENDENCIES_BYTES = 5 * 1024  # all of it


class RunProblem(ValueError):
    """A move, a run or a plan the hub refuses; ``str`` says why for the person or worker who asked."""


class TransitionRefused(RunProblem):
    pass


# States


def can_transition(old: str, new: str, actor: str) -> bool:
    """Whether ``actor`` may move a run from ``old`` to ``new``."""
    return actor in TRANSITIONS.get(old, {}).get(new, ())


def check_transition(old: str, new: str, actor: str) -> None:
    """Raise TransitionRefused unless TRANSITIONS lets ``actor`` move a run from ``old`` to ``new``."""
    for state in (old, new):
        if state not in TRANSITIONS:
            raise TransitionRefused(f"{state!r} is not a run state; the states are {', '.join(RUN_STATES)}")
    if actor not in ACTORS:
        raise TransitionRefused(f"{actor!r} cannot move runs; the ones who can are {', '.join(ACTORS)}")
    if old == new:
        raise TransitionRefused(f"the run is already {new}")
    if old in TERMINAL_STATES:
        raise TransitionRefused(f"the run is {old}, which is final: running the step again takes a new run")
    allowed = TRANSITIONS[old].get(new)
    if not allowed:
        raise TransitionRefused(f"a run cannot go from {old} to {new}")
    if actor not in allowed:
        who = " or the ".join(name for name in ACTORS if name in allowed)
        raise TransitionRefused(f"the {actor} cannot move a run from {old} to {new}; only the {who} can")


# Events


def _json_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def _longest_string(value) -> tuple[object, object, int] | None:
    """(container, key, size) of the string in ``value`` whose JSON is the largest, among those longer than a cut
    mark; None when there is none. A walk with a stack of its own, so a deeply nested body cannot exhaust the
    interpreter's."""
    best = None
    stack = [value]
    while stack:
        node = stack.pop()
        items = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()
        for key, child in items:
            if isinstance(child, str):
                size = _json_size(child)
                if size > len(EVENT_CUT_MARK) + 16 and (best is None or size > best[2]):
                    best = (node, key, size)
            elif isinstance(child, (dict, list)):
                stack.append(child)
    return best


def _cut_strings(body: dict, limit: int) -> bool:
    """Cut the longest strings of ``body`` in place until its JSON takes at most ``limit`` bytes; whether it does."""
    mark = EVENT_CUT_MARK.format(limit=limit)
    mark_size = _json_size(mark) - 2  # in a JSON string, without its quotes
    for _ in range(MAX_CUT_PASSES):
        size = _json_size(body)
        if size <= limit:
            return True
        found = _longest_string(body)
        if found is None:
            return False
        node, key, _ = found
        text = node[key].removesuffix(mark)  # a string cut before is cut again from its text, not its mark
        size -= _json_size(node[key]) - _json_size(text)
        data = text.encode()
        # Drop as many bytes as the text's JSON takes per byte (more where it is escaped); a string escaped unevenly
        # may need another pass.
        ratio = (_json_size(text) - 2) / len(data)
        drop = math.ceil((size + mark_size - limit) / ratio)
        node[key] = data[: max(0, len(data) - drop)].decode("utf-8", "ignore") + mark
    return _json_size(body) <= limit


def fit_event_body(body: dict, limit: int = MAX_EVENT_BODY_BYTES) -> tuple[dict, bool]:
    """``body`` and False when its JSON takes at most ``limit`` bytes of UTF-8. Otherwise a cut copy and True: its
    longest strings end early with EVENT_CUT_MARK, so the keys of the body stay and the web shows the start of the
    text; and when cutting strings is not enough (a body of many short values), ``{"cut": ...}`` with the start of
    the body's JSON as text."""
    if _json_size(body) <= limit:
        return body, False
    copy = json.loads(json.dumps(body))
    if _cut_strings(copy, limit):
        return copy, True
    fallback = {"cut": json.dumps(body, ensure_ascii=False, separators=(",", ":"))}
    if not _cut_strings(fallback, limit):
        fallback = {"cut": EVENT_CUT_MARK.format(limit=limit).strip()}
    return fallback, True


def step_title(step: dict) -> str | None:
    """The title a run keeps of its step: one line of at most SHORT_BYTES characters, or None."""
    title = step.get("title") if isinstance(step, dict) else None
    if title is None:
        return None
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in _text(title)).split())
    return text[:SHORT_BYTES] or None


# Workers


def worker_status(
    *, last_heartbeat_at: datetime | None, drained_at: datetime | None, revoked_at: datetime | None, now: datetime
) -> str:
    """``revoked`` once revoked; else ``offline`` when the last heartbeat is more than OFFLINE_AFTER_SECONDS old or
    there was none; else ``draining`` once drained (it finishes its runs and claims no new one); else ``online``.
    Offline wins over draining: a drained worker that is down finishes nothing."""
    if revoked_at is not None:
        return "revoked"
    if last_heartbeat_at is None or now - last_heartbeat_at > timedelta(seconds=OFFLINE_AFTER_SECONDS):
        return "offline"
    if drained_at is not None:
        return "draining"
    return "online"


# Which steps are ready


def _steps(body: dict) -> list:
    steps = body.get("steps")
    return steps if isinstance(steps, list) else []


def _status(step: dict):
    status = step.get("status")
    return "pending" if status is None else status


def unready_reason(body: dict, step) -> str | None:
    """Why ``step``, one of the steps of ``body``, is not ready to run; None when it is."""
    if not isinstance(step, dict):
        return "it is a bare string, not a mapping, so it has no status"
    if _status(step) != "pending":
        return f"its status is {_status(step)}, not pending"
    depends_on = step.get("depends_on")
    if depends_on is None:
        return None
    if not isinstance(depends_on, list):
        return "its depends_on is not a list of step ids"
    statuses: dict[str, object] = {}
    for index, other in enumerate(_steps(body)):
        if isinstance(other, dict):
            statuses.setdefault(step_key(other, index), _status(other))
    waiting = []
    for dep in depends_on:
        key = str(dep)
        if key not in statuses:
            waiting.append(f"step {key}, which the plan does not have")
        elif statuses[key] != "done":
            waiting.append(f"step {key} ({statuses[key]})")
    return f"it waits for {', '.join(waiting)}" if waiting else None


def ready_steps(body: dict) -> list[dict]:
    """The steps of the plan ``body`` that are ready to run, in plan order (see the module's docstring)."""
    return [step for step in _steps(body) if unready_reason(body, step) is None]


# The prompt


def clip(text: str, limit: int) -> str:
    """``text`` in at most ``limit`` UTF-8 bytes: whole when it fits, else its start, cut on a character boundary,
    and TRUNCATION_MARK saying how much was left out."""
    data = text.encode()
    if len(data) <= limit:
        return text
    longest_mark = TRUNCATION_MARK.format(omitted=len(data), total=len(data)).encode()
    if len(longest_mark) >= limit:
        return data[:limit].decode("utf-8", "ignore")
    head = data[: limit - len(longest_mark)].decode("utf-8", "ignore")
    return head + TRUNCATION_MARK.format(omitted=len(data) - len(head.encode()), total=len(data))


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False)


def _short(value, missing: str) -> str:
    text = " ".join(_text(value).split())
    return clip(text, SHORT_BYTES) if text else missing


def plan_repo(plan: dict, step: dict) -> dict | None:
    """The entry of the plan's ``repos`` for the repo ``step`` names; the plan's only repo when the step names none.
    A repo the step names but the plan does not list gets an entry without a branch."""
    repos = [entry for entry in plan.get("repos") or [] if isinstance(entry, dict)]
    name = step.get("repo")
    if name is None:
        return repos[0] if len(repos) == 1 else None
    return next((entry for entry in repos if entry.get("repo") == name), {"repo": name})


def _rules(branch: str) -> list[str]:
    return [
        "Rules for this run:",
        "- Do this one step and nothing else. The other steps of the plan get runs of their own.",
        "- Do not record progress in the plan: do not call the hub's plan_step tool, `evo harness step` or "
        "`evo-agents hub plan`. The hub writes this step's status and evidence from what this run reports.",
        f"- Work in the current directory and commit there, on the branch it is on ({branch}). Do not switch "
        "branches, merge or push: the worker pushes the branch when the run ends.",
        f"- Before you finish, write {RESULT_FILE} as a JSON object with two keys, and keep {RESULT_DIR}/ out of "
        "your commits:",
        '  "verify_commands": the shell commands you ran to check this step, each one a string;',
        '  "summary": what you changed, how you checked it, and anything left undone.',
        "  The worker runs every verify command again in this directory, and the step counts as verified only when "
        "each one exits 0.",
    ]


def _dependencies(plan: dict, step: dict) -> list[str]:
    depends_on = step.get("depends_on")
    if not isinstance(depends_on, list) or not depends_on:
        return []
    by_key: dict[str, dict] = {}
    for index, other in enumerate(_steps(plan)):
        if isinstance(other, dict):
            by_key.setdefault(step_key(other, index), other)
    parts = []
    for dep in depends_on:
        other = by_key.get(str(dep))
        if other is None:
            parts.append(f"## Step {_short(dep, '?')}\nThe plan has no such step.")
            continue
        evidence = clip(_text(other.get("evidence")) or "No evidence recorded.", EVIDENCE_BYTES)
        parts.append(f"## Step {_short(dep, '?')}: {_short(other.get('title'), 'untitled')}\n{evidence}")
    return ["# Evidence from the steps this one depends on", clip("\n\n".join(parts), DEPENDENCIES_BYTES)]


def build_prompt(plan: dict, step: dict, repo: dict | None) -> str:
    """The prompt of a run of ``step``, one of the steps of ``plan``, in the checkout of ``repo`` (an entry of the
    plan's ``repos``, as ``plan_repo`` finds it): what the agent must and must not do, the plan's goal and its
    context, shortened, the step's what, verify and note, and the evidence of the steps it depends on. Each part is
    cut to its own budget, and the whole to MAX_PROMPT_BYTES, with TRUNCATION_MARK where something was cut."""
    index = next((position for position, other in enumerate(_steps(plan)) if other is step), 0)
    key = _short(step_key(step, index), "?")
    repo = repo or {}
    branch = _short(repo.get("branch"), "the branch checked out")
    lines = [
        f"You are running step {key} of the plan {_short(plan.get('id'), 'without an id')} from the evo-agents hub.",
        "",
        *_rules(branch),
        "",
        f"Repository: {_short(repo.get('repo'), 'not named by the plan')}. Branch: {branch}.",
    ]
    for heading, value, budget in (
        ("# Goal of the plan", plan.get("goal"), GOAL_BYTES),
        ("# Context of the plan, shortened", plan.get("context"), CONTEXT_BYTES),
    ):
        if _text(value):
            lines += ["", heading, clip(_text(value), budget)]
    lines += ["", f"# Step {key}: {_short(step.get('title'), 'untitled')}"]
    for heading, value, budget in (
        ("## What to do", step.get("what"), WHAT_BYTES),
        ("## How the step is verified", step.get("verify"), VERIFY_BYTES),
        ("## Note", step.get("note"), NOTE_BYTES),
    ):
        if _text(value):
            lines += ["", heading, clip(_text(value), budget)]
    dependencies = _dependencies(plan, step)
    if dependencies:
        lines += ["", *dependencies]
    return clip("\n".join(lines) + "\n", MAX_PROMPT_BYTES)
