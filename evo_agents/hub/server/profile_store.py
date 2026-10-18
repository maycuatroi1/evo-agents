"""Agent profiles in the database (``evo_agents.hub.tables_profiles``): a profile as the hub holds it, the default
profile of a project and the version rows, for the routes of ``agent_profiles``, the registration of a project
(``projects``) and the runs (``runs.service.profiles``).

A version row holds the profile whole as that version had it (``snapshot``), its agent packs with the version each
pins; it is written in the transaction that makes the version. A row written before the packs (schema 0041) has no
``packs`` key, read as none. ``ensure_default`` gives a project its default profile when it has none: the registration of a new
project calls it, and so does a run queued in a project that has none (one inserted outside the API), so that every
run records a profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import profiles, tables

DEFAULT_DESCRIPTION = "The project's default agent: every permission, no instructions."
SNAPSHOT = (
    "handle",
    "name",
    "description",
    "instructions",
    "color",
    "avatar_sha256",
    "is_default",
    "permissions",
    "packs",
)


@dataclass(frozen=True)
class Profile:
    """A profile as the hub holds it now, with the name of its project and the login of who created it."""

    id: int
    project_id: int
    project: str
    handle: str
    name: str
    description: str
    instructions: str
    color: str
    avatar_sha256: str | None
    is_default: bool
    permissions: dict
    packs: list  # the agent packs it attaches, each {"plugin", "name", "version"} (agent_packs)
    version: int
    created_by: str
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None

    def snapshot(self) -> dict:
        return {key: getattr(self, key) for key in SNAPSHOT}

    @property
    def tag(self) -> str:
        """How an audit row names the profile and its version: ``@<handle>:v<version>``."""
        return f"@{self.handle}:v{self.version}"


def profile_select() -> Select:
    """The fields of Profile, a profile with its project's name and its creator's login."""
    ap, p, u = tables.agent_profiles, tables.projects, tables.users
    return (
        select(
            ap.c.id,
            ap.c.project_id,
            p.c.name.label("project"),
            ap.c.handle,
            ap.c.name,
            ap.c.description,
            ap.c.instructions,
            ap.c.color,
            ap.c.avatar_sha256,
            ap.c.is_default,
            ap.c.permissions,
            ap.c.packs,
            ap.c.version,
            u.c.login.label("created_by"),
            ap.c.created_at,
            ap.c.updated_at,
            ap.c.deleted_at,
        )
        .join_from(ap, p, p.c.id == ap.c.project_id)
        .join(u, u.c.id == ap.c.created_by)
    )


def profile_of(row) -> Profile:
    return Profile(**row._mapping)


async def by_handle(conn: AsyncConnection, handle: str, *, lock: bool = False) -> Profile | None:
    """The profile ``handle`` names, in any project, deleted or not; with ``lock`` its row is locked."""
    query = profile_select().where(tables.agent_profiles.c.handle == handle)
    if lock:
        query = query.with_for_update(of=tables.agent_profiles)
    row = (await conn.execute(query)).one_or_none()
    return None if row is None else profile_of(row)


async def by_id(conn: AsyncConnection, profile_id: int) -> Profile | None:
    row = (await conn.execute(profile_select().where(tables.agent_profiles.c.id == profile_id))).one_or_none()
    return None if row is None else profile_of(row)


async def write_version(conn: AsyncConnection, profile_id: int, version: int, body: dict, changed_by: int) -> None:
    v = tables.agent_profile_versions
    await conn.execute(insert(v).values(profile_id=profile_id, version=version, body=body, changed_by=changed_by))


async def ensure_default(conn: AsyncConnection, project_id: int) -> Profile:
    """The default profile of project ``project_id``, created at version 1 by the member who created the project when
    it has none."""
    ap, p = tables.agent_profiles, tables.projects
    found = (await conn.execute(profile_select().where(ap.c.project_id == project_id, ap.c.is_default))).one_or_none()
    if found is not None:
        return profile_of(found)
    project = (await conn.execute(select(p.c.name, p.c.created_by).where(p.c.id == project_id))).one()
    values = {
        "project_id": project_id,
        "handle": project.name,
        "name": project.name,
        "description": DEFAULT_DESCRIPTION,
        "instructions": "",
        "color": profiles.DEFAULT_COLOR,
        "avatar_sha256": None,
        "is_default": True,
        "permissions": profiles.all_access(),
        "packs": [],
        "created_by": project.created_by,
    }
    statement = pg_insert(ap).values(**values).on_conflict_do_nothing().returning(ap.c.id)
    created = (await conn.execute(statement)).scalar_one_or_none()
    if created is not None:
        body = {key: values[key] for key in SNAPSHOT}
        await write_version(conn, created, 1, body, project.created_by)
    row = (await conn.execute(profile_select().where(ap.c.project_id == project_id, ap.c.is_default))).one()
    return profile_of(row)


async def version_rules(conn: AsyncConnection, profile_id: int, version: int) -> dict | None:
    """The profile ``profile_id`` as version ``version`` had it (``Profile.snapshot``); None when there is none."""
    v = tables.agent_profile_versions
    query = select(v.c.body).where(v.c.profile_id == profile_id, v.c.version == version)
    return (await conn.execute(query)).scalar_one_or_none()


async def handle_owner(conn: AsyncConnection, name: str) -> str | None:
    """The project whose agent has the handle ``name``, deleted ones included, when it is not that project's own
    default profile; None when no other agent has it. A project of that name would make @<name> ambiguous."""
    ap, p = tables.agent_profiles, tables.projects
    query = (
        select(p.c.name)
        .join_from(ap, p, p.c.id == ap.c.project_id)
        .where(ap.c.handle == name, ~(ap.c.is_default & (p.c.name == name)))
    )
    return (await conn.execute(query)).scalar_one_or_none()
