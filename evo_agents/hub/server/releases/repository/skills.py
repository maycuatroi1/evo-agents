"""The SQL of the release of a global skill from the Curator's merges (``service.skill_release``): the latest version
of a global skill, the owner of a project's night shift, the merges due a release, and the versions and blob it
records."""

from __future__ import annotations

from sqlalchemy import exists, func, insert, select, true
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.releases.models.skill_release import ACTION, MAX_RELEASES, NIGHT_SHIFT


def _latest_global(name: str):
    """The global skill ``name`` (matched ignoring case) with its latest version's number, bundle and source."""
    s, v = tables.skills, tables.skill_versions
    latest = select(v).where(v.c.skill_id == s.c.id).order_by(v.c.version.desc()).limit(1).lateral("v")
    return (
        select(s.c.id, s.c.name, latest.c.version, latest.c.sha256, latest.c.source_repo)
        .select_from(s.join(latest, true()))
        .where(s.c.scope == "global", func.lower(s.c.name) == name.lower())
    )


async def latest_global(conn: AsyncConnection, name: str):
    """The global skill ``name`` with its latest version, or None."""
    return (await conn.execute(_latest_global(name))).one_or_none()


async def lock_latest(conn: AsyncConnection, skill_id: int, name: str):
    """The global skill ``name`` (row ``skill_id``) locked, then read again, so the version numbers follow one at a
    time."""
    s = tables.skills
    await conn.execute(select(s.c.id).where(s.c.id == skill_id).with_for_update())
    return (await conn.execute(_latest_global(name))).one()


async def night_shift_owner(conn: AsyncConnection, project_id: int) -> int | None:
    s = tables.schedules
    query = select(s.c.owner_id).where(s.c.project_id == project_id, s.c.kind == NIGHT_SHIFT).order_by(s.c.id)
    return (await conn.execute(query.limit(1))).scalar_one_or_none()


async def due(conn: AsyncConnection) -> list:
    """The hub's merges that release a skill and have no line ``skill_release`` yet, the oldest first."""
    lg, c, r = tables.curator_ledger, tables.curator_changes, tables.project_repos
    done = tables.curator_ledger.alias("done")
    released = exists().where(done.c.change_id == lg.c.change_id, done.c.action == ACTION)
    query = (
        select(
            lg.c.change_id,
            lg.c.details,
            c.c.project_id,
            c.c.proposal_id,
            c.c.repo,
            c.c.merge_sha,
            r.c.origin,
        )
        .join_from(lg, c, c.c.id == lg.c.change_id)
        .join(r, (r.c.project_id == c.c.project_id) & (r.c.name == c.c.repo), isouter=True)
        .where(lg.c.action == "merged", lg.c.actor == "curator", lg.c.details.has_key("release"), ~released)
        .order_by(lg.c.id)
        .limit(MAX_RELEASES)
    )
    return list((await conn.execute(query)).all())


async def insert_version(conn: AsyncConnection, values: dict) -> None:
    await conn.execute(insert(tables.skill_versions).values(**values))


async def insert_blob(conn: AsyncConnection, values: dict) -> None:
    """The blob ``values`` of the hub's own, unless it is there."""
    await conn.execute(pg_insert(tables.blobs).values(**values).on_conflict_do_nothing())


async def release_details(conn: AsyncConnection, change_id: int) -> dict | None:
    """The details of the newest line ``skill_release`` of change ``change_id``."""
    lg = tables.curator_ledger
    query = select(lg.c.details).where(lg.c.change_id == change_id, lg.c.action == ACTION).order_by(lg.c.id.desc())
    return (await conn.execute(query.limit(1))).scalar_one_or_none()


async def version_of(conn: AsyncConnection, skill_id: int, version: int):
    v = tables.skill_versions
    return (await conn.execute(select(v).where(v.c.skill_id == skill_id, v.c.version == version))).one()
