"""The rules a core plugin adds to the Loop (extension point ``loop_rules``, version 1; plan autonomous-oss-growth, step
15, criterion a12).

A core plugin's manifest contributes ``loop_rules``: each names a Python handler, the events it reads (events of the hub
or ``plugin.<id>.<name>`` of the plugin's own, ``hub_events.own_kind``), whether it also reads the clock, and the kinds
of work items it makes (``model.PLUGIN_KINDS``), each with how its work gets done: ``task`` (a task run of the plugin's
own agent type, through ``task_dispatch.hand_own_task`` like the core kinds') or ``job`` (the plugin's own code does
it: the work item runs with no run of the hub, and the plugin moves it).

The handler is ``handler(params, context)``, awaited, in the tick's transaction but with no connection of it: ``params``
is ``{project, project_id, rule, event, now, items, goals}``, ``event`` the event read (None for the clock), ``items``
the plugin's own work items not finished (id, kind, key, state, reason, detail, failures, attempts, run_id, goal_id,
claim_id, parent_id). It returns ``{"effects": [...]}``, each a dict of ``effect``:

- ``work``: ``kind``, ``key`` and ``detail`` (a ``task`` and a ``title`` for a task run), with the ancestors it comes of,
  ``goal_id``, ``claim_id``, ``parent_id``, ``use_case``, ``task_id`` (each must exist in the project); a ``WorkSpec``,
  which carries the problem its goal answers as a core work item does (plan problem-oriented-hub);
- ``move``: ``item_id`` of one of the plugin's own work items, ``state`` (queued, watching, waiting_human, done or
  cancelled), its ``reason`` of that state (``model.REASONS``), ``detail``, and ``failed`` to count a failure when it
  moves back to queued; a ``Transition``;
- ``note``: ``claim_id``, ``key`` and ``detail``; a ``Note``.

Nothing else: the hub writes no row a handler names, and an effect that does not read, names a kind the rule did not
declare or touches a work item of another plugin is left out with a log line. A work item made goes through the same path
as a core one: its key is ``<plugin>:<key>``, so the same delta read twice makes nothing more; admission checks the
breaker, the concurrency cap of its cycle (business), the rate of hand-outs and, for a task, the daily cap of the agent
type; stalls and triage count its failures. A handler that raises or takes more than HANDLER_TIMEOUT seconds makes
nothing for that call. The core table of rules is untouched: a plugin's rule runs beside it, never in its place.

A plugin turned off in a project (or one whose manifest no longer declares a work item's kind) makes no work there, and
its work items not finished are cancelled with reason ``plugin_off`` (``RuleSet.reconcile``).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import plugin_calls
from evo_agents.hub.plugin_calls import CoreContext
from evo_agents.hub.server.loop.admit import TYPE_REFUSED, Handed
from evo_agents.hub.server.loop.model import (
    CANCELLED,
    CANCELLED_EVENT,
    DONE,
    DONE_EVENT,
    FAILED,
    PLUGIN_ENGINE,
    PLUGIN_KINDS,
    PLUGIN_OFF,
    QUEUED,
    READY,
    REASONS,
    RUNNING,
    WAITING,
    WAITING_HUMAN,
    WATCHING,
    Effect,
    Event,
    ItemView,
    Note,
    Transition,
    View,
    WorkSpec,
)
from evo_agents.hub.server.loop.rules import _asks, _cancel, _run_end, _triaged, run_of

log = logging.getLogger(__name__)

POINT = "loop_rules"
HANDLER_TIMEOUT = 20  # seconds a handler may take
MAX_EFFECTS = 50  # effects of one call the hub takes
MAX_KEY_CHARS = 400  # a work item's key before the plugin's id is put in front of it
MAX_DETAIL_BYTES = 16 * 1024
MOVE_STATES = {QUEUED: READY, WATCHING: WAITING, WAITING_HUMAN: WAITING, DONE: DONE_EVENT, CANCELLED: CANCELLED_EVENT}
TASK, JOB = "task", "job"


@dataclass(frozen=True)
class Rule:
    """A rule of a core plugin enabled in the project."""

    plugin: str
    project: str
    name: str
    handler: Callable
    on: frozenset[str]
    schedule: bool
    kinds: Mapping[str, Mapping]  # kind -> {"run": task|job, "agent_type": ...}

    @property
    def engine(self) -> str:
        return PLUGIN_ENGINE.format(self.plugin)


@dataclass(frozen=True)
class Made:
    """An effect of a rule with the engine name it is written under."""

    effect: Effect
    engine: str


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


def _own(plugin: str, item: ItemView) -> bool:
    return item.kind in PLUGIN_KINDS and item.detail.get("plugin") == plugin


def _shown(item: ItemView) -> dict:
    return {
        "id": item.id,
        "kind": item.kind,
        "key": item.key,
        "state": item.state,
        "reason": item.reason,
        "detail": dict(item.detail),
        "failures": item.failures,
        "attempts": item.attempts,
        "run_id": item.run_id,
        "goal_id": item.goal_id,
        "claim_id": item.claim_id,
        "parent_id": item.parent_id,
    }


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _detail(raw: Any) -> dict | None:
    """``raw`` as a detail object the hub keeps; None when it is not one."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        return None
    try:
        text = json.dumps(raw)
    except (TypeError, ValueError):
        return None
    return raw if len(text.encode()) <= MAX_DETAIL_BYTES else None


