"""``evo-agents hub run watch [RUN ...] [--plan PLAN[:STEP,...]] [--state STATE] [--no-decisions] [--for MIN]
[--every SEC]``, for ``evo_agents.hub.cli.run``: wait until something on the hub changes that an agent waiting on it
needs to see, print what changed and exit 0; after ``--for`` minutes with nothing changed, say so and exit
EXIT_NO_CHANGE.

What counts as a change, against the first look:

- a watched run is in another state (with ``--state``, only once it is in one of those states);
- a decision of the project opened or closed, a run's own wait aside (category ``wait``: it needs nobody, and the
  run's move to waiting or parked says the same);
- a step of a watched plan turned blocked or descoped, a step named after ``PLAN:`` has another status or is gone,
  or every step of the plan is closed (done or descoped).

Every ``--every`` seconds it reads each run (GET .../runs/{id}), the project's open decisions (GET
.../decisions?state=open) and each plan (GET .../plans/{id}). A read the hub could not answer for a moment (no
answer, 408, 429, any 5xx, or the proxy's 404 while the hub is redeployed: ``passing``) skips that look,
with one note on stderr per outage; the first look is tried again the same way until the time is up. Any other
refusal ends the command with the hub's error. When every run named has already ended at the first look and no plan
is watched, it exits 0 at once: nothing it waits for can come.

It is for an agent in an interactive session: started in the background (Bash with ``run_in_background``, or
Monitor), it wakes the session when it exits. A plan run on a worker waits with ``evo-agents worker wait``, which
parks the run; its wait guard (``evo_agents.worker.wait_guard``) refuses this command. Standard library only, like
the rest of the client.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from evo_agents.clikit import Arg, Result, UsageError
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext
from evo_agents.hub.cli.plan import Plans
from evo_agents.hub.cli.run.resources import Decisions, Runs
from evo_agents.hub.client import HubError, transient
from evo_agents.hub.plan_writes import checked_plan_id
from evo_agents.hub.plans import CLOSED_STEP_STATUSES
from evo_agents.hub.runs import SHOWN_STATES, TERMINAL_STATES

EXIT_NO_CHANGE = 3  # the time ran out and nothing watched changed
DEFAULT_MINUTES = 360
MAX_MINUTES = 7 * 24 * 60  # a parked run is cancelled after a week (runs.PARKED_DAYS)
DEFAULT_EVERY = 60
MIN_EVERY = 15  # seconds between looks at the least: each look is a request per run and plan, and one for decisions
DECISIONS_PAGE = 200  # the most decisions the hub lists at once (``evo_agents.hub.cli.decision.MAX_LIST``)
QUIET_CATEGORIES = frozenset({"wait"})  # decisions that need nobody: a run's own wait for a time or an event
ATTENTION_STATUSES = frozenset({"blocked", "descoped"})  # a step that turns one of these wakes a plan's watch
FINAL_STATES = frozenset({*TERMINAL_STATES, "ended"})  # ended: a local run's session is over
QUESTION_CHARS = 100


def passing(exc: HubError) -> bool:
    """Whether a look that failed with ``exc`` is skipped: ``client.transient``, and any 5xx too. A look only reads, so
    looking again cannot do anything twice, and while the hub is redeployed the proxy in front of it answers a plain
    500 for a minute or two (2026-11-26), not only 502."""
    return transient(exc) or (exc.status is not None and exc.status >= 500)


@dataclass(frozen=True)
class Look:
    """What one look found: each run's state, the open decisions that need someone, each plan's step statuses."""

    runs: dict[int, str]
    decisions: dict[int, dict]
    plans: dict[str, dict[str, str]]


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _plan_option(text: str) -> tuple[str, tuple[str, ...]]:
    """``PLAN`` or ``PLAN:STEP,STEP`` as the plan's id and the steps named."""
    plan, colon, steps = text.partition(":")
    named = tuple(step.strip() for step in steps.split(",") if step.strip())
    if colon and not named:
        raise HubError(f"--plan {text!r} names no step after the colon")
    return checked_plan_id(plan), named


def watched_plans(given: list[str]) -> dict[str, tuple[str, ...]]:
    """The ``--plan`` options as each plan watched with the steps named, a plan given twice once."""
    plans: dict[str, tuple[str, ...]] = {}
    for text in given:
        plan, steps = _plan_option(text)
        plans[plan] = tuple(dict.fromkeys((*plans.get(plan, ()), *steps)))
    return plans


