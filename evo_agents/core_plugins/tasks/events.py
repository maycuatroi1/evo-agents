"""The plugin's events contribution ``steps``: the hub's ``plan.step`` event of a project that enabled the plugin and
granted it ``plans:read`` (``evo_agents.hub.plugin_events``).

When a step turns done, every task of the project linked to it gets a row in its history: the hub (no actor) saw plan
X step N go from its old status to done. When the config sets ``on_steps_done`` and every plan step a task links is
done now, the task moves to that status, unless it is finished already (a status of group done or cancelled); the
row of that move names the step that was the last. The plan is never changed.

The hub delivers an event at least once, with the same delivery id on every try: a row written for a delivery names
it, and a task that has its row already is passed over, so a second try writes nothing.
"""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.tasks.links import DONE, step_states
from evo_agents.core_plugins.tasks.settings import Settings, TasksError, settings
from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import CoreContext

STEP = "plan_step"  # the field of a step's row in a task's history


async def _linked(conn: AsyncConnection, project_id: int, plan_id: str, step: str) -> list:
    t, k = tables.tasks.items, tables.tasks.links
    query = (
        select(t.c.id, t.c.number, t.c.status)
        .join_from(k, t, t.c.id == k.c.task_id)
        .where(t.c.project_id == project_id, k.c.kind == "plan_step", k.c.plan_id == plan_id, k.c.step_id == step)
        .order_by(t.c.number)
        .distinct()
    )
    return list(await conn.execute(query))


async def _written(conn: AsyncConnection, task_id: int, name: str, old, new, cause: str, delivery: int | None) -> bool:
    """Write a row of the hub into the history of task ``task_id``; False when this delivery wrote it already."""
    e = tables.tasks.events
    values = {"task_id": task_id, "field": name, "from_value": old, "to_value": new, "cause": cause}
    statement = insert(e).values(**values, delivery_id=delivery).on_conflict_do_nothing().returning(e.c.id)
    return (await conn.execute(statement)).first() is not None


async def _all_done(conn: AsyncConnection, project_id: int, task_id: int) -> bool:
    k = tables.tasks.links
    query = select(k.c.plan_id, k.c.step_id).where(k.c.task_id == task_id, k.c.kind == "plan_step")
    pairs = {(row.plan_id, row.step_id) for row in await conn.execute(query)}
    states = await step_states(conn, project_id, pairs, None, None)
    return bool(pairs) and all(state.status == DONE for state in states.values())


async def _move(conn: AsyncConnection, row, config: Settings, cause: str, delivery: int | None) -> None:
    target = config.on_steps_done
    if target is None or row.status == target or config.finished(row.status):
        return
    t = tables.tasks.items
    if await _written(conn, row.id, "status", row.status, target, cause, delivery):
        await conn.execute(update(t).where(t.c.id == row.id).values(status=target, updated_at=func.now()))


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
    for row in await _linked(conn, project_id, plan_id, str(step)):
        if not await _written(conn, row.id, STEP, payload.get("from"), DONE, cause, delivery):
            continue
        written += 1
        if config is not None and config.on_steps_done and await _all_done(conn, project_id, row.id):
            await _move(
                conn, row, config, f"every plan step it links is done, plan {plan_id} step {step} last", delivery
            )
    return written


async def on_plan_step(params: dict, context: CoreContext) -> dict:
    """The contribution ``steps``: ``params`` is plugin/1's ``event`` (the event's payload and its delivery)."""
    if params.get("kind", "plan.step") != "plan.step" or context.engine is None or context.project_id is None:
        return {}
    async with context.engine.begin() as conn:
        await steps_done(conn, context.project_id, context.project or "", params["payload"], params.get("delivery"))
    return {}


__all__ = ["on_plan_step", "steps_done"]