def _work(rule: Rule, raw: dict, view: View) -> WorkSpec | str:
    kind, key = raw.get("kind"), raw.get("key")
    if kind not in rule.kinds:
        return f"kind {kind!r} is not one the rule declares"
    if not isinstance(key, str) or not 1 <= len(key) <= MAX_KEY_CHARS:
        return f"a key of 1 to {MAX_KEY_CHARS} characters is required"
    detail = _detail(raw.get("detail"))
    if detail is None:
        return "detail is not a JSON object of a bounded size"
    ancestors: dict[str, int | None] = {}
    known = {
        "goal_id": view.goals,
        "claim_id": view.claims,
        "task_id": view.tasks,
        "parent_id": {item.id for item in view.items},
    }
    for name, within in known.items():
        value = raw.get(name)
        if value is not None and (_int(value) is None or value not in within):
            return f"{name} {value!r} is not one of the project's"
        ancestors[name] = value
    use_case = raw.get("use_case")
    if use_case is not None and not isinstance(use_case, str):
        return "use_case is a string"
    detail = {**detail, "plugin": rule.plugin, "rule": rule.name}
    goal = view.goals.get(ancestors["goal_id"]) if ancestors["goal_id"] is not None else None
    problem = None if goal is None else goal.problem_id  # the problem its goal answers, as a core work item's
    spec = WorkSpec(kind=kind, key=f"{rule.plugin}:{key}", use_case=use_case, detail=detail, **ancestors)
    opportunity = None if goal is None else goal.opportunity_id
    if opportunity is not None:
        spec = replace(spec, opportunity_id=opportunity)
    return spec if problem is None else replace(spec, problem_id=problem)


def _move(rule: Rule, raw: dict, view: View) -> Transition | str:
    item = next((item for item in view.items if item.id == raw.get("item_id")), None)
    if item is None or not _own(rule.plugin, item) or item.finished:
        return f"work item {raw.get('item_id')!r} is not one of the plugin's, still open"
    state = raw.get("state")
    if state not in MOVE_STATES:
        return f"state {state!r} is not one a plugin moves a work item to"
    reason = raw.get("reason")
    if reason is None and REASONS[state] and state != QUEUED:
        return f"state {state} has a reason of {REASONS[state]}"
    if reason is not None and reason not in REASONS[state]:
        return f"reason {reason!r} is not one of state {state}: {REASONS[state]}"
    detail = _detail(raw.get("detail"))
    if detail is None:
        return "detail is not a JSON object of a bounded size"
    failed = raw.get("failed") is True
    if failed and state != QUEUED:
        return "a failure moves a work item back to queued"
    return Transition(item.id, state, FAILED if failed else MOVE_STATES[state], reason, detail, failed=failed)