def look(hub, project: str, runs: tuple[int, ...], plans: tuple[str, ...], decisions: bool) -> Look:
    """One look at the hub; HubError as ``Hub.call`` raises it."""
    states = {run: Runs(hub, project).get(run)["state"] for run in runs}
    opened: dict[int, dict] = {}
    if decisions:
        listed = Decisions(hub, project).open(DECISIONS_PAGE)
        opened = {
            decision["id"]: decision
            for decision in listed["decisions"]
            if decision.get("category") not in QUIET_CATEGORIES
        }
    steps = {}
    for plan in plans:
        body = Plans(hub, project).get(plan)["body"]
        steps[plan] = {str(step["id"]): str(step.get("status") or "pending") for step in body.get("steps") or ()}
    return Look(states, opened, steps)


def changes(
    first: Look, now: Look, named: dict[str, tuple[str, ...]], wanted: frozenset[str] = frozenset()
) -> list[str]:
    """What changed from ``first`` to ``now`` that wakes the watch, one line each; empty when nothing did."""
    lines = []
    for run, state in now.runs.items():
        before = first.runs[run]
        if state != before and (not wanted or state in wanted):
            lines.append(f"run #{run}: {before} -> {state}")
    for decision_id, decision in now.decisions.items():
        if decision_id not in first.decisions:
            question = _clip(decision.get("question") or "", QUESTION_CHARS)
            lines.append(f"decision #{decision_id} opened (run #{decision.get('run_id')}): {question}")
    lines += [f"decision #{decision_id} closed" for decision_id in first.decisions if decision_id not in now.decisions]
    for plan, steps in now.plans.items():
        before = first.plans[plan]
        for step in named.get(plan, ()):
            if step not in steps:
                lines.append(f"plan {plan}: step {step} is gone")
        for step, status in steps.items():
            was = before.get(step)
            if status != was and (step in named.get(plan, ()) or status in ATTENTION_STATUSES):
                lines.append(f"plan {plan}: step {step} {was or 'new'} -> {status}")
        if _closed(steps) and not _closed(before):
            lines.append(f"plan {plan}: every step is closed")
    return lines


def _closed(steps: dict[str, str]) -> bool:
    return bool(steps) and all(status in CLOSED_STEP_STATUSES for status in steps.values())


def summary(seen: Look, named: dict[str, tuple[str, ...]], decisions: bool = True) -> str:
    """A look in one line: each run's state, the open decisions, each plan's closed steps and the named ones."""
    parts = [f"run #{run} {state}" for run, state in seen.runs.items()]
    if decisions:
        ids = ", ".join(f"#{decision_id}" for decision_id in sorted(seen.decisions))
        parts.append(f"{len(seen.decisions)} open decision(s)" + (f" ({ids})" if ids else ""))
    for plan, steps in seen.plans.items():
        closed = sum(status in CLOSED_STEP_STATUSES for status in steps.values())
        marks = [f"step {step} {steps.get(step, 'gone')}" for step in named.get(plan, ())]
        marks += [f"step {step} blocked" for step, status in steps.items() if status == "blocked"]
        parts.append(f"plan {plan} {closed}/{len(steps)} closed" + (f" ({', '.join(marks)})" if marks else ""))
    return "; ".join(parts)


@dataclass
class Clock:
    """The time the watch reads and sleeps on, replaced in tests."""

    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    wall: Callable[[], datetime] = lambda: datetime.now(UTC)


