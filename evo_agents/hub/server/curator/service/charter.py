"""A project's charter: who may read and write it, how a revision is shown, and writing a new one with the schedule
of the night shift it keeps (see the package)."""

from __future__ import annotations

import json

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import curator
from evo_agents.hub.server.agents.service import placement as agent_placement
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, Forbidden, NotFound, Unprocessable
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.curator.models.charter import CHARTER, Charter, CharterRevision, CharterWrite
from evo_agents.hub.server.curator.repository import charter as store
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import Principal, holds


def admin(access: ProjectAccess, doing: str) -> None:
    if not holds(access.role, "admin"):
        held = access.role or "no grant"
        raise Forbidden(f"{doing} of project {access.name} needs the admin role on it; you hold {held}")


def reader(access: ProjectAccess) -> None:
    if access.role is None:
        raise Forbidden(f"reading the night shift of project {access.name} needs a grant on it")


async def read_access(conn: AsyncConnection, user: Principal, project: str) -> ProjectAccess:
    """``user``'s access to the project, once they may read its night shift."""
    access = await project_access(conn, user, project)
    reader(access)
    return access


def charter_view(project: str, row, *, admin: bool) -> Charter:
    body = dict(row.body)
    if not admin:
        body["judge"] = {**body.get("judge", {}), "hidden_checks": None}
    return Charter(
        **body,
        project=project,
        revision=row.revision,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
        schedule_owner=row.schedule_owner,
    )


def no_charter(project: str) -> NotFound:
    return NotFound(f"project {project} has no charter: an admin writes one with `evo-agents hub curator charter set`")


async def show(engine: AsyncEngine, user: Principal, project: str, revision: int | None) -> Charter:
    """The project's charter, at its newest revision or at ``revision``."""
    async with UnitOfWork(engine) as conn:
        access = await read_access(conn, user, project)
        row = await store.charter_row(conn, access.project_id, revision)
        if row is None:
            if revision is not None:
                raise NotFound(f"project {project} has no charter revision {revision}")
            raise no_charter(project)
        return charter_view(access.name, row, admin=holds(access.role, "admin"))


async def revisions(engine: AsyncEngine, user: Principal, project: str) -> list[CharterRevision]:
    async with UnitOfWork(engine) as conn:
        access = await read_access(conn, user, project)
        rows = await store.revisions(conn, access.project_id)
        return [
            CharterRevision(
                revision=row.revision,
                updated_by=row.updated_by,
                updated_at=row.updated_at,
            )
            for row in rows
        ]


# Writing


async def _time_zone_known(conn: AsyncConnection, name: str) -> None:
    if not await store.time_zone_known(conn, name):
        raise Unprocessable(f"{name!r} is not a time zone the hub knows: give an IANA name such as Asia/Tokyo")


def _stored(body: CharterWrite, current) -> dict:
    """The body to keep: the Judge's checks of ``current`` when the write gives none."""
    stored = body.model_dump(mode="json")
    for key in ("window", "pacing", "night_budget_usd"):  # a charter has a window or pacing: the other is left out
        if stored[key] is None:
            del stored[key]
    if stored["review"].get("trigger") is None:  # a charter without a trigger keeps what it stored before
        stored["review"].pop("trigger", None)
    if stored["judge"]["hidden_checks"] is None:
        kept = (current.body.get("judge") or {}).get("hidden_checks") if current is not None else None
        stored["judge"]["hidden_checks"] = kept or []
    return stored


async def _duty_owner(conn: AsyncConnection, user: Principal, project_id: int) -> int:
    """The owner of the pool of the project's default agent, whose runs the night shift dispatches as; the writer when
    the agent has no pool."""
    home = await agent_placement.default_home(conn, project_id)
    if home is None:
        return user.user_id
    return await store.pool_owner(conn, home.pool_id)


async def write(engine: AsyncEngine, user: Principal, project: str, body: CharterWrite) -> tuple[Charter, int | None]:
    """Write a new revision of the project's charter, or none when ``body`` is the newest one's: the charter and the
    revision written, None when none was."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        admin(access, "changing the charter")
        current = await store.charter_row(conn, access.project_id)
        await _time_zone_known(conn, (body.window or body.pacing).timezone)  # pyright: ignore[reportOptionalMemberAccess]
        stored = _stored(body, current)
        if len(json.dumps(stored, ensure_ascii=False).encode()) > curator.MAX_CHARTER_BYTES:
            raise HTTPException(413, f"the charter is over {curator.MAX_CHARTER_BYTES // 1024} KiB as JSON")
        owner_id = await _duty_owner(conn, user, access.project_id)
        if current is not None and current.body == stored:
            await store.keep_schedule(conn, access.project_id, owner_id)
            return charter_view(access.name, current, admin=True), None
        revision = 1 if current is None else current.revision + 1
        values = {
            "project_id": access.project_id,
            "revision": revision,
            "body": stored,
            "created_by": user.user_id,
        }
        try:
            async with conn.begin_nested():
                await store.insert_revision(conn, values)
        except IntegrityError:
            raise Conflict(
                f"the charter of project {project} changed while this write was made: read it again"
            ) from None
        await store.keep_schedule(conn, access.project_id, owner_id)
        await Auditor.of(conn, user, access).record(CHARTER, f"{access.name} revision={revision}")
        row = await store.charter_row(conn, access.project_id, revision)
        return charter_view(access.name, row, admin=True), revision