def _note(rule: Rule, raw: dict, view: View) -> Note | str:
    claim, key = raw.get("claim_id"), raw.get("key")
    detail = _detail(raw.get("detail"))
    if claim not in view.claims or not isinstance(key, str) or not key or detail is None:
        return "a note names a claim of the project, a key and a detail object"
    return Note(claim, f"{rule.plugin}:{_cut(key, MAX_KEY_CHARS)}", detail)


def effects_of(rule: Rule, answer: Any, view: View) -> list[Effect]:
    """The effects ``rule``'s handler answered that the hub takes; the others are logged and left out."""
    raws = answer.get("effects") if isinstance(answer, dict) else None
    if not isinstance(raws, list):
        log.warning("loop rule answered no effects", extra={"plugin": rule.plugin, "rule": rule.name})
        return []
    found: list[Effect] = []
    for raw in raws[:MAX_EFFECTS]:
        kind = raw.get("effect") if isinstance(raw, dict) else None
        make = {"work": _work, "move": _move, "note": _note}.get(kind)
        made = make(rule, raw, view) if make is not None else f"effect {kind!r} is not work, move or note"
        if isinstance(made, str):
            log.warning("loop rule effect left out", extra={"plugin": rule.plugin, "rule": rule.name, "why": made})
        else:
            found.append(made)
    return found


class RuleSet:
    """The rules of the core plugins a project enables, for one step of a tick."""

    def __init__(self, rules: tuple[Rule, ...] = (), engine: Any = None, project_id: int | None = None):
        self.rules = rules
        self.engine = engine
        self.project_id = project_id

    def __bool__(self) -> bool:
        return bool(self.rules)

    def declares(self, plugin: Any, kind: str) -> bool:
        return any(rule.plugin == plugin and kind in rule.kinds for rule in self.rules)

    def _params(self, rule: Rule, event: Event | None, view: View) -> dict:
        return {
            "project": rule.project,
            "project_id": self.project_id,
            "rule": rule.name,
            "event": None
            if event is None
            else {
                "id": event.id,
                "kind": event.kind,
                "payload": event.payload,
                "created_at": event.created_at.isoformat(),
            },
            "now": view.now.isoformat(),
            "items": [_shown(item) for item in view.items if _own(rule.plugin, item) and not item.finished],
            "goals": [{"id": g.id, "code": g.code, "status": g.status} for g in view.goals.values()],
        }

    async def _call(self, rule: Rule, event: Event | None, view: View) -> list[Made]:
        context = CoreContext(rule.plugin, self.engine, None, rule.project, self.project_id)
        try:
            answer = rule.handler(self._params(rule, event, view), context)
            if inspect.isawaitable(answer):
                answer = await asyncio.wait_for(answer, HANDLER_TIMEOUT)
        except Exception:  # a core plugin's own code may raise anything; it makes nothing, the tick goes on
            log.exception("loop rule failed", extra={"plugin": rule.plugin, "rule": rule.name})
            return []
        return [Made(effect, rule.engine) for effect in effects_of(rule, answer, view)]

    async def rules_for(self, event: Event, view: View) -> list[Made]:
        """What the plugins' rules make of ``event``, in the order of the rules."""
        found: list[Made] = []
        for rule in self.rules:
            if event.kind in rule.on:
                found += await self._call(rule, event, view)
        return found

    async def scheduled(self, view: View) -> list[Made]:
        """What the plugins' rules that read the clock make now."""
        found: list[Made] = []
        for rule in self.rules:
            if rule.schedule:
                found += await self._call(rule, None, view)
        return found

    def reconcile(self, item: ItemView, view: View) -> Transition | None:
        """Where work item ``item`` of a plugin's kind moves now: cancelled (``plugin_off``) when no enabled plugin
        declares its kind; a run that ended done finishes it, one that ended otherwise is a failure of it, as for a
        core kind; its agent's question waits for a person; triage ends as for any work item. Otherwise it stays: the
        plugin's own rules move it."""
        if item.finished:
            return None
        plugin = item.detail.get("plugin")
        if not self.declares(plugin, item.kind):
            return _cancel(item, PLUGIN_OFF, plugin=plugin)
        if item.state == RUNNING or item.state == WAITING_HUMAN:
            run = run_of(item, view)
            if run is not None and run.ended and run.state == "done":
                return Transition(item.id, DONE, DONE_EVENT, detail={"run_id": run.id})
            ended = _run_end(item, view)
            if ended is not None:
                return ended
            if run is not None and (moved := _asks(item, run, "run_decision")) is not None:
                return moved
        if item.state == WATCHING and item.reason == "triage":
            return _triaged(item, view)
        return None  # a waiting_human work item of loop_decision waits for the person's answer (``stall.answer``)


