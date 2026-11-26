"""The plugin's events contribution ``steps``: the hub's ``plan.step`` event of a project that enabled the plugin and
granted it ``plans:read`` (``evo_agents.hub.plugin_events``). What the event does to the tasks linked to the step is
``service.steps``'s: each gets a row of its history, and moves to ``on_steps_done`` once every plan step it links is
done.

The hub delivers an event at least once, with the same delivery id on every try: a row written for a delivery names
it, and a task that has its row already is passed over, so a second try writes nothing.
"""

from __future__ import annotations

from evo_agents.hub.plugin_calls import CoreContext
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.tasks.service.steps import steps_done


async def on_plan_step(params: dict, context: CoreContext) -> dict:
    """The contribution ``steps``: ``params`` is plugin/1's ``event`` (the event's payload and its delivery)."""
    if params.get("kind", "plan.step") != "plan.step" or context.engine is None or context.project_id is None:
        return {}
    async with UnitOfWork(context.engine) as conn:
        await steps_done(conn, context.project_id, context.project or "", params["payload"], params.get("delivery"))
    return {}


__all__ = ["on_plan_step"]
