"""Plans as the hub keeps them: what a plan body may hold, the one-item update of ``evo harness step``, completion,
and the summary a revision carries. Pure functions over JSON values, shared by the server, which applies them under
a row lock, and by the client, which checks a retry with them.

A plan body is the plan file as ``evo_agents.harness.load_yaml`` reads it, without the hub key. Its digest is
``evo_agents.harness.plan_digest``. The hub stores it as JSON, so a body must be JSON all the way down: objects with
string keys, arrays, strings without NUL, finite numbers, booleans and null.

``update_item`` is ``_mutate.update_item`` of evo-cli on data instead of text: the item at ``index`` of ``section``
must be a mapping, every key of ``updates`` is set on it, and the result must still be a valid plan. Only the keys
evo-cli writes may be set: the status, its date (``done_at``, ``fixed_at``, ``answered_at``, ``merged_at``),
``note``, and ``evidence``, which ``evo harness step --evidence`` adds. Standard library only.
"""

from __future__ import annotations

import math

from evo_agents.kg.protocol import canonical_json

SECTIONS = ("steps", "repos", "tech_debt", "open_questions")  # what evo harness step, repo, debt, question set
UPDATABLE = ("status", "done_at", "fixed_at", "answered_at", "merged_at", "note", "evidence")
STEP_STATUSES = ("done", "in_progress", "pending", "blocked")
AREAS = ("active", "completed")
MAX_PLAN_BYTES = 1024 * 1024  # canonical JSON of one plan; the largest plan of the three harnesses is 0.12 MiB
MAX_VALUE_CHARS = 64 * 1024  # one value set by an update
MAX_DEPTH = 32
SUMMARY_PATHS = 8  # changed paths named in a revision summary before "and N more"


class PlanProblem(ValueError):
    """A body, an update or a completion the hub refuses; ``str`` says why for the person who sent it."""


class PlanTooLarge(PlanProblem):
    pass


def check_json(value, where: str = "", depth: int = 0) -> None:
    """Raise PlanProblem naming the first value Postgres or another reader of the plan could not keep as is."""
    if depth > MAX_DEPTH:
        raise PlanProblem(f"{where or 'the plan'} is nested more than {MAX_DEPTH} levels deep")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise PlanProblem(f"{where or 'the plan'} has a key that is not a string: {key!r}")
            if "\x00" in key:
                raise PlanProblem(f"{where or 'the plan'} has a key holding a NUL character")
            check_json(item, f"{where}.{key}" if where else key, depth + 1)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            check_json(item, f"{where}[{index}]", depth + 1)
    elif isinstance(value, str):
        if "\x00" in value:
            raise PlanProblem(f"{where} holds a NUL character")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise PlanProblem(f"{where} is {value!r}, which JSON cannot hold: write it as a string")
    elif value is not None and not isinstance(value, (bool, int)):
        raise PlanProblem(f"{where} is a {type(value).__name__}, which JSON cannot hold: write it as a string")


def check_body(body, plan_id: str | None = None) -> None:
    """A plan body the hub can take: a JSON object whose id is ``plan_id`` (when given), at most MAX_PLAN_BYTES."""
    if not isinstance(body, dict):
        raise PlanProblem("a plan must be a mapping")
    check_json(body)
    if plan_id is not None and body.get("id") != plan_id:
        raise PlanProblem(f"the plan's id is {body.get('id')!r}, not {plan_id!r}: a plan is stored under its own id")
    size = len(canonical_json(body))
    if size > MAX_PLAN_BYTES:
        raise PlanTooLarge(f"the plan is {size} bytes as JSON, more than the {MAX_PLAN_BYTES} the hub keeps")


def step_key(step, index: int) -> str:
    """How plans name a step, as evo-cli's ``step_key``: its id, else its order, else its position."""
    if isinstance(step, dict):
        for name in ("id", "order"):
            if step.get(name) is not None:
                return str(step[name])
    return str(index)


def step_index(body: dict, key) -> int:
    """The position of the step ``key`` names (compared as text, so 5 and "5" are the same step)."""
    steps = body.get("steps")
    steps = steps if isinstance(steps, list) else []
    wanted = str(key)
    for index, step in enumerate(steps):
        if step_key(step, index) == wanted:
            return index
    known = ", ".join(step_key(step, index) for index, step in enumerate(steps)) or "none"
    raise PlanProblem(f"no step {wanted!r} in plan {body.get('id')}; its steps are {known}")


