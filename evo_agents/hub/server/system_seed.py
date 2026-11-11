"""The system the hub gives a project that has none (plan hub-steward, criterion a11): ``orchestrator``, named
"Orchestrator", an entry node and an orchestrator node on a profile the hub creates for it, as the project's default
system with its ops off.

The hub seeds a project once (``projects.systems_seeded_at``, 0237): the pass (``seed_pass``, in the reaper's pass
every minute) looks at each project not seeded yet, gives it the system when it has none, and marks it seeded either
way. A project whose default system was deleted later stays without one until an admin sets another.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import profiles, system_spec, tables
from evo_agents.hub.server import audit, profile_store, system_store

log = logging.getLogger(__name__)

SLUG = "orchestrator"
NAME = "Orchestrator"
HANDLE_SUFFIX = "-orchestrator"
HANDLE_TRIES = 9  # @<project>-orchestrator, then -2 to -9 when another agent holds it
DESCRIPTION = "The Orchestrator of the project's default system, created by the hub."
PURPOSE = "The project's default system: its Orchestrator handles what goes wrong in the project's runs."


def _handles(project: str):
    stem = project[: 100 - len(HANDLE_SUFFIX) - 2] + HANDLE_SUFFIX
    yield stem
    for n in range(2, HANDLE_TRIES + 1):
        yield f"{stem}-{n}"


async def _profile(conn: AsyncConnection, project_id: int, project: str, owner_id: int) -> str | None:
    """Create the Orchestrator's profile at version 1, with its pool; its handle, or None when every handle tried is
    taken."""
    ap, p = tables.agent_profiles, tables.projects
    for handle in _handles(project):
        if await conn.scalar(select(p.c.id).where(p.c.name == handle)) is not None:
            continue
        values = {
            "handle": handle,
            "name": NAME,
            "description": DESCRIPTION,
            "instructions": "",
            "color": profiles.default_color(handle),
            "avatar_sha256": None,
            "is_default": False,
            "permissions": profiles.all_access(),
            "packs": [],
            "starters": [],
            "runs_on": profiles.DEFAULT_RUNS_ON,
        }
        statement = (
            pg_insert(ap)
            .values(project_id=project_id, created_by=owner_id, **values)
            .on_conflict_do_nothing(index_elements=[ap.c.handle])
            .returning(ap.c.id)
        )
        profile_id = (await conn.execute(statement)).scalar_one_or_none()
        if profile_id is None:
            continue
        body = {key: values[key] for key in profile_store.SNAPSHOT}
        await profile_store.write_version(conn, profile_id, 1, body, owner_id)
        await profile_store.create_pool(conn, profile_id, handle, project_id, owner_id)
        return handle
    return None


def spec_of(handle: str) -> dict:
    """The spec of the system the hub gives a project, its orchestrator on profile ``handle`` at version 1."""
    given = {
        "schema": system_spec.SCHEMA,
        "name": NAME,
        "purpose": PURPOSE,
        "ops": {"enabled": False},
        "nodes": [
            {"id": "entry", "kind": "entry", "position": {"x": 0, "y": 0}},
            {
                "id": "orchestrator",
                "kind": "orchestrator",
                "profile": handle,
                "version": 1,
                "position": {"x": 320, "y": 0},
            },
        ],
        "edges": [],
    }
    spec, problems = system_spec.check(given)
    assert not system_spec.errors(problems), problems
    return spec


async def seed(conn: AsyncConnection, project_id: int) -> str | None:
    """Give project ``project_id`` its ``orchestrator`` system as its default when it was never seeded and has no
    system, and mark it seeded; the slug of the system made, or None."""
    p, s = tables.projects, tables.agent_systems
    query = select(p.c.name, p.c.created_by, p.c.systems_seeded_at).where(p.c.id == project_id)
    project = (await conn.execute(query.with_for_update(skip_locked=True))).one_or_none()
    if project is None or project.systems_seeded_at is not None:
        return None
    await conn.execute(update(p).values(systems_seeded_at=func.now()).where(p.c.id == project_id))
    oldest = await conn.scalar(select(func.min(s.c.id)).where(s.c.project_id == project_id))
    if oldest is not None:  # one an admin made before this pass: the oldest is the default, as 0237 made it
        if await conn.scalar(select(s.c.id).where(s.c.project_id == project_id, s.c.is_default)) is None:
            await conn.execute(update(s).values(is_default=True).where(s.c.id == oldest))
        return None
    handle = await _profile(conn, project_id, project.name, project.created_by)
    if handle is None:
        log.warning("no handle for the orchestrator of a project", extra={"project": project.name})
        return None
    await system_store.write(conn, project_id, SLUG, spec_of(handle), None, project.created_by, None)
    await conn.execute(update(s).values(is_default=True).where(s.c.project_id == project_id, s.c.slug == SLUG))
    await audit.record(
        conn,
        actor_id=None,
        token_id=None,
        action=audit.SYSTEM_CREATE,
        target=f"{project.name} system={SLUG} revision=1 by=hub",
        project_id=project_id,
    )
    return SLUG


async def seed_pass(engine: AsyncEngine) -> int:
    """Seed each project not seeded yet (``seed``), one transaction a project; how many systems it made."""
    p = tables.projects
    async with engine.connect() as conn:
        ids = (await conn.execute(select(p.c.id).where(p.c.systems_seeded_at.is_(None)).order_by(p.c.id))).scalars()
        ids = list(ids)
    made = 0
    for project_id in ids:
        async with engine.begin() as conn:
            made += await seed(conn, project_id) is not None
    if made:
        log.info("orchestrator systems seeded", extra={"systems": made})
    return made


__all__ = ["NAME", "SLUG", "seed", "seed_pass", "spec_of"]
