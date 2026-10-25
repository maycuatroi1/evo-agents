"""Reads and helpers of API 1.3 added while the core plugins of evo-agents moved onto the API (plan plugin-market,
step 5): a project that enables the plugin with its default label, the read rule of a caller's labels, the join
links of a project, members by login whatever its letter case, the first run each member dispatched, and who a run
is for. Private to ``core_plugin_api``, which exports each name of ``__all__``."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import signals, tables
from evo_agents.hub.access import ProjectRules
from evo_agents.hub.plugin_calls import enabled_in
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.core_plugin_api._reads import MAX_ROWS, enabled_project
from evo_agents.hub.server.core_plugin_api._types import Plugin, ProjectRef, Refused, UserRef, own_plugin
from evo_agents.hub.server.plugin_routes import not_enabled

# PROJECT_NAME: the pattern of a project's name, as the hub's routes take it in a path.


@dataclass(frozen=True)
class EnabledProject:
    """A project that enables the plugin: its id (what the plugin's own rows keep), its name and the label an object
    gets when nothing names one (the harness source's, else the top of its ladder)."""

    id: int
    name: str
    default_label: Mapping[str, Any]


@dataclass(frozen=True)
class LabelReader:
    """The read rule of one caller in one project (from ``label_reader``): ``visible`` says whether they read an
    object with a label through a sink of the project, as the hub's routes check it."""

    project: str
    max_level: str | None  # the caller's grant's, as ``project_access`` gave it
    hub_sink: str | None  # the project's hub sink, which a read that names no sink goes through
    _rules: ProjectRules = field(repr=False, compare=False)

    def visible(self, label, sink: str | None = None) -> bool:
        """Whether an object carrying ``label`` (as stored) is read through ``sink``, else the project's hub sink;
        False when the project has none."""
        through = sink or self.hub_sink
        return through is not None and self._rules.visible(label, self.max_level, through)


@dataclass(frozen=True)
class InviteRef:
    id: int
    kind: str  # join, or register for the hub
    role: str | None
    for_login: str | None  # the GitHub login alone that may accept it, when it is bound to one
    created_at: datetime
    expires_at: datetime
    accepted_by: str | None  # the login of the member who accepted it
    accepted_at: datetime | None


@dataclass(frozen=True)
class RunActorRef:
    """Who and what a run is for: its project, the profile of the agent it runs, the member who dispatched it."""

    id: int
    project: str
    kind: str | None
    profile_id: int | None
    dispatcher_id: int | None
    dispatcher: str | None  # their login


async def read_project(conn: AsyncConnection, plugin: Plugin, project: str | int) -> EnabledProject:
    """``project``, by name or by the id the plugin's own rows keep, which must enable the plugin: 404 otherwise."""
    own_plugin(plugin)
    p = tables.projects
    by = p.c.id == project if isinstance(project, int) and not isinstance(project, bool) else p.c.name == project
    row = (await conn.execute(select(p.c.id, p.c.name).where(by))).one_or_none()
    if row is None or await enabled_in(conn, plugin.id, row.id) is None:
        failed = not_enabled(plugin.id, row.name if row is not None else str(project))
        raise Refused(failed.status_code, failed.detail)
    rules = await signals.project_rules(conn, row.id)
    return EnabledProject(row.id, row.name, MappingProxyType(dict(rules.default_label)))


async def label_reader(conn: AsyncConnection, plugin: Plugin, access: ProjectRef) -> LabelReader:
    """The read rule of the caller ``access`` is for (from ``project_access``) in its project, which must enable the
    plugin: what of the plugin's rows, each with its label, they may see."""
    project_id = await enabled_project(conn, plugin, access.name)
    rules = await signals.project_rules(conn, project_id)
    hub_sink = rules.hub_sink.sink if rules.hub_sink else None
    return LabelReader(access.name, access.max_level, hub_sink, rules)


async def read_invites(
    conn: AsyncConnection,
    plugin: Plugin,
    project: str,
    *,
    kinds: Iterable[str] = (),
    since: datetime | None = None,
) -> tuple[InviteRef, ...]:
    """The invite links of ``project``, by id: of one of ``kinds``, made or accepted at or after ``since``. Never a
    token."""
    project_id = await enabled_project(conn, plugin, project)
    i, u = tables.invites, tables.users
    query = (
        select(i, u.c.login.label("acceptor"))
        .join_from(i, u, u.c.id == i.c.accepted_by, isouter=True)
        .where(i.c.project_id == project_id)
    )
    if kinds:
        query = query.where(i.c.kind.in_(list(kinds)))
    if since is not None:
        query = query.where(or_(i.c.created_at >= since, i.c.accepted_at >= since))
    rows = (await conn.execute(query.order_by(i.c.id).limit(MAX_ROWS))).all()
    return tuple(
        InviteRef(r.id, r.kind, r.role, r.for_login, r.created_at, r.expires_at, r.acceptor, r.accepted_at)
        for r in rows
    )


async def match_users(conn: AsyncConnection, plugin: Plugin, logins: Iterable[str]) -> tuple[UserRef, ...]:
    """The members of the hub whose login is one of ``logins``, letter case aside (as GitHub reads a login), by
    id."""
    own_plugin(plugin)
    wanted = sorted({login.lower() for login in logins if isinstance(login, str)})
    if not wanted:
        return ()
    u = tables.users
    query = select(u.c.id, u.c.login, u.c.created_at, u.c.last_seen_at).where(func.lower(u.c.login).in_(wanted))
    rows = (await conn.execute(query.order_by(u.c.id).limit(MAX_ROWS))).all()
    return tuple(UserRef(r.id, r.login, r.created_at, r.last_seen_at) for r in rows)


async def read_first_runs(
    conn: AsyncConnection,
    plugin: Plugin,
    project: str,
    users: Iterable[int],
    *,
    since: datetime | None = None,
    before: datetime | None = None,
) -> Mapping[int, datetime]:
    """member id -> when the first run they dispatched in ``project`` was queued, at or after ``since`` and before
    ``before``, for each member of ``users`` who dispatched one."""
    project_id = await enabled_project(conn, plugin, project)
    ids = sorted(set(users))
    if not ids:
        return MappingProxyType({})
    r = tables.runs.c
    query = select(r.dispatched_by, func.min(r.queued_at)).where(r.project_id == project_id, r.dispatched_by.in_(ids))
    if since is not None:
        query = query.where(r.queued_at >= since)
    if before is not None:
        query = query.where(r.queued_at < before)
    rows = (await conn.execute(query.group_by(r.dispatched_by))).all()
    return MappingProxyType({user_id: first for user_id, first in rows})


async def read_run(conn: AsyncConnection, plugin: Plugin, run_id: int | None) -> RunActorRef | None:
    """Who and what run ``run_id`` is for (``Context.run_id`` of a tool the agent of a run calls), or None without
    one. The plugin checks its project and profile against its own agent type (``enabled_agent_type``)."""
    own_plugin(plugin)
    if run_id is None:
        return None
    r, p, u = tables.runs, tables.projects, tables.users
    query = (
        select(r.c.id, r.c.kind, r.c.profile_id, r.c.dispatched_by, p.c.name, u.c.login)
        .join_from(r, p, p.c.id == r.c.project_id)
        .join(u, u.c.id == r.c.dispatched_by, isouter=True)
        .where(r.c.id == run_id)
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        return None
    return RunActorRef(row.id, row.name, row.kind, row.profile_id, row.dispatched_by, row.login)


__all__ = [
    "EnabledProject",
    "PROJECT_NAME",
    "InviteRef",
    "LabelReader",
    "RunActorRef",
    "label_reader",
    "match_users",
    "read_first_runs",
    "read_invites",
    "read_project",
    "read_run",
]
