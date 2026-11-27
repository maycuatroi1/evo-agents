"""The operations on the records of an isolated plugin (``evo_agents.hub.plugin_records``) that both doors of
``routes.records`` run, the plugin's backend's and a member's, each in the transaction its route opens. A refusal of
the store is the HTTP error of its status; a record the call may not see answers 404, as one that does not exist."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import plugin_calls, plugin_records
from evo_agents.hub.plugin_records import RecordError
from evo_agents.hub.server.plugin_api.models.records import RecordIn, RecordOut, RecordPage
from evo_agents.hub.server.plugins.models.delegation import Resolved
from evo_agents.hub.server.plugins.service import delegation
from evo_agents.hub.server.plugins.service.delegation import may_write
from evo_agents.hub.server.plugins.service.extension import enabled_for

Door = Callable[[AsyncConnection], Awaitable[Resolved]]


def _fail(exc: RecordError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


async def direct(conn: AsyncConnection, user: Any, plugin: str, project: str) -> Resolved:
    """What a member (or a page's bridge, or a run's agent) reaches of ``plugin``'s records with their own grant."""
    access, row = await enabled_for(conn, user, plugin, project)
    if delegation.PERMISSION not in plugin_calls.effective_grant(row):
        raise HTTPException(404, f"plugin {plugin} is not granted {delegation.PERMISSION} in project {project}")
    scope = user.scope
    people = await delegation.people_of(
        conn, user.user_id, scope.run_id if scope else None, scope.kind if scope else None
    )
    project_id, plugin_id = row.project_id, row.id  # pyright: ignore[reportAttributeAccessIssue]
    reach = plugin_records.Reach(
        project_id, plugin_id, access.rules, access.max_level, user.user_id, delegation.through(user, None), people
    )
    return Resolved(reach, access.role)


async def listing(conn: AsyncConnection, door: Door, collection: str, after: str | None, limit: int) -> RecordPage:
    """At most ``limit`` records of ``collection`` after the key ``after``."""
    try:
        resolved = await door(conn)
        records, more = await plugin_records.listing(conn, resolved.reach, collection, after=after, limit=limit)
    except RecordError as exc:
        raise _fail(exc) from None
    return RecordPage(records=records, next_after=more)  # pyright: ignore[reportArgumentType]


async def show(conn: AsyncConnection, door: Door, collection: str, key: str) -> RecordOut:
    """The record ``key`` of ``collection``."""
    try:
        resolved = await door(conn)
        return RecordOut(**await plugin_records.get(conn, resolved.reach, collection, key))
    except RecordError as exc:
        raise _fail(exc) from None


async def write(
    conn: AsyncConnection, door: Door, collection: str, key: str, given: RecordIn
) -> tuple[RecordOut, bool]:
    """(the record ``key`` of ``collection`` as written, whether it is new)."""
    try:
        resolved = await door(conn)
        may_write(resolved)
        record, new = await plugin_records.put(
            conn,
            resolved.reach,
            collection,
            key,
            given.body,
            level=given.level,
            audience=given.audience,
            revision=given.revision,
        )
    except RecordError as exc:
        raise _fail(exc) from None
    return RecordOut(**record), new


async def erase(conn: AsyncConnection, door: Door, collection: str, key: str, revision: int | None) -> None:
    """Delete the record ``key`` of ``collection``, only at ``revision`` when given."""
    try:
        resolved = await door(conn)
        may_write(resolved)
        await plugin_records.remove(conn, resolved.reach, collection, key, revision=revision)
    except RecordError as exc:
        raise _fail(exc) from None


__all__ = ["Door", "direct", "erase", "listing", "show", "write"]
