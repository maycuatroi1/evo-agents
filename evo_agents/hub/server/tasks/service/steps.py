"""What the hub's ``plan.step`` event does to the tasks of a project linked to the step (``steps_done``), which the
plugin's events contribution ``steps`` hands over (``events``).

When a step turns done, every task of the project linked to it gets a row in its history: the hub (no actor) saw plan
X step N go from its old status to done. When the config sets ``on_steps_done`` and every plan step a task links is
done now, the task moves to that status, unless it is finished already (a status of group done or cancelled); the
row of that move names the step that was the last, and the move emits ``tasks.status`` (cause ``steps_done``, the hub
its actor; ``evo_agents.hub.task_status``). The plan is never changed.

The hub delivers an event at least once, with the same delivery id on every try: a row written for a delivery names
it, and a task that has its row already is passed over, so a second try writes nothing.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import task_status
from evo_agents.hub.server.tasks.models.rows import DONE
from evo_agents.hub.server.tasks.models.values import Settings, TasksError
from evo_agents.hub.server.tasks.repository import history
from evo_agents.hub.server.tasks.repository import links as link_rows
from evo_agents.hub.server.tasks.repository import tasks as task_rows
from evo_agents.hub.server.tasks.service.links import step_states
from evo_agents.hub.server.tasks.service.project import settings

STEP = "plan_step"  # the field of a step's row in a task's history


async def _all_done(conn: AsyncConnection, project_id: int, task_id: int) -> bool:
    pairs = {(row.plan_id, row.step_id) for row in await link_rows.step_links(conn, [task_id])}
    states = await step_states(conn, project_id, pairs, None, None)
    return bool(pairs) and all(state.status == DONE for state in states.values())


async def _move(
    conn: AsyncConnection, project_id: int, row, config: Settings, step: dict, cause: str, delivery: int | None
) -> None:
    target = config.on_steps_done
    if target is None or row.status == target or config.finished(row.status):
        return
    if await history.record_delivered(conn, row.id, "status", row.status, target, cause, delivery):
        await task_rows.update_task(conn, row.id, {"status": target})
        groups = config.group(row.status), config.group(target)
        moved = task_status.StatusChange(
            row.id, row.status, target, *groups, task_status.STEPS_DONE, config.prefix, source=step
        )
        await task_status.emit(conn, project_id, moved)


async def steps_done(conn: AsyncConnection, project_id: int, project: str, payload: dict, delivery: int | None) -> int:
    """What a ``plan.step`` event of project ``project_id`` does to the tasks linked to its step; how many tasks got a
    row of it."""
    plan_id, step = payload.get("plan_id"), payload.get("step")
    if payload.get("to") != DONE or not plan_id or step is None:
        return 0
    try:
        config: Settings | None = await settings(conn, project_id, project)
    except TasksError:
        config = None  # a config the hub cannot use moves no task; the rows are written all the same
    cause = f"plan {plan_id} step {step} is done"
    written = 0
    for row in await task_rows.linked_to_step(conn, project_id, plan_id, str(step)):
        if not await history.record_delivered(conn, row.id, STEP, payload.get("from"), DONE, cause, delivery):
            continue
        written += 1
        if config is not None and config.on_steps_done and await _all_done(conn, project_id, row.id):
            last = f"every plan step it links is done, plan {plan_id} step {step} last"
            await _move(conn, project_id, row, config, {"plan_id": plan_id, "step": str(step)}, last, delivery)
    return written


__all__ = ["STEP", "steps_done"]