def _hhmm(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%H:%M")


def watch(
    hub,
    project: str,
    runs: tuple[int, ...],
    plans: dict[str, tuple[str, ...]],
    *,
    decisions: bool = True,
    wanted: frozenset[str] = frozenset(),
    minutes: int = DEFAULT_MINUTES,
    every: int = DEFAULT_EVERY,
    clock: Clock | None = None,
    out: Callable[[str], None] = lambda line: print(line, flush=True),
    warn: Callable[[str], None] = lambda line: print(line, file=sys.stderr, flush=True),
) -> int:
    """Look at the hub every ``every`` seconds until something changed (0) or ``minutes`` passed (EXIT_NO_CHANGE)."""
    clock = clock or Clock()
    started, deadline = clock.monotonic(), clock.monotonic() + minutes * 60
    until = clock.wall() + timedelta(minutes=minutes)
    outage = False

    def take() -> Look | None:
        nonlocal outage
        try:
            seen = look(hub, project, runs, tuple(plans), decisions)
        except HubError as exc:
            if not passing(exc):
                raise
            if not outage:
                warn(f"note: the hub did not answer ({exc}); looking again every {every} s")
            outage = True
            return None
        if outage:
            warn("note: the hub answers again")
        outage = False
        return seen

    first = take()
    while first is None:
        left = deadline - clock.monotonic()
        if left <= 0:
            raise HubError(f"the hub did not answer a first look in {minutes} min")
        clock.sleep(min(every, left))
        first = take()
    for plan, steps in plans.items():
        missing = [step for step in steps if step not in first.plans[plan]]
        if missing:
            have = ", ".join(first.plans[plan]) or "none"
            raise HubError(f"plan {plan} has no step {', '.join(missing)}; its steps: {have}")
    said = summary(first, plans, decisions)
    if runs and not plans and all(state in FINAL_STATES for state in first.runs.values()):
        out(f"Nothing left to wait for: {said}.")
        return 0
    out(f"Watching until {_hhmm(until)} UTC, a look every {every} s: {said}.")
    now = first
    while (left := deadline - clock.monotonic()) > 0:
        clock.sleep(min(every, left))
        now = take() or now
        found = changes(first, now, plans, wanted)
        if found:
            after = round((clock.monotonic() - started) / 60)
            out(f"Changed at {_hhmm(clock.wall())} UTC, after {after} min:")
            for line in found:
                out(f"  {line}")
            out(f"Now: {summary(now, plans, decisions)}.")
            return 0
    out(f"No change in {minutes} min, at {_hhmm(clock.wall())} UTC. Now: {summary(now, plans, decisions)}.")
    return EXIT_NO_CHANGE


DESCRIPTION = (
    "Look at the hub every --every seconds and exit 0 as soon as something changed against the first "
    "look: a run named moved to another state, a decision of the project opened or closed (a run's own wait "
    "aside), or a step of a plan watched turned blocked or descoped, changed status when it is named after "
    "PLAN:, or every step of the plan is closed. After --for minutes with no change it exits "
    f"{EXIT_NO_CHANGE}. A look the hub could not answer for a moment (a redeploy, 5xx) is skipped. Start it in "
    "the background of an interactive session: its exit wakes the agent. A plan run on a worker waits with "
    "`evo-agents worker wait` instead."
)


class RunWatch(HubCommand):
    """Writes each line as ``watch`` says it, as the looks go: the session waiting on it reads them when it exits."""

    name = "watch"
    help = (
        "wait until a run's state, an open decision of the project or a plan's step changes, then say what "
        "changed; for an agent's background wait"
    )
    parser_options = {"description": DESCRIPTION}
    arguments = (
        Arg("runs", metavar="RUN", type=int, nargs="*", help="a run's id, as `hub run list` shows it"),
        PROJECT,
        Arg(
            "--plan",
            action="append",
            metavar="PLAN[:STEP,...]",
            help="watch this plan's steps: a step turning blocked or descoped, or every step closed, wakes it; with "
            ":STEP (or :STEP,STEP), so does any change of those steps' status; repeat it for several plans",
        ),
        Arg(
            "--state",
            action="append",
            choices=SHOWN_STATES,
            metavar="STATE",
            help=f"wake only when a run named reaches one of these states, one of {', '.join(SHOWN_STATES)}; repeat "
            "it for several (default: any other state)",
        ),
        Arg("--no-decisions", dest="decisions", action="store_false", help="do not watch the project's open decisions"),
        Arg(
            "--for",
            dest="minutes",
            type=int,
            default=DEFAULT_MINUTES,
            metavar="MIN",
            help=f"give up after this many minutes with no change, exit {EXIT_NO_CHANGE} (default {DEFAULT_MINUTES})",
        ),
        Arg(
            "--every",
            type=int,
            default=DEFAULT_EVERY,
            metavar="SEC",
            help=f"seconds between looks, {MIN_EVERY} or more (default {DEFAULT_EVERY})",
        ),
    )

    def check(self, args: argparse.Namespace) -> None:
        if not args.runs and not args.plan and not args.decisions:
            raise UsageError("nothing to watch: name a run or a --plan, or leave decisions watched")
        if not 1 <= args.minutes <= MAX_MINUTES:
            raise UsageError(f"--for takes 1 to {MAX_MINUTES} minutes")
        if args.every < MIN_EVERY:
            raise UsageError(f"--every takes {MIN_EVERY} seconds or more")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        plans = watched_plans(args.plan or [])
        status = watch(
            ctx.hub,
            ctx.project,
            tuple(dict.fromkeys(args.runs)),
            plans,
            decisions=args.decisions,
            wanted=frozenset(args.state or ()),
            minutes=args.minutes,
            every=args.every,
            out=lambda line: print(line, file=ctx.out, flush=True),
        )
        return Result(None, status=status)
