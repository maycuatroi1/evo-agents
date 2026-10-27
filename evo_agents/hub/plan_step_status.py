"""The event ``plan.step``: a step's status of a plan set or changed on the hub, whatever wrote it.

Every write of a plan goes through one function (``evo_agents.hub.server.plans._store``): a member's PATCH of a step
(``evo-agents hub plan step``, the MCP tool ``plan_step``), the hub's write of a run's step (``evo-agents worker
step``, ``evo_agents.hub.server.run_state``), a PUT that creates or replaces the plan (an import, an edit, an author
run's plan), and complete with ``mark_steps_done``. Once the revision is written, ``emit`` compares the steps of the
revision before with those of the new one, and emits the event once for each step whose status differs, in the order
of the steps, in the transaction that wrote the plan: for the plugins granted ``plans:read`` and into hub_events
(``evo_agents.hub.plugin_events``), where the Loop reads it by cursor and the wait of a plan run wakes on it
(``evo-agents worker wait --on plan.step``, ``evo_agents.hub.server.run_waits``). So a run that waits on another plan's
step wakes once that step is done, not when a timer runs out.

A step is named as plans name it (``plans.step_key``: its id, else its order, else its position), which is what
``--step`` takes. A step new in the revision (a plan created, a step added) had no status before (``from`` None); a
step the revision drops emits nothing, and neither does a step whose status the write left alone.

A payload carries ids and states, never a step's text: ``plan`` and ``plan_id`` the plan's id, ``step`` the step,
``status`` and ``to`` its new status, ``from`` the one before, ``revision`` the plan's new revision. ``plan_id``,
``from`` and ``to`` are what plugins read since the event first went to them; ``plan`` and ``status`` are what a wait
names (``--contains plan=SLUG --contains step=8 --contains status=done``). ``v`` is the version of its shape
(``hub_events.VERSION``).

Nothing here imports ``evo_agents.hub.server``.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import hub_events, plugin_events
from evo_agents.hub.plans import step_key

KIND = hub_events.PLAN_STEP


def _steps(body: dict | None) -> list:
    steps = None if body is None else body.get("steps")
    return steps if isinstance(steps, list) else []


def _status(step):
    return step.get("status") if isinstance(step, dict) else None


def changes(before: dict | None, after: dict) -> list[tuple[str, str | None, str | None]]:
    """(step, status before, status after) of each step of plan body ``after`` whose status differs from its status
    in ``before`` (None for a plan this write creates), in the order of the steps of ``after``."""
    held = {step_key(step, index): _status(step) for index, step in enumerate(_steps(before))}
    found = []
    for index, step in enumerate(_steps(after)):
        key = step_key(step, index)
        old, new = held.get(key), _status(step)
        if old != new:
            found.append((key, old, new))
    return found


def payload(plan_id: str, revision: int, step: str, old: str | None, new: str | None) -> dict:
    """The payload of ``plan.step``: step ``step`` of plan ``plan_id`` went from ``old`` to ``new`` in ``revision``."""
    return {
        "v": hub_events.VERSION,
        "plan": plan_id,
        "plan_id": plan_id,
        "step": step,
        "status": new,
        "from": old,
        "to": new,
        "revision": revision,
    }


async def emit(
    conn: AsyncConnection, project_id: int, plan_id: str, revision: int, before: dict | None, after: dict
) -> int:
    """Emit ``plan.step`` in project ``project_id`` for each step whose status revision ``revision`` of plan
    ``plan_id`` changed, ``before`` the body it replaced (None for a plan it creates) and ``after`` its own, in the
    caller's transaction; how many steps changed."""
    found = changes(before, after)
    for step, old, new in found:
        await plugin_events.emit(conn, KIND, project_id, payload(plan_id, revision, step, old, new))
    return len(found)


__all__ = ["KIND", "changes", "emit", "payload"]
