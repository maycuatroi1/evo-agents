"""The rows of the kg_init runs (``kg_init_runs``) and what asks for one after a build: the database half of
``evo_agents.hub.kg_init``, kept outside the server package because ``evo_agents.hub.kg_build`` calls ``on_build`` as it
records a build. A failure here never fails the build: ``on_build`` runs in a savepoint and logs what it could not do."""

from __future__ import annotations

import logging
from collections.abc import Mapping

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import kg_init, tables
from evo_agents.hub.tables_kg import INIT_ACTIVE

log = logging.getLogger(__name__)


async def active_of(conn: AsyncConnection, project_id: int):
    t = tables.kg_init_runs
    return (await conn.execute(select(t).where(t.c.project_id == project_id, t.c.state.in_(INIT_ACTIVE)))).one_or_none()


async def of_agent_run(conn: AsyncConnection, run_id: int):
    """The kg_init run whose review run ``run_id`` is, None when none is."""
    t = tables.kg_init_runs
    return (await conn.execute(select(t).where(t.c.agent_run_id == run_id).order_by(t.c.id.desc()).limit(1))).first()


async def last_profile(conn: AsyncConnection, project_id: int) -> Mapping | None:
    """The profile the project's newest kg_init run was given; None when it never had a run."""
    t = tables.kg_init_runs
    found = await conn.scalar(
        select(t.c.profile)
        .where(t.c.project_id == project_id, t.c.profile.is_not(None))
        .order_by(t.c.id.desc())
        .limit(1)
    )
    return found if isinstance(found, dict) else None


async def create(
    conn: AsyncConnection,
    project_id: int,
    via: str,
    why: list[str],
    *,
    requested_by: int | None = None,
    build_id: int | None = None,
    profile: Mapping | None = None,
):
    """Queue a run of ``project_id``: (the row, True), or (the run in flight, False): one runs at a time."""
    t = tables.kg_init_runs
    values = {
        "project_id": project_id,
        "via": via,
        "requested_by": requested_by,
        "state": "queued",
        "why": why,
        "build_id": build_id,
        "profile": None if profile is None else dict(profile),
        "body": {},
    }
    try:
        async with conn.begin_nested():
            return (await conn.execute(insert(t).values(**values).returning(t))).one(), True
    except IntegrityError:  # another call queued one in between
        found = await active_of(conn, project_id)
        if found is None:
            raise
        return found, False


async def on_build(conn: AsyncConnection, project_id: int, build_id: int, coverage: Mapping | None) -> int | None:
    """Queue a kg_init run after build ``build_id`` of the project succeeded with ``coverage``, when ``kg_init.wants_run``
    says so: the project's first successful build with a knowledge config, or a profile that moved far from the last
    run's. Its id, or None. A project with no run yet and an earlier build gets none: it waits to be called."""
    profile = (coverage or {}).get("profile")
    if not isinstance(profile, dict) or not profile:
        return None
    c, b = tables.kg_configs, tables.kg_builds
    has_config = (await conn.execute(select(c.c.project_id).where(c.c.project_id == project_id))).first() is not None
    earlier = await conn.scalar(
        select(func.count()).where(b.c.project_id == project_id, b.c.status == "succeeded", b.c.id != build_id)
    )
    previous = await last_profile(conn, project_id)
    why = kg_init.wants_run(has_config=has_config, first_build=not earlier, last_profile=previous, profile=profile)
    if not why:
        return None
    via = "first_build" if not earlier else "profile"
    if await active_of(conn, project_id) is not None:
        return None
    row, made = await create(conn, project_id, via, why, build_id=build_id, profile=profile)
    log.info("kg init run queued", extra={"project_id": project_id, "id": row.id, "via": via})
    return row.id if made else None
