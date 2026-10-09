"""Runs and workers as the hub models them: the states of a run and who may move a run between them, a worker's
status as its heartbeats show it, the kinds of a run's events, which steps of a plan are ready to run, the prompt a
run hands its agent, and the decisions and notices a plan run sends its owner. Pure functions over JSON values and
datetimes, standard library only, so the api and the worker daemon share them. ``docs/workers.md`` describes the
protocol built on them, and ``docs/notifications.md`` how decisions and notices reach the owner.

A run is of one of RUN_KINDS. A ``step`` run is one attempt at one plan step on one worker. A ``plan`` run is one
long session on one worker that does every step of a plan that is not done yet, in the order ``depends_on`` allows,
and reports each step as it goes (``build_plan_prompt`` tells its agent how). A ``review`` run is the Curator's
Reviewer of one night of a project's charter: it works on no plan, reads the project and writes findings and
proposals, and changes no code (``evo_agents.hub.review``). A ``judge`` run is the Curator's Judge of one change its
Builder made: it reads the change at the head of its pull request, runs the plan's verify and the project's hidden
checks, and says whether the change passes, without reading the Builder's transcript and without changing anything
(``evo_agents.hub.judge``). An ``author`` run writes an execution plan from a member's request with the
create-exec-plan skill, on one of the member's own workers, and changes no code (``evo_agents.hub.author``). A worker
takes a kind of run only once its heartbeat says its daemon runs that kind (``run_kinds``): a daemon older than the
review run reads every run as a step or plan run.
A run starts ``queued``; a worker
claims it (``leased``), starts the agent (``running``, or ``interactive`` when a person drives the agent in a
terminal), re-runs the agent's verify commands (``verifying``), and ends ``done``, ``failed`` or ``cancelled``, or in
``review`` until the owner approves it. A run whose worker stops extending its lease is ``lost``, and the hub queues a
new run for the same step (attempt + 1), or ``failed`` when it was the last of MAX_ATTEMPTS.

A plan run's agent may ask its owner a decision of DECISION_CATEGORIES. When its turn ends with a decision still
open, the run is ``waiting``: the worker keeps it, with its slot and lease, and hands the answer to the agent in the
same session, and the run is ``running`` again. A run that waited DECISION_WAIT_SECONDS is ``parked``: the worker lets
it go and frees the slot, and keeps the agent's session and the worktrees. An answer then queues a new plan run on the
same worker that goes on in that session (the run it resumes is ``done``), and a run parked PARKED_DAYS is
``cancelled``. Neither waiting nor parked counts toward the run's timeout: ``run_seconds`` counts only CLOCK_STATES.

TRANSITIONS names, for each move, who may do it: the ``worker`` that holds the run, the ``owner`` (the member who
dispatched it, who is the owner of the worker), or the ``reaper``, the hub itself acting on an expired lease, a revoked
worker, a run nobody can claim any more, or a decision nobody answered.

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
import re
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
MAX_PROMPT_BYTES = 32 * 1024  # the prompt build_prompt or build_plan_prompt returns, as UTF-8

# one step; every step of a plan not done yet; a night's review of a project; the Judge of a change of the Curator; a
# plan written from a member's request
RUN_KINDS = ("step", "plan", "review", "judge", "author")
PLAN_KINDS = ("step", "plan")  # the kinds of run that work on a plan; a review run has none (its plan_id is null)
CURATOR_KINDS = ("review", "judge")  # the kinds only the night shift queues, besides the Builder's plan runs
PLAN_RUN_AGENT = (0, 4, 0)  # the first daemon release that runs a plan run; an older one reads it as a step run
RUNTIMES = ("claude-code", "opencode", "codex")
MAX_MODEL_CHARS = 200  # a run's model, one line (runs.model, as schema 0010 bounds it), and each model a runtime lists
MAX_RUNTIME_MODELS = 200  # the models a heartbeat lists for one runtime
MODES = ("headless", "interactive")
APPROVALS = ("auto", "review")
# What a heartbeat answer carries: per run, whether to cancel, take over, hand back, open the terminal or park, how
# many inbox messages and open decisions it has; for the worker, whether to drain.
CONTROLS = ("cancel", "takeover", "handback", "terminal_open", "park", "inbox", "decisions", "drain")

DECISION_WAIT_SECONDS = 24 * 3600  # a run waiting this long for its owner's answer is parked
PARKED_DAYS = 7  # a run parked this long is cancelled, and its open decisions expire
PLAN_TIMEOUT_CHOICES = (2, 4, 8, 24)  # hours of agent time a plan run may take; waiting and parked do not count

HELD_STATES = ("leased", "running", "interactive", "verifying", "waiting")  # a worker holds the run, extends its lease
ACTIVE_STATES = ("queued", *HELD_STATES, "review", "parked")  # at most one run of a step, or plan run of a plan
TERMINAL_STATES = ("done", "failed", "lost", "cancelled")
RUN_STATES = ACTIVE_STATES + TERMINAL_STATES
# The time a run spends in these counts toward its timeout; waiting for an answer, parked, queued and review do not.
CLOCK_STATES = ("leased", "running", "interactive", "verifying")
TAKEOVER_STATES = ("leased", "running")  # the owner may ask for a takeover while the agent is not driven by a person
HANDBACK_STATES = ("interactive",)  # and for a handback while it is
MESSAGE_STATES = ("queued", *HELD_STATES)  # a message waits in the inbox only while an agent may still read it
ACTORS = ("worker", "owner", "reaper")

_W, _O, _R = frozenset({"worker"}), frozenset({"owner"}), frozenset({"reaper"})
_HELD_EXITS = {"failed": _W | _R, "cancelled": _W | _R, "lost": _R}  # the worker gives up or stops; the lease ends

TRANSITIONS: dict[str, dict[str, frozenset[str]]] = {
    "queued": {"leased": _W, "cancelled": _O, "failed": _R},
    "leased": {"running": _W, "interactive": _W, **_HELD_EXITS},
    "running": {"interactive": _W, "verifying": _W, "waiting": _W, **_HELD_EXITS},
    "interactive": {"running": _W, "verifying": _W, **_HELD_EXITS},
    "verifying": {"done": _W, "review": _W, **_HELD_EXITS},
    # the agent's turn ended with a decision open: the answer sets it going again, DECISION_WAIT_SECONDS parks it
    "waiting": {"running": _W, "parked": _R, **_HELD_EXITS},
    "review": {"done": _O, "cancelled": _O},
    # no worker holds it: the owner's answer queues the run that resumes it, and this one is done; PARKED_DAYS, or the
    # owner, cancel it
    "parked": {"done": _O, "cancelled": _O | _R},
    **{state: {} for state in TERMINAL_STATES},
}

# Decisions and notices of a plan run (docs/notifications.md). The agent asks its owner only a decision of one of
# these categories, and decides everything else itself.
DECISION_CATEGORIES = (
    "deploy",
    "delete_data",
    "live_migration",
    "external_send",
    "spend_money",
    "architecture",
    "scope",
)
DECISION_CATEGORY_TEXT = {
    "deploy": "deploying or releasing anything to any environment, a checkpoint step that deploys included",
    "delete_data": "deleting data that is not this run's own scratch: database rows, files, buckets, other "
    "people's branches",
    "live_migration": "a migration, backfill or bulk change on real data rather than a test database",
    "external_send": "sending anything to a service outside this machine and the repos' own remotes: mail, chat, "
    "issues, third-party APIs",
    "spend_money": "anything that costs money beyond this runtime's own usage: paid APIs, cloud resources, purchases",
    "architecture": "an architectural choice the plan leaves open",
    "scope": "a question of scope the plan leaves open: adding, dropping or reshaping a step or what it delivers",
}
DECISION_STATES = ("open", "answered", "expired", "cancelled")
DECISION_OPTIONS = (2, 6)  # the fewest and the most options a decision offers
OPTION_KEY = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$"  # the key of an option, as `worker ask --option KEY=LABEL` gives it
MAX_DECISION_CONTEXT_BYTES = 16 * 1024  # the markdown context of a decision
MAX_QUESTION_CHARS = 2000  # the question of a decision
MAX_ANSWER_BYTES = 4 * 1024  # the owner's own text in an answer, which goes to the agent in an inbox message
MAX_NOTICE_BODY_BYTES = 16 * 1024  # the body of a notice, and of any notification
MAX_NOTICE_COMMITS = 100  # the commits a notice of a push or merge names
# The notices a plan run's worker may send (POST /v1/worker/runs/{id}/notices); the hub sends the last two itself too.
WORKER_NOTICE_KINDS = ("push_default_branch", "merge_default_branch", "plan_finished", "run_failed")
# curator_brief: the morning brief of a project's Curator, which the hub alone sends at the charter's brief_at;
# curator_paused: the circuit breaker of a project's night shift paused it, which the hub alone sends too
NOTICE_KINDS = (*WORKER_NOTICE_KINDS, "curator_brief", "curator_paused")
NOTIFICATION_KINDS = ("decision", "notice", "proposal")  # proposal: a tier 2 proposal of the Curator, in the Inbox
DELIVERY_STATES = ("pending", "delivered", "failed")  # of one notification on one channel
MAX_DELIVERY_ATTEMPTS = 5

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

RESULT_DIR = ".evo-run"  # in the run's checkout, or above a plan run's worktrees; never committed
RESULT_FILE = f"{RESULT_DIR}/result.json"  # {"verify_commands": [str, ...], "summary": str}, written by the agent
PLAN_FILE = f"{RESULT_DIR}/plan.yaml"  # a plan run's plan as it was claimed, written by the daemon

TRUNCATION_MARK = "\n[cut by the hub: {omitted} of {total} bytes left out; the plan on the hub has the full text]"
SHORT_BYTES = 200  # a plan id, step key, title, repo or branch in the prompt
GOAL_BYTES = 2 * 1024
CONTEXT_BYTES = 4 * 1024
WHAT_BYTES = 10 * 1024
VERIFY_BYTES = 3 * 1024
NOTE_BYTES = 2 * 1024
EVIDENCE_BYTES = 1536  # the evidence of one step this one depends on
DEPENDENCIES_BYTES = 5 * 1024  # all of it
LIST_TITLE_BYTES = 120  # the title of a step in a plan run's list of steps
LIST_DEPENDS_BYTES = 120  # and its depends_on
STEP_LIST_BYTES = 14 * 1024  # the whole list; the steps past it are counted, and the plan file has them
REPOS_BYTES = 3 * 1024  # a plan run's repos with their branches and worktrees


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


# Time toward the timeout


def run_seconds(spent: int, state: str, since: datetime | None, now: datetime) -> int:
    """The seconds of a run's timeout used by ``now``: ``spent``, those counted up to ``since``, plus the time from
    ``since`` to ``now`` while the run is in one of CLOCK_STATES. A run waiting for its owner's answer, parked, queued
    or in review adds nothing, and neither does a ``since`` after ``now`` (two clocks apart). The hub settles the count
    at each move and heartbeat, so a run that ran an hour, waited a day for an answer and ran another hour has used two
    hours of its timeout."""
    if since is None or state not in CLOCK_STATES:
        return spent
    return spent + max(0, int((now - since).total_seconds()))


def past_timeout(spent: int, state: str, since: datetime | None, now: datetime, timeout_s: int) -> bool:
    """Whether a run has used more than ``timeout_s`` seconds of agent time by ``now`` (see ``run_seconds``)."""
    return run_seconds(spent, state, since, now) > timeout_s


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


def takes_plan_runs(agent_version: str | None) -> bool:
    """Whether a daemon of ``agent_version`` runs a plan run: PLAN_RUN_AGENT or later. A version whose release
    cannot be read does not, as an older daemon would fail the run."""
    match = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", (agent_version or "").strip())
    if match is None:
        return False
    return tuple(int(part or 0) for part in match.groups()) >= PLAN_RUN_AGENT


def version_text(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


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


# The prompt of a plan run


def _plan_rules() -> list[str]:
    fewest, most = DECISION_OPTIONS
    return [
        "Rules for this plan run:",
        "- If your runtime has the execute-plan skill, use it: it knows how to work inside a worker's plan run "
        "(EVO_RUN_KIND=plan). Without it, follow these rules yourself.",
        f"- {PLAN_FILE} holds the whole plan as this run was claimed. Before each step, read the plan as the hub "
        "holds it now with `evo-agents worker plan`: the hub keeps the plan's progress, not your memory of it.",
        "- A step is ready when its status is pending and every step its depends_on names is done. Do ready steps "
        "until every step is done. Checkpoint steps (a verification, a review, a deploy) are yours too.",
        "- Record progress only with `evo-agents worker step`: `evo-agents worker step KEY in_progress --repo REPO` "
        "when you start a step, and `evo-agents worker step KEY done --repo REPO --evidence TEXT --verify COMMAND` "
        "(--verify once for each command) when it is done. The worker runs each verify command again in that repo's "
        "worktree and refuses done when one exits other than 0; then it commits what is left there and pushes the "
        "repo's branch. `evo-agents worker step KEY pending --evidence TEXT` hands a step back with what is done of "
        "it. Do not call the hub's plan_step tool, `evo harness step` or `evo-agents hub plan`.",
        "- Ask the run's owner only a decision of one of these categories:",
        *(f"  - {name}: {DECISION_CATEGORY_TEXT[name]}" for name in DECISION_CATEGORIES),
        "  Decide everything else yourself, and write each such choice and why into the evidence of its step, on a "
        'line that starts with "Decision:".',
        "- To ask: `evo-agents worker ask --category CATEGORY --question TEXT --context-file FILE "
        f"--option KEY=LABEL[:DESCRIPTION] --recommended KEY --step KEY`, with {fewest} to {most} options. It prints "
        "the decision's id. Go on with the work that does not depend on the answer; when none is left, end your "
        "turn. The answer comes back in this session as a message naming the decision's id, and the time you wait "
        "does not count toward the run's timeout.",
        "- Commit in each repo's worktree, on the branch it is on. You may push, and merge into, the branch the plan "
        "names for a repo, even when it is the repo's default branch; never push or merge into a branch the plan does "
        "not name for that repo, never force-push, and never rewrite history you pushed. After each push or merge "
        "into a repo's default branch that you make yourself (the pushes of `evo-agents worker step` send their own "
        "notice), run `evo-agents worker notify --kind push_default_branch --title TEXT --body TEXT`, or "
        "`--kind merge_default_branch`, naming the repo, the branch and the commits.",
        f"- Keep {RESULT_DIR}/ out of your commits. When you stop, because every step is done or because you cannot "
        f'go on, write {RESULT_FILE} as a JSON object whose "summary" says which steps you finished, what you '
        "decided yourself, and what is left and why. The worker runs no verify command at the end of a plan run: "
        "`evo-agents worker step` ran each step's.",
    ]


def _line(value, limit: int, missing: str) -> str:
    """``value`` as one line of at most ``limit`` UTF-8 bytes: blanks collapsed, and a longer one cut on a character
    boundary and ended with "...", since a list keeps one line per entry; ``missing`` when it is empty. Only the start
    of a long value is read, so a list of many long values costs little."""
    text = value.lstrip() if isinstance(value, str) else _text(value)
    head = 4 * limit  # characters enough to fill the line, unless the value is mostly blanks
    cut = len(text) > head
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in text[:head]).split())
    if not text:
        return missing
    data = text.encode()
    if len(data) <= limit and not cut:
        return text
    return data[: max(0, limit - 3)].decode("utf-8", "ignore").rstrip() + "..."


def _packed(lines: list[str], budget: int, rest: str) -> list[str]:
    """As many of ``lines`` as fit in ``budget`` bytes, whole, then ``rest`` formatted with how many were left out."""
    kept, used = [], 0
    for shown, line in enumerate(lines):
        if used + len(line.encode()) + 1 > budget:
            return [*kept, rest.format(left=len(lines) - shown)]
        kept.append(line)
        used += len(line.encode()) + 1
    return kept


def _repo_lines(repos: list, worktrees: dict) -> list[str]:
    lines = []
    for entry in repos:
        entry = entry if isinstance(entry, dict) else {"repo": entry}
        name = _text(entry.get("repo"))
        branch = _line(entry.get("branch"), SHORT_BYTES, "the branch checked out")
        worktree = _line(worktrees.get(name, name), SHORT_BYTES, "not made yet")
        lines.append(f"- {_line(name, SHORT_BYTES, 'unnamed')}: branch {branch}, worktree {worktree}")
    if not lines:
        return ["- none named; the plan file says which repos the steps are in"]
    return _packed(lines, REPOS_BYTES, f"- and {{left}} more repos, which {PLAN_FILE} lists")


def _step_line(step, index: int) -> str:
    if not isinstance(step, dict):
        return f"- {index} [pending] {_line(step, LIST_TITLE_BYTES, 'untitled')}"
    repo = _line(step.get("repo"), SHORT_BYTES, "")
    facts = [f"repo {repo}"] if repo else []
    depends_on = step.get("depends_on")
    if isinstance(depends_on, list) and depends_on:
        names, used = [], 0
        for dep in depends_on:  # as many as the line shows, and one more so that it ends with "..."
            names.append(_line(dep, SHORT_BYTES, "?"))
            used += len(names[-1].encode()) + 2
            if used > LIST_DEPENDS_BYTES:
                break
        facts.append("after " + _line(", ".join(names), LIST_DEPENDS_BYTES, "?"))
    tail = f" ({'; '.join(facts)})" if facts else ""
    key = _line(step_key(step, index), SHORT_BYTES, "?")
    status = _line(_status(step), SHORT_BYTES, "pending")
    return f"- {key} [{status}] {_line(step.get('title'), LIST_TITLE_BYTES, 'untitled')}{tail}"


def _step_list(plan: dict) -> list[str]:
    steps = _steps(plan)
    open_steps = [
        (index, step) for index, step in enumerate(steps) if not isinstance(step, dict) or _status(step) != "done"
    ]
    lines = [
        f"# Steps not done yet: {len(open_steps)} of {len(steps)}",
        f"{PLAN_FILE} has each step's what, verify and note; the steps not listed here are done.",
    ]
    listed = [_step_line(step, index) for index, step in open_steps]
    return lines + _packed(listed, STEP_LIST_BYTES, f"- and {{left}} more steps not done, which {PLAN_FILE} lists")


def build_plan_prompt(plan: dict, repos: list, worktrees: dict | None = None) -> str:
    """The prompt of a plan run of ``plan`` over ``repos`` (the run's repos, each ``{"repo", "branch"}`` as the
    plan's ``repos`` names them), each checked out in the directory ``worktrees`` maps its name to (the repo's name,
    under the agent's directory, when it maps none): what the agent must and must not do, the repos, the plan's goal
    and its context, shortened, and a line for each step not done yet. The steps' what, verify and note stay in
    PLAN_FILE, which the daemon writes, so the prompt stays within MAX_PROMPT_BYTES however long the plan is."""
    worktrees = worktrees or {}
    lines = [
        f"You are running the plan {_line(plan.get('id'), SHORT_BYTES, 'without an id')} from the evo-agents hub, as "
        "one plan run on a worker: do every step of it that is not done yet, in the order their depends_on allows.",
        "",
        *_plan_rules(),
        "",
        "Repositories of this run, each in a worktree of its own under the current directory:",
        *_repo_lines(repos if isinstance(repos, list) else [], worktrees),
    ]
    for heading, value, budget in (
        ("# Goal of the plan", plan.get("goal"), GOAL_BYTES),
        ("# Context of the plan, shortened", plan.get("context"), CONTEXT_BYTES),
    ):
        if _text(value):
            lines += ["", heading, clip(_text(value), budget)]
    lines += ["", *_step_list(plan)]
    return clip("\n".join(lines) + "\n", MAX_PROMPT_BYTES)
