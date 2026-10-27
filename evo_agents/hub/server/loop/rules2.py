"""The engine ``rules/2`` (plan problem-oriented-hub, criterion a6): the Loop goes from the problem a goal answers. It
is the engine ``rules/1`` (``rules``) with the changes below, three pure functions over a ``View``
(``model.Engine``); nothing here reads a table or writes.

- Each work item it makes carries the problem of the project its goal answers (``WorkSpec.problem_id``, from
  ``GoalView.problem_id``); one that comes of another work item (the build of a use case, the deliver of a build)
  carries that one's problem when it has one. The same holds for the opportunity a goal answers
  (``WorkSpec.opportunity_id``, plan opportunity-intake): a goal answers a problem or an opportunity, never both. The brainstorm of a goal that answers a problem names the problem, its
  metric and the claims that link it (``texts.brainstorm_task``).
- ``canvas.baseline`` moving a claim to testing makes a work item by the claim's cell, when the claim serves an active
  goal with a metric (``View.goal_of_canvas``):

  - Solution or Unique value proposition: in a project of use cases (``View.use_cases``, the policy's
    ``loop.use_cases``) the ``use_case`` of rules/1 (row 3, key ``use_case:<canvas>:<claim>:b<baseline>``), and the
    rest of its chain as rules/1 has it; otherwise a ``build`` straight from the claim, whose author run in Loop mode
    takes the problem, the goal, the claim and its test card as its request (``texts.claim_request``), then a
    ``deliver`` of the plan it wrote as rules/1's (rows 9 to 11). Key ``build:claim:<canvas>:<claim>:b<baseline>``.
  - the same cells, when the claim's delivery is ``action`` (plan problem-intake): an ``act``, whatever the project's
    use cases. It needs no code: ``act.hand_out`` makes a task of the plugin tasks with the problem, the goal, the
    claim and its test card, for the claim's creator or the project's writer. The work item is done when the task is
    done; a task cancelled, gone or past its due day is a failure as for any kind (queued again, triage, a person).
    Key ``act:<canvas>:<claim>:b<baseline>``.
  - Problem: a ``discover`` (cycle business) of @strategist, whose task run runs the claim's test card
    (``texts.discover_task``): done once that run is done; a run that fails, is lost or ends on its caps counts a
    failure as for any kind (queued again, then triage, then a person). Key ``discover:<canvas>:<claim>:b<baseline>``.
  - any other cell: no core work item. A core plugin's rule makes what it makes of the same event, as before (the
    campaign of the plugin marketing for a claim of the cells Channels and Customer segments).

  Each waits in watching (reason ``stable``) until the baseline has been approved ``loop.stable_hours``, as row 3's.
- A goal that aims at an SRS document whose baseline an admin confirmed gets a ``build`` for each group of its backlog on
  each tick (``srs_rules``, plan srs-backlog-loop); it makes no brainstorm. Key ``build:srs:<document>:<group>:<round>``.
- Everything else is rules/1's: the result of a claim validated, invalidated or retired noted in its chain (rows 4 and
  5), an approved version answering a use_case (row 6), the brainstorms of the clock (rows 1 and 2), the stale work
  items cancelled, failures, triage, and where each work item of rules/1's kinds moves.

rules/2 goes on from the cursor rules/1 left, with the same keys for the same deltas, so it makes no work item again;
the work items rules/1 made move as rules/1 moves them, and one made of them carries the problem of its goal.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from evo_agents.hub.hub_events import BASELINE, SRS_APPROVED
from evo_agents.hub.server.loop.model import (
    ACT,
    BUILD,
    CANCELLED_GROUP,
    CLAIM_BUILD,
    DISCOVER,
    DONE,
    DONE_EVENT,
    DONE_GROUP,
    ENGINE,
    PROBLEM_CELL,
    QUEUED,
    READY,
    RUNNING,
    SOLUTION_CELLS,
    STRATEGIST,
    TESTING,
    WATCHING,
    Effect,
    Event,
    ItemView,
    Transition,
    View,
    WorkSpec,
)
from evo_agents.hub.server.loop.rules import (
    NOTED_RESULTS,
    Rules,
    _failure,
    approved,
    move_build,
    noted,
    run_of,
    use_case_work,
)
from evo_agents.hub.server.loop.srs_rules import srs_builds

STABLE = "stable"
ACTION = "action"  # canvas_claims.delivery: the claim is delivered by a task, not by code

# The problem or opportunity a work item goes from


def _problem(spec: WorkSpec, view: View) -> int | None:
    """The problem of the work item ``spec`` comes of when it has one, else the one its goal answers now."""
    if spec.parent_id is not None:
        parent = next((item for item in view.items if item.id == spec.parent_id), None)
        if parent is not None and parent.problem_id is not None:
            return parent.problem_id
    goal = view.goals.get(spec.goal_id) if spec.goal_id is not None else None
    return None if goal is None else goal.problem_id


def _opportunity(spec: WorkSpec, view: View) -> int | None:
    """The opportunity of the work item ``spec`` comes of when it has one, else the one its goal answers now."""
    if spec.parent_id is not None:
        parent = next((item for item in view.items if item.id == spec.parent_id), None)
        if parent is not None and parent.opportunity_id is not None:
            return parent.opportunity_id
    goal = view.goals.get(spec.goal_id) if spec.goal_id is not None else None
    return None if goal is None else goal.opportunity_id


def _stamped(spec: WorkSpec, view: View) -> WorkSpec:
    if spec.problem_id is None:
        problem = _problem(spec, view)
        spec = spec if problem is None else replace(spec, problem_id=problem)
    if spec.opportunity_id is None:
        opportunity = _opportunity(spec, view)
        spec = spec if opportunity is None else replace(spec, opportunity_id=opportunity)
    return spec


def stamp(effect: Effect, view: View) -> Effect:
    """``effect`` with the problem or opportunity its work items go from: the one it makes, or those a transition
    spawns."""
    if isinstance(effect, WorkSpec):
        return _stamped(effect, view)
    if isinstance(effect, Transition) and effect.spawn:
        return replace(effect, spawn=tuple(_stamped(spec, view) for spec in effect.spawn))
    return effect


# What an event makes


def _testing(event: Event, view: View, moved: dict, kind: str, key: str, owner: str | None = None) -> list[Effect]:
    """A work item of ``kind`` for a claim a baseline moved to testing, when it serves an active goal with a metric;
    in watching until the baseline has been approved ``loop.stable_hours``."""
    payload = event.payload
    goal = view.goal_of_canvas(payload["canvas_id"])
    if goal is None:
        return []
    stable_at = event.created_at + timedelta(hours=float(view.policy.get("stable_hours") or 0))
    waits = view.now < stable_at
    detail = {"baseline": payload["baseline"], "from": moved.get("from"), "claim": moved["claim"]}
    detail["canvas"] = payload["canvas"]
    if waits:
        detail["stable_at"] = stable_at.isoformat()
    state, reason = (WATCHING, STABLE) if waits else (QUEUED, None)
    return [WorkSpec(kind, key, goal.id, state, reason, claim_id=moved["claim_id"], owner=owner, detail=detail)]


def _in_testing(event: Event, view: View, moved: dict) -> list[Effect]:
    """A claim moved to testing, by its cell: a use case or a build of a solution, a discover of a problem."""
    claim = view.claims.get(moved["claim_id"])
    cell = None if claim is None else claim.cell
    at = f"{event.payload['canvas']}:{moved['claim']}:b{event.payload['baseline']}"
    if cell in SOLUTION_CELLS and claim is not None and claim.delivery == ACTION:
        return _testing(event, view, moved, ACT, f"{ACT}:{at}")
    if cell in SOLUTION_CELLS and view.use_cases:
        return use_case_work(event, view, moved)
    if cell in SOLUTION_CELLS:
        return _testing(event, view, moved, BUILD, CLAIM_BUILD + at)
    if cell == PROBLEM_CELL:
        return _testing(event, view, moved, DISCOVER, f"{DISCOVER}:{at}", STRATEGIST)
    return []


def _baseline(event: Event, view: View) -> list[Effect]:
    effects: list[Effect] = []
    for moved in event.payload.get("transitions") or []:
        if moved.get("to") == TESTING:
            effects.extend(_in_testing(event, view, moved))
        elif moved.get("to") in NOTED_RESULTS:
            effects.extend(noted(event, view, moved))
    return effects


# Where a work item moves


def _stable(item: ItemView, view: View) -> Transition | None:
    """A work item past the stable hours of its baseline, queued."""
    stable_at = item.detail.get("stable_at")
    if stable_at is None or view.now >= datetime.fromisoformat(stable_at):
        return Transition(item.id, QUEUED, READY)
    return None


def _built(item: ItemView, view: View) -> Transition | None:
    """A build made straight from a claim waits out its stable hours; then, as every build, its author run writes a
    plan and a deliver comes of it (rules/1, rows 9 to 11)."""
    if item.from_claim and item.state == WATCHING:
        return _stable(item, view) if item.reason == STABLE else None
    return move_build(item, view)


def _discovered(item: ItemView, view: View) -> Transition | None:
    """A discover past its stable hours; one whose task run is done, done with the proposals it made."""
    if item.state == WATCHING and item.reason == STABLE:
        return _stable(item, view)
    run = run_of(item, view)
    if item.state == RUNNING and run is not None and run.state == "done":
        return Transition(item.id, DONE, DONE_EVENT, detail={"run_id": run.id, "proposals": run.proposals})
    return None


def _acted(item: ItemView, view: View) -> Transition | None:
    """An act past its stable hours; one running with its task of the plugin tasks (``act.hand_out``): done once the
    task is done, a failure (as for any kind: queued again, then triage, then a person) when it is cancelled or gone or
    past its due day."""
    if item.state == WATCHING and item.reason == STABLE:
        return _stable(item, view)
    if item.state != RUNNING or item.task_id is None:
        return None
    task = view.tasks.get(item.task_id)
    if task is None:
        return _failure(item, why="the task of the act is gone")
    if task.group == DONE_GROUP:
        return Transition(item.id, DONE, DONE_EVENT, detail={"task": task.number})
    if task.group == CANCELLED_GROUP:
        return _failure(item, task=task.number, why="the task of the act was cancelled")
    if task.due_on is not None and view.now.date() > task.due_on:
        return _failure(
            item, task=task.number, why=f"the task of the act is past its due day {task.due_on.isoformat()}"
        )
    return None


MOVES = {BUILD: _built, DISCOVER: _discovered, ACT: _acted}


class Rules2(Rules):
    """The engine of this release (``model.ENGINE``)."""

    name = ENGINE

    def rules(self, event: Event, view: View) -> list[Effect]:
        if event.kind == BASELINE:
            found = _baseline(event, view)
        elif event.kind == SRS_APPROVED:
            found = approved(event, view)
        else:
            found = []
        return [stamp(effect, view) for effect in found]

    def scheduled(self, view: View) -> list[Effect]:
        return [stamp(effect, view) for effect in [*super().scheduled(view), *srs_builds(view)]]

    def reconcile(self, item: ItemView, view: View) -> Transition | None:
        found = super().reconcile(item, view)
        return None if found is None else stamp(found, view)

    def move(self, item: ItemView, view: View) -> Transition | None:
        own = MOVES.get(item.kind)
        return own(item, view) if own is not None else super().move(item, view)


RULES = Rules2()

__all__ = ["RULES", "Rules2", "stamp"]
