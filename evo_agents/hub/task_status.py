"""The event ``tasks.status`` (plan loop-engine, criterion a3): a task's status set or changed, whatever did it.

A task takes a status when it is made (``from`` None): by a member on the web or the CLI, by the agent of a run for the
member it works for, by an import, or by the plugin srs when a version is approved (a frame task of a new use case).
Its status changes when a member edits it, when the plugin srs cancels the task of a use case an approved version
dropped, and when the config's ``on_steps_done`` moves it once every plan step it links is done. Each of those emits
the event in the transaction that made it, for the plugins granted ``tasks:read`` (the core plugin tasks takes it
ungranted) and into hub_events, where the Loop reads it by cursor (``evo_agents.hub.plugin_events``).

The core plugins tasks and srs both write tasks through the hub's tables (``tables.tasks``) and import no other core
plugin, so both emit through this module. The caller says what changed and why (``StatusChange``), with the groups
its config gives the two statuses and the prefix of the project's numbers; ``emit`` reads the rest of the task in the
caller's transaction: its number, its parent, and its links to SRS items and plan steps.

A payload carries ids, codes and states, never a task's title, description or the text of its history: a plugin that
needs more reads the task through the API with its grant. ``v`` is the version of its shape (``hub_events.VERSION``).

SQLAlchemy Core on ``evo_agents.hub.tables``; nothing here imports ``evo_agents.hub.server`` or a core plugin.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import hub_events, plugin_events, tables

KIND = hub_events.TASK_STATUS
# what set or changed the status: a task made by a member or for one (``create``), by an import, or as the frame task
# of a use case new in an approved SRS version (``srs_approval``, ``from`` None); a member's edit; the plugin srs
# cancelling the task of a use case an approved version dropped (``srs_approval``); on_steps_done (``steps_done``)
CREATE, EDIT, IMPORT, SRS_APPROVAL, STEPS_DONE = "create", "edit", "import", "srs_approval", "steps_done"
CAUSES = (CREATE, EDIT, IMPORT, SRS_APPROVAL, STEPS_DONE)
PERSON, HUB = "person", "hub"  # who changed it: a member (or an agent for one), or the hub on its own


@dataclass(frozen=True)
class StatusChange:
    """A task's status set (``old`` None) or changed: the two statuses with the groups the config gives them, why
    (one of CAUSES, with the ids of what made it in ``source``: the SRS document and version, or the plan step), who
    (``user_id``; None: the hub) and the prefix of the project's task numbers."""

    task_id: int
    old: str | None
    new: str
    old_group: str | None
    new_group: str | None
    cause: str
    prefix: str
    user_id: int | None = None
    source: dict = field(default_factory=dict)


async def _task(conn: AsyncConnection, task_id: int):
    t = tables.tasks.items
    parent = t.alias("parent")
    query = (
        select(t.c.number, t.c.parent_id, parent.c.number.label("parent_number"))
        .select_from(t.outerjoin(parent, parent.c.id == t.c.parent_id))
        .where(t.c.id == task_id)
    )
    return (await conn.execute(query)).one()


async def _links(conn: AsyncConnection, task_id: int) -> tuple[list[dict], list[dict]]:
    """(the SRS items task ``task_id`` links, id and code each; the plan steps it links, plan and step each)."""
    k, i = tables.tasks.links, tables.srs.items
    query = (
        select(k.c.kind, k.c.srs_item_id, i.c.code, k.c.plan_id, k.c.step_id)
        .select_from(k.outerjoin(i, i.c.id == k.c.srs_item_id))
        .where(k.c.task_id == task_id, k.c.kind.in_(("srs_item", "plan_step")))
        .order_by(k.c.id)
    )
    items, steps = [], []
    for row in await conn.execute(query):
        if row.kind == "srs_item":
            items.append({"id": row.srs_item_id, "code": row.code})
        else:
            steps.append({"plan_id": row.plan_id, "step": row.step_id})
    return items, steps


async def payload(conn: AsyncConnection, change: StatusChange) -> dict:
    """The payload of ``tasks.status`` for ``change``, read in the caller's transaction."""
    if change.cause not in CAUSES:
        raise ValueError(f"{change.cause!r} is not a cause of tasks.status")
    row = await _task(conn, change.task_id)
    items, steps = await _links(conn, change.task_id)
    login = None
    if change.user_id is not None:
        login = await conn.scalar(select(tables.users.c.login).where(tables.users.c.id == change.user_id))
    return {
        "v": hub_events.VERSION,
        "task_id": change.task_id,
        "number": row.number,
        "ref": f"{change.prefix}-{row.number}",
        "from": change.old,
        "to": change.new,
        "from_group": change.old_group,
        "to_group": change.new_group,
        "actor": HUB if change.user_id is None else PERSON,
        "user_id": change.user_id,
        "login": login,
        "cause": change.cause,
        "source": change.source or None,
        "parent_id": row.parent_id,
        "parent": f"{change.prefix}-{row.parent_number}" if row.parent_number is not None else None,
        "srs_items": items,
        "plan_steps": steps,
    }


async def emit(conn: AsyncConnection, project_id: int, change: StatusChange) -> int:
    """Emit ``tasks.status`` of ``change`` in project ``project_id``, in the caller's transaction; how many plugins
    take it."""
    return await plugin_events.emit(conn, KIND, project_id, await payload(conn, change))


__all__ = [
    "CAUSES",
    "CREATE",
    "EDIT",
    "HUB",
    "IMPORT",
    "KIND",
    "PERSON",
    "SRS_APPROVAL",
    "STEPS_DONE",
    "StatusChange",
    "emit",
    "payload",
]
