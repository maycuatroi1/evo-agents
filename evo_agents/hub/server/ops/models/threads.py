"""What the ops threads of a default system pass and write (``ops.service.threads`` is the design): the system as they
read it, the digest of a day, their limits, and the lines of a batch's summary."""

from __future__ import annotations

from dataclasses import dataclass

from evo_agents.hub import runs
from evo_agents.hub.server.systems.service import traces as system_traces

CAP_NOTICE, PAUSED_NOTICE, DIGEST_NOTICE = "ops_cap", "ops_paused", "ops_digest"  # runs.NOTICE_KINDS
RECURRING_TIMES, RECURRING_DAYS = 3, 7  # a cause key that closed this many batches in these days is recurring
BREAKER_FAILURES = 2  # ops runs in a row failed of their agent that pause a system's ops
CAP_HOURS = 24  # the window of limits.ops_daily_usd, and of the ops_cap notice
DIGEST_HOURS = 24  # the most a digest tells of
ITEMS_SHOWN = 30  # items a summary names
STEPS_SHOWN = 10  # ready steps a plan.stalled item names
DIAGNOSIS_CHARS = 300
KEY = "ops:batch:{id}"  # orchestrator_threads.key of a batch's thread
ACTIONS_LINE = (
    "Act with `evo-agents worker ops rerun|redeliver|answer-wait|curator-resume|read|finding|priority|plan-dispatch` "
    "(on the hub runtime, the tool ops_action), give work to an agent with `evo-agents worker triage reassign`, and "
    "ask the owner at most one decision for this batch with `evo-agents worker triage ask-owner`. Never decide for "
    "the owner what is theirs: credentials, scope, spending over the cap, or what cannot be undone without a backup."
)
RECURRING_LINE = (
    "Recurring: this cause key closed {times} batches in {days} days. Fix the system rather than run the work again: "
    "write a finding for the Curator (`evo-agents worker ops finding`) or give the fix to an agent as a task."
)


@dataclass(frozen=True)
class OpsSystem:
    """The default system of a project as the ops threads read it, with its owner: the person who wrote it (the
    project's creator for a system of none), as ``hub_runtime`` tells about its cap."""

    ref: system_traces.SystemRef
    paused: bool
    failures: int
    owner: int
    lead: str | None  # the profile of its orchestrator node


@dataclass(frozen=True)
class Digest:
    """What the ops of a project did since ``since`` and what waits on a person."""

    tier0: int
    orchestrator: int
    threads: int
    waiting: list[str]

    def empty(self) -> bool:
        return not (self.tier0 or self.orchestrator or self.threads or self.waiting)


def lead_of(spec: dict | None) -> str | None:
    """The Orchestrator of a system: the profile of its orchestrator node; None for none."""
    for node in (spec or {}).get("nodes") or ():
        if isinstance(node, dict) and node.get("kind") == "orchestrator" and node.get("profile"):
            return str(node["profile"])
    return None


def flat(text: str | None, fallback: str) -> str:
    """``text`` (``fallback`` when empty) on one line, clipped to DIAGNOSIS_CHARS."""
    return runs.clip(" ".join(str(text or fallback).split()), DIAGNOSIS_CHARS)


def item_line(row) -> str:
    """One item of a batch's summary: what it is, its cause, a diagnosis, and the inbox command that shows it."""
    if row.decision_id is not None:
        what = f"decision #{row.decision_id} ({row.category or 'decision'}) waits: {flat(row.question, 'no question')}"
        shown = f"evo-agents hub inbox --show d{row.decision_id}"
    elif row.run_id is not None:
        agent = f", @{row.handle}" if row.handle else ""
        state = row.run_state or "gone"
        error = flat(row.error, "its worker gave no reason")
        what = f"run #{row.run_id} ({row.run_kind or 'run'}{agent}) {state}: {error}"
        shown = f"evo-agents hub inbox --show r{row.run_id}"
    elif row.worker_id is not None:
        what = f"worker #{row.worker_id} ({row.worker or 'gone'}) reported its health: {row.cause}"
        shown = "evo-agents hub inbox"
    else:  # plan.stalled names its plan, mention.refused its agent, in the subject alone
        what = f"{row.kind} of {row.subject}: {row.cause}"
        shown = "evo-agents hub inbox"
    return f"- {row.kind}, cause {row.cause}: {what}. Next: `{shown}`"


def stalled_line(row, trace) -> str:
    """A ``plan.stalled`` item: the plan, its revision and ready steps, its last trace ``trace`` (None for none), and
    the handoff command."""
    payload = row.payload or {}
    plan_id = payload.get("plan_id") or row.subject.removeprefix("plan:")
    steps = [str(step) for step in payload.get("steps") or ()]
    last = (
        "no trace"
        if trace is None
        else f"last trace #{trace.id} of system {trace.slug}, moved {trace.last_at:%Y-%m-%d %H:%M} UTC"
    )
    ready = ", ".join(steps[:STEPS_SHOWN]) + (
        f" and {len(steps) - STEPS_SHOWN} more" if len(steps) > STEPS_SHOWN else ""
    )
    step = f" --step {steps[0]}" if steps else ""
    what = (
        f"plan {plan_id} (revision {payload.get('revision', '?')}) has ready steps {ready or 'none'} and no run works "
        f"on it; {last}"
    )
    return (
        f"- {row.kind}, cause {row.cause}: {what}. Next: `evo-agents hub plan handoff {plan_id}{step}`, then "
        f"`evo-agents worker ops plan-dispatch {plan_id}`"
    )


def queued_line(row, ahead: str) -> str:
    """A ``run.queued_long`` item: the run, the node it waits for, how many runs are ahead of it (``ahead``), its
    priority."""
    agent = f", @{row.handle}" if row.handle else ""
    node = "any node of its pool" if row.worker_id is None else f"node #{row.worker_id} ({row.worker or 'gone'})"
    what = (
        f"run #{row.run_id} (chat{agent}) {row.run_state or 'gone'} for a slot of {node}, {ahead} runs ahead of it, "
        f"priority {row.priority or runs.DEFAULT_PRIORITY}"
    )
    return (
        f"- {row.kind}, cause {row.cause}: {what}. Next: `evo-agents hub inbox --show r{row.run_id}`, then "
        f"`evo-agents worker ops priority {row.run_id} LEVEL` to move it"
    )