def update_item(body: dict, section: str, index: int, updates: dict) -> tuple[dict, dict]:
    """The plan with ``updates`` set on item ``index`` of ``section``, and the item's old values of those keys
    (None for a key it lacked). ``body`` is not changed."""
    if section not in SECTIONS:
        raise PlanProblem(f"section must be one of {', '.join(SECTIONS)}, not {section!r}")
    if not updates:
        raise PlanProblem("nothing to set: give at least one key")
    for key, value in updates.items():
        if key not in UPDATABLE:
            raise PlanProblem(f"{key!r} cannot be set on one item; the keys that can are {', '.join(UPDATABLE)}")
        if not isinstance(value, str):
            raise PlanProblem(f"{key} must be text")
        if len(value) > MAX_VALUE_CHARS:
            raise PlanProblem(f"{key} is longer than {MAX_VALUE_CHARS} characters")
        check_json(value, key)
    if section == "steps" and "status" in updates and updates["status"] not in STEP_STATUSES:
        raise PlanProblem(f"a step's status is one of {', '.join(STEP_STATUSES)}, not {updates['status']!r}")
    items = body.get(section)
    if not isinstance(items, list):
        raise PlanProblem(f"this plan has no {section!r} section")
    if not 0 <= index < len(items):
        raise PlanProblem(f"section {section!r} holds {len(items)} items, so index {index} does not exist")
    item = items[index]
    if not isinstance(item, dict):
        raise PlanProblem(
            f"{section}[{index}] is a bare string, not a mapping, so it has no status to set. "
            "Give it `what:` and `status:` keys first"
        )
    old = {key: item.get(key) for key in updates}
    result = dict(body)
    result[section] = [{**item, **updates} if position == index else entry for position, entry in enumerate(items)]
    return result, old


def item_label(body: dict, section: str, index: int) -> str:
    """An item as people name it: a step by its key, any other item by its position."""
    if section == "steps":
        return f"step {step_key(body['steps'][index], index)}"
    return f"{section}[{index}]"


def update_summary(body: dict, section: str, index: int, old: dict, updates: dict) -> str:
    """The summary of a revision made by ``update_item``: the item, a status change, the other keys set."""
    parts = []
    if "status" in updates and old.get("status") != updates["status"]:
        parts.append(f"status {old.get('status') or '-'} -> {updates['status']}")
    others = [key for key in updates if key != "status" and old.get(key) != updates[key]]
    if others:
        parts.append(f"set {', '.join(others)}")
    return f"{item_label(body, section, index)}: {'; '.join(parts) or 'no change'}"


def undone_steps(body: dict) -> list[str]:
    """The steps that are not done, by ``step_key``: complete refuses a plan that has any."""
    steps = body.get("steps")
    steps = steps if isinstance(steps, list) else []
    return [
        step_key(step, index)
        for index, step in enumerate(steps)
        if not (isinstance(step, dict) and step.get("status") == "done")
    ]


def _changed_paths(old, new, where: str) -> list[str]:
    """Where ``new`` differs from ``old``, down to an item of a top-level section or a key of an item."""
    if old == new:
        return []
    depth = where.count(".") + where.count("[")
    if isinstance(old, dict) and isinstance(new, dict) and depth < 2:
        paths = []
        for key in [*old, *(k for k in new if k not in old)]:
            child = f"{where}.{key}" if where else key
            if key not in new:
                paths.append(f"-{child}")
            elif key not in old:
                paths.append(f"+{child}")
            else:
                paths += _changed_paths(old[key], new[key], child)
        return paths
    if isinstance(old, list) and isinstance(new, list) and depth < 2:
        paths = []
        for index in range(max(len(old), len(new))):
            child = f"{where}[{index}]"
            if index >= len(new):
                paths.append(f"-{child}")
            elif index >= len(old):
                paths.append(f"+{child}")
            else:
                paths += _changed_paths(old[index], new[index], child)
        return paths
    return [where]


def summarize(old: dict | None, new: dict) -> str:
    """One line naming what a revision changed (paths only, never the values): ``+`` added, ``-`` removed."""
    if old is None:
        return "created"
    paths = _changed_paths(old, new, "")
    if not paths:
        return "no change to the body"
    shown = ", ".join(paths[:SUMMARY_PATHS])
    more = f" and {len(paths) - SUMMARY_PATHS} more" if len(paths) > SUMMARY_PATHS else ""
    return f"changed {shown}{more}"
