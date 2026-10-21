"""Who a tool of the plugin canvas acts for (``acting``), and what it writes as (``Acting.writer``).

The agent of a chat run acts for the person who mentioned @strategist, the origin of the chain
(``chat_runs.ChatRun.dispatcher_id``), also when the run went to the worker of the type's run_as; the agent of any
other run, a task run included, for the member who dispatched it (a task run the hub handed the type: its run_as);
anyone else, a member's own session, for themselves. A canvas is read when that person sees it through the session's
sink and, when the run's worker is another member's, when that member does too: what a tool answers reaches the
machine the agent runs on. What the agent of a run writes carries the provenance of an agent: the person, the run,
its runtime and model, and for a chat run the message that started it, which an undo in chat takes back by.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.canvas import store, writes
from evo_agents.core_plugins.canvas.content import CanvasError
from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import CoreContext, enabled_in
from evo_agents.hub.server.agent_types import enabled_type
from evo_agents.hub.server.chat_runs import chat_run
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import MACHINE, Principal

PLUGIN = "canvas"
STRATEGIST = "strategist"  # the plugin's agent type, and the handle of its profile


@dataclass(frozen=True)
class Acting:
    """The person a tool acts for, their access to the project, the session's sink, and for the agent of a run: the
    run, its kind, whether it is a run of @strategist, the access of the member whose worker runs it when another,
    and the run's runtime, model and the chat message that started it."""

    access: ProjectAccess
    person: Principal
    sink: str | None
    run_id: int | None = None
    run_kind: str | None = None
    strategist: bool = False
    worker: ProjectAccess | None = None
    runtime: str | None = None
    model: str | None = None
    chat_message_id: int | None = None

    def sees(self, label: dict) -> bool:
        """Whether the person, and the member whose worker runs the agent when another, see what carries ``label``."""
        if not store.visible(self.access, label, self.sink):
            return False
        return self.worker is None or store.visible(self.worker, label, self.sink)

    def writer(self) -> writes.Writer:
        """The writer of what the tool writes: an agent for the person, with the run's provenance."""
        if self.run_id is None:
            return writes.person(self.access, self.person.user_id, self.sink)
        author = writes.Author(
            "agent", self.person.user_id, self.run_id, self.runtime, self.model, self.chat_message_id
        )
        return writes.Writer(self.access, author, self.sink)


async def _principal(conn: AsyncConnection, user_id: int, config) -> Principal:
    login = (await conn.execute(select(tables.users.c.login).where(tables.users.c.id == user_id))).scalar_one()
    return Principal(user_id, login, config is not None and config.is_admin(login), None, MACHINE, "")


async def _access(conn: AsyncConnection, person: Principal, project: str) -> ProjectAccess:
    try:
        return await project_access(conn, person, project)
    except HTTPException:
        raise CanvasError(403, f"{person.login} holds no grant on project {project} now") from None


async def is_strategist(conn: AsyncConnection, context: CoreContext, profile_id: int | None) -> bool:
    """Whether ``profile_id`` is the profile of the project's agent type strategist, enabled and on."""
    plugin = await enabled_in(conn, context.plugin, context.project_id)
    kind = await enabled_type(conn, context.project_id, plugin.id, STRATEGIST) if plugin is not None else None
    return (
        kind is not None
        and kind.disabled_at is None
        and kind.profile_deleted_at is None
        and kind.profile_id == profile_id
    )


async def acting(conn: AsyncConnection, params: dict, context: CoreContext) -> Acting:
    """Who the tool of ``params`` acts for (see the module)."""
    if context.run_id is None:
        u = tables.users
        login = (params.get("caller") or {}).get("login") or ""
        user_id = (await conn.execute(select(u.c.id).where(u.c.login == login))).scalar_one_or_none()
        if user_id is None:
            raise CanvasError(403, f"no member {login} on the hub")
        person = await _principal(conn, user_id, context.config)
        return Acting(await _access(conn, person, context.project), person, context.sink)
    r = tables.runs
    columns = (r.c.kind, r.c.profile_id, r.c.dispatched_by, r.c.runtime, r.c.model, r.c.chat_message_id)
    row = (await conn.execute(select(*columns).where(r.c.id == context.run_id))).one()
    person_id = row.dispatched_by
    found = await chat_run(conn, context.run_id) if row.kind == "chat" else None
    if found is not None:
        person_id = found.dispatcher_id
    person = await _principal(conn, person_id, context.config)
    access = await _access(conn, person, context.project)
    worker = None
    if person_id != row.dispatched_by:
        worker = await _access(conn, await _principal(conn, row.dispatched_by, context.config), context.project)
    return Acting(
        access,
        person,
        context.sink,
        run_id=context.run_id,
        run_kind=row.kind,
        strategist=await is_strategist(conn, context, row.profile_id),
        worker=worker,
        runtime=row.runtime,
        model=row.model,
        chat_message_id=row.chat_message_id if row.kind == "chat" else None,
    )


def strategist_run(who: Acting, tool: str, kinds: tuple[str, ...]) -> None:
    """Refuse a caller other than the agent of a run of @strategist of one of ``kinds``."""
    if who.run_id is None or not who.strategist or who.run_kind not in kinds:
        runs = " or ".join(f"a {kind} run" for kind in kinds)
        raise CanvasError(
            403, f"only the agent of {runs} of @{STRATEGIST} in project {who.access.name} calls canvas__{tool}"
        )


async def canvas(conn: AsyncConnection, who: Acting, slug: str, *, lock: bool = False) -> store.CanvasRow:
    """Canvas ``slug`` as ``who`` sees it; the 404 of one that does not exist when they cannot."""
    row = await store.canvas(conn, who.access, slug, who.sink, lock=lock)
    if not who.sees(row.label):
        raise store.missing(who.access, slug)
    return row


__all__ = ["PLUGIN", "STRATEGIST", "Acting", "acting", "canvas", "is_strategist", "strategist_run"]
