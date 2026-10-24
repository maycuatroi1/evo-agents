"""The ``act`` work item of the engine ``rules/2`` (plan problem-intake, criterion a3): a claim of a solution whose
delivery is ``action`` needs no plan and no build, so its work item hands out a task of the plugin tasks instead of a
run.

The task carries the problem, the goal, the claim and its test card (``description``). It is assigned to the member
who created the claim while they hold a grant on the project, else the owner of its goal, else a member who may write
in the project; it is due on the test card's deadline while that day has not passed. The work item stands as running
with no run; ``rules2`` moves it from the task alone: done when the task is done, a failure (queued again, then
triage, then a person, as for any kind) when the task is cancelled or past its due day.

A task of an earlier attempt that is still open is the work item's task again, so a failure for being overdue does not
make a second task; a cancelled one is not, and the next attempt makes a new one. The task is made through the tables,
as the plugin srs makes its frame tasks: the hub layer does not import a core plugin.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import access, runs, tables, task_status
from evo_agents.hub.plugin_calls import enabled_in
from evo_agents.hub.server.loop import texts
from evo_agents.hub.server.loop.admit import TYPE_REFUSED, Handed
from evo_agents.hub.server.loop.model import CLOSED_GROUPS, ItemView
from evo_agents.hub.server.loop.runs_of import goal_owner
from evo_agents.hub.server.loop.view import status_groups
from evo_agents.hub.server.plugin_configs import stored_stacks
from evo_agents.hub.tables_tasks import MAX_TITLE_CHARS
from evo_agents.plugins import config as plugin_config

TASKS = "tasks"  # the core plugin whose tasks an act becomes
KEY = "act:{project}:{item}:a{attempt}"  # tasks_items.external_key of the task of one attempt
CAUSE = "made by the Loop for an act work item"
MAX_DESCRIPTION_BYTES = 20_000


def key_of(project_id: int, item_id: int, attempt: int) -> str:
    return KEY.format(project=project_id, item=item_id, attempt=attempt)


async def _config(conn: AsyncConnection, project_id: int):
    """(prefix, first status, type, status -> group) of the plugin tasks in the project; None when it is off or its
    config names no status."""
    plugin = await enabled_in(conn, TASKS, project_id)
    if plugin is None:
        return None
    groups = await status_groups(conn, plugin, project_id)
    if not groups:
        return None
    stacks = await stored_stacks(conn, [plugin.id], project_id, None)
    merged = plugin_config.merge(plugin.manifest.get("config"), stacks.get(plugin.id, []))
    values = {name: item.value for name, item in merged.fields.items() if not item.secret}
    statuses = [s for s in values.get("statuses") or [] if isinstance(s, dict) and s.get("key")]
    types = [t for t in values.get("types") or [] if isinstance(t, str)]
    if not statuses or not types:
        return None
    return values.get("prefix") or "T", statuses[0]["key"], "task" if "task" in types else types[0], groups


async def _holds(conn: AsyncConnection, project_id: int, user_id: int | None) -> bool:
    if user_id is None:
        return False
    g = tables.grants
    query = select(g.c.role).where(g.c.project_id == project_id, g.c.user_id == user_id)
    role = await conn.scalar(query)
    return role is not None and access.ROLES.index(role) >= access.ROLES.index("writer")


async def assignee(conn: AsyncConnection, project_id: int, item: ItemView, created_by: int | None) -> int | None:
    """Who the task goes to: the claim's creator, the goal's owner, or the member with a writer or admin grant who was
    granted first; None when the project has none."""
    for user_id in (created_by, await goal_owner(conn, item.goal_id)):
        if await _holds(conn, project_id, user_id):
            return user_id
    g = tables.grants
    query = select(g.c.user_id).where(g.c.project_id == project_id, g.c.role.in_(("writer", "admin")))
    return await conn.scalar(query.order_by(g.c.granted_at, g.c.user_id).limit(1))


def _due(card: dict | None, today: date) -> date | None:
    deadline = (card or {}).get("deadline")
    try:
        day = date.fromisoformat(str(deadline)) if deadline else None
    except ValueError:
        return None
    return day if day is not None and day >= today else None


async def _earlier(conn: AsyncConnection, project_id: int, item: ItemView, groups: dict):
    """The newest task of an earlier attempt of ``item`` while it is open, else None."""
    t = tables.tasks.items
    query = (
        select(t.c.id, t.c.status)
        .where(t.c.project_id == project_id, t.c.external_key.like(f"act:{project_id}:{item.id}:a%"))
        .order_by(t.c.id.desc())
        .limit(1)
    )
    found = (await conn.execute(query)).first()
    if found is None or groups.get(found.status) in CLOSED_GROUPS:
        return None
    return found


async def description(conn: AsyncConnection, project: str, item: ItemView) -> tuple[str, str]:
    """(the task's description, its title) for act ``item``: the problem and goal it answers, the claim and its test
    card, with what members wrote marked as data."""
    goal, claim = await texts.goal_of(conn, item.goal_id), await texts._claim(conn, item.claim_id)
    ref = f"CL-{claim.number}" if claim is not None else "a claim"
    lines = [
        f"Do what claim {ref} proposes in project {project}: it needs no code, so the Loop made this task of it as "
        f"work item #{item.id} ({item.key}). Close the task when the action is done, and cancel it when it will not "
        "be.",
        "",
        *texts._goal_lines(goal),
        *texts._problem_lines(goal),
    ]
    title = f"Act on {ref}"
    if claim is not None:
        lines += [
            f"Claim {ref} of canvas {claim.slug} {texts.DATA}, cell {claim.cell}, in testing: {texts._line(claim.text)}",
            f"Its test card: {texts._card(claim)}",
        ]
        title = f"{ref}: {texts._line(claim.text)}"
    return runs.clip("\n".join(lines), MAX_DESCRIPTION_BYTES), title[:MAX_TITLE_CHARS]


async def _next_number(conn: AsyncConnection, project_id: int) -> int:
    t = tables.tasks.items
    return (
        int(await conn.scalar(select(func.coalesce(func.max(t.c.number), 0)).where(t.c.project_id == project_id))) + 1
    )


async def _make(conn: AsyncConnection, project_id: int, item: ItemView, config, who: int, today: date) -> int:
    prefix, first, kind, groups = config
    claim = await texts._claim(conn, item.claim_id)
    c = tables.canvas.claims
    label = await conn.scalar(select(c.c.label).where(c.c.id == item.claim_id)) if item.claim_id else None
    project = await conn.scalar(select(tables.projects.c.name).where(tables.projects.c.id == project_id))
    body, title = await description(conn, project, item)
    t, p = tables.tasks.items, tables.tasks.people
    number = await _next_number(conn, project_id)
    values = {
        "project_id": project_id,
        "number": number,
        "type": kind,
        "title": title,
        "description_md": body,
        "status": first,
        "due_on": _due(None if claim is None else claim.test_card, today),
        "label": label or {},
        "external_key": key_of(project_id, item.id, item.attempts + 1),
        "external_ref": {"source": "loop", "item": item.id, "claim": item.claim_id},
        "created_by": (await goal_owner(conn, item.goal_id)) or who,
    }
    task_id = await conn.scalar(insert(t).values(**values).returning(t.c.id))
    event = {"task_id": task_id, "actor_id": values["created_by"], "field": "created", "from_value": None}
    await conn.execute(insert(tables.tasks.events).values(**event, to_value=f"{prefix}-{number}", cause=CAUSE))
    await conn.execute(insert(p).values(task_id=task_id, role="assignee", user_id=who))
    change = task_status.StatusChange(
        task_id, None, first, None, groups.get(first), task_status.CREATE, prefix, values["created_by"]
    )
    await task_status.emit(conn, project_id, change)
    return task_id


async def hand_out(conn: AsyncConnection, project_id: int, item: ItemView, now: datetime) -> Handed:
    """The task of act ``item``: an open task of an earlier attempt again, else a new one; the work item takes it as its
    frame task and goes to running with no run. ``type_refused`` with why when the project cannot take one."""
    config = await _config(conn, project_id)
    if config is None:
        return Handed(
            reason=TYPE_REFUSED, why="the plugin tasks is not enabled in the project, or its config has no status"
        )
    c = tables.canvas.claims
    created_by = await conn.scalar(select(c.c.created_by).where(c.c.id == item.claim_id)) if item.claim_id else None
    who = await assignee(conn, project_id, item, created_by)
    if who is None:
        return Handed(reason=TYPE_REFUSED, why="the project has no member who may write, to give the task to")
    held = await _earlier(conn, project_id, item, config[3])
    task_id = held.id if held is not None else None
    if task_id is None:
        try:
            async with conn.begin_nested():
                task_id = await _make(conn, project_id, item, config, who, now.date())
        except IntegrityError:
            return Handed(
                reason=TYPE_REFUSED,
                why="another write of the project's tasks took the number first; the next tick tries again",
            )
    i = tables.loop_items
    await conn.execute(update(i).where(i.c.id == item.id, i.c.project_id == project_id).values(task_id=task_id))
    return Handed(started=True)


__all__ = ["assignee", "description", "hand_out", "key_of"]