async def load_rules(conn: AsyncConnection, project_id: int, engine: Any = None) -> RuleSet:
    """The rules of the core plugins project ``project_id`` enables, as their manifests declare them."""
    found: list[Rule] = []
    for row in await plugin_calls.enabled_plugins(conn, project_id):
        if row.tier != "core":
            continue
        for item in (row.manifest.get("contributes") or {}).get(POINT) or []:
            try:
                handler = plugin_calls.load(item["python"])
            except Exception:  # a plugin whose handler does not load makes no work, the tick goes on
                log.exception("loop rule does not load", extra={"plugin": row.name, "rule": item.get("name")})
                continue
            kinds = {entry["kind"]: entry for entry in item.get("kinds") or [] if entry.get("kind") in PLUGIN_KINDS}
            found.append(
                Rule(
                    plugin=row.name,
                    project=row.project,
                    name=item["name"],
                    handler=handler,
                    on=frozenset(item.get("on") or ()),
                    schedule=item.get("schedule") is True,
                    kinds=kinds,
                )
            )
    return RuleSet(tuple(found), engine, project_id)


async def hand_out(conn: AsyncConnection, project_id: int, item: ItemView) -> Handed:
    """The run of work item ``item`` of a plugin's kind, which passed admission: a task run of the plugin's own agent
    type for run kind task (its text the ``task`` of the work item's detail), none for run kind job (the work item runs
    and the plugin moves it), ``type_refused`` with why when the plugin is off or the agent type refuses."""
    # Imported here: task_dispatch's own imports go round a cycle when it is the first module of the server a process
    # loads (``loop.caps`` says the same).
    from evo_agents.hub.server.run_state import write_event
    from evo_agents.hub.server.task_dispatch import hand_own_task

    plugin = item.detail.get("plugin")
    enabled = await plugin_calls.enabled_in(conn, plugin, project_id) if isinstance(plugin, str) else None
    declared = (enabled.manifest.get("contributes") or {}).get(POINT) or [] if enabled is not None else []
    specs = [entry for rule in declared for entry in rule.get("kinds") or [] if entry.get("kind") == item.kind]
    spec = specs[0] if specs else None
    if spec is None:
        return Handed(
            reason=TYPE_REFUSED, why=f"plugin {plugin} is off in the project or declares no rule for {item.kind}"
        )
    if spec.get("run") != TASK:
        return Handed(started=True)
    task = item.detail.get("task")
    title = item.detail.get("title")
    text = task if isinstance(task, str) and task else f"Work item #{item.id} of the Loop ({item.kind}, {item.key})."
    run_id, why = await hand_own_task(
        conn, plugin, project_id, spec["agent_type"], text, title if isinstance(title, str) else None
    )
    if run_id is None:
        return Handed(reason=TYPE_REFUSED, why=why)
    note = f"Queued by the Loop of project {enabled.project} for work item #{item.id} ({item.key})."
    await write_event(conn, run_id, {"text": note, "loop_item": item.id})
    return Handed(run_id=run_id)


__all__ = ["POINT", "Made", "Rule", "RuleSet", "effects_of", "hand_out", "load_rules"]
