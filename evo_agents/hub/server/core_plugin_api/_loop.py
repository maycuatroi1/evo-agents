"""Reads and helpers of API 1.3 added while the core plugins of evo-agents moved onto the API (plan plugin-market,
step 5): the audit of what a job or a member did in a project, the run behind a session, notices to a project's
admins with their details, the goals of a project measured, the test cards of its canvas claims, the plugin's own work
items of its Loop, the agent types a project enabled, the member a run acts for and a batch of signal values. Private
to ``core_plugin_api``, which exports each name of ``__all__``."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import goals as hub_goals
from evo_agents.hub import signals, tables
from evo_agents.hub.plugin_calls import enabled_query
from evo_agents.hub.server import agent_types, notifications
from evo_agents.hub.server import audit as audit_log
from evo_agents.hub.server.core_plugin_api._base import AUDIT_WORD, HUB_FAMILIES, MAX_AUDIT_TARGET
from evo_agents.hub.server.core_plugin_api._reads import MAX_ROWS, enabled_project
from evo_agents.hub.server.core_plugin_api._types import (
    Plugin,
    Refused,
    Session,
    issue_session,
    own_plugin,
    own_session,
    refusing,
    session_principal,
)
from evo_agents.hub.server.core_plugin_api._writes import SIGNAL_DAYS, _pusher
from evo_agents.hub.server.loop import view
from evo_agents.hub.server.security import MACHINE, Principal
from evo_agents.hub.server.signals import SignalIn

PLUGIN_NOTICE = "plugin_notice"  # the hub's notice kind of what a plugin tells a project's admins
ACTIVE = "active"
MAX_POINTS = 1000  # the most values one push takes

_T = tables.metadata.tables


@dataclass(frozen=True)
class GoalMeasure:
    """An active goal of a project with a metric, and the value of its signal at a time: ``reached`` is None when
    nothing measured it."""

    code: str  # G-<number>
    statement: str
    metric: Mapping[str, Any]
    metric_text: str
    value: float | None
    target: float | None
    reached: bool | None


@dataclass(frozen=True)
class ClaimCardRef:
    """A claim of a project's canvas with its test card and the signal keys that measure it."""

    id: int
    number: int
    cell: str
    text: str
    test_card: Mapping[str, Any] | None
    signal_keys: tuple[str, ...]


@dataclass(frozen=True)
class WorkItemRef:
    """A work item of the plugin's on a project's Loop: ``key`` as the plugin's rules gave it, without the prefix the
    hub stores it with (``<plugin>:``)."""

    id: int
    kind: str
    key: str
    state: str
    created_at: datetime


@dataclass(frozen=True)
class ProjectAgentTypeRef:
    """An agent type of a plugin a project enables, as that project enabled it: ``enabled`` while it is not
    disabled and its profile is not deleted."""

    plugin: str
    name: str
    handle: str
    profile_id: int
    enabled: bool


@dataclass(frozen=True)
class SignalPoint:
    """One value of a signal for ``push_signals``, as ``push_signal`` takes it."""

    key: str
    value: float | None = None
    text: str | None = None
    measured_at: datetime | None = None
    tenant: str | None = None
    external_id: str | None = None


@dataclass(frozen=True)
class SignalsPushed:
    """What ``push_signals`` did with a batch: how many values it stored, how many it found stored already, and why
    it refused each of the others."""

    accepted: int
    duplicates: int
    rejected: tuple[str, ...]


# The audit


def _action(plugin: Plugin, action: str) -> str:
    family, *words = action.split(".") if isinstance(action, str) else ("",)
    if (
        family not in (plugin.id, *plugin.kinds)
        or family in HUB_FAMILIES
        or not words
        or not all(AUDIT_WORD.fullmatch(word) for word in words)
    ):
        raise ValueError(
            f"a plugin's audit action is {plugin.id}.<word>[.<word>...] or <a kind of its channels>.<word>"
        )
    return action


async def record_audit(
    conn: AsyncConnection,
    plugin: Plugin,
    project: str,
    action: str,
    target: str,
    *,
    actor: Session | int | None = None,
) -> None:
    """Record in the hub's audit log, as a row of ``project``, that ``actor`` did ``action``
    (``<the plugin's id or one of its kinds>.<word>[.<word>...]``) to ``target``, one line of at most 1 KiB that holds
    no secret. ``actor`` is a ``Session`` (its credential is recorded with it), the id of the member a job of the plugin
    acted for, or None for the plugin itself."""
    own_plugin(plugin)
    project_id = await enabled_project(conn, plugin, project)
    _action(plugin, action)
    if not isinstance(target, str) or len(target) > MAX_AUDIT_TARGET or "\n" in target or "\r" in target:
        raise ValueError(f"an audit target is one line of at most {MAX_AUDIT_TARGET} characters")
    if isinstance(actor, Session):
        own_session(actor)
        actor_id, token_id = actor.user_id, session_principal(actor).token_id
    elif actor is None or (isinstance(actor, int) and not isinstance(actor, bool)):
        actor_id, token_id = actor, None
    else:
        raise ValueError("an audit's actor is a Session, a member's id or None")
    await audit_log.record(
        conn, actor_id=actor_id, token_id=token_id, action=action, target=target, project_id=project_id
    )


def session_run(session: Session) -> int | None:
    """The id of the run whose agent made the request of ``session``, with the scope of that run on /mcp or with the
    run's token that acts as its owner (``run_access``); None for a member's own credential."""
    return session_principal(own_session(session)).acts_for_run


# Notices to the admins


async def _admins(conn: AsyncConnection, project_id: int) -> list[int]:
    g = tables.grants.c
    found = await conn.execute(
        select(g.user_id).where(g.project_id == project_id, g.role == "admin").order_by(g.user_id)
    )
    return [row.user_id for row in found]


async def notify_admins(
    conn: AsyncConnection,
    plugin: Plugin,
    project: str,
    *,
    title: str,
    body: str | None = None,
    link: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> tuple[int, ...]:
    """A notice of the hub's kind ``plugin_notice`` for each admin of ``project``, on each of their channels that is
    on, with ``details`` (a JSON object) after the plugin's id and the project's name; the ids of the notices."""
    own_plugin(plugin)
    project_id = await enabled_project(conn, plugin, project)
    if link is not None and not (isinstance(link, str) and link.startswith("/") and not link.startswith("//")):
        raise ValueError("a notice's link is a path of the hub's web, such as /p/<project>/runs")
    more = dict(details or {})
    if {"plugin", "project"} & set(more):
        raise ValueError("a notice's details name the plugin and the project already")
    found = []
    for user_id in await _admins(conn, project_id):
        found.append(
            await notifications.notify(
                conn,
                user_id=user_id,
                kind="notice",
                notice_kind=PLUGIN_NOTICE,
                title=title,
                body=body,
                project_id=project_id,
                details={"plugin": plugin.id, "project": project, **more},
                link=link,
            )
        )
    return tuple(found)


async def admins_notified(conn: AsyncConnection, plugin: Plugin, project: str, key: str, value: str) -> bool:
    """Whether a notice of the plugin's to the admins of ``project`` (``notify_admins``) has ``details[key]`` equal
    to ``value``: what a plugin reads to tell something once."""
    own_plugin(plugin)
    project_id = await enabled_project(conn, plugin, project)
    if not isinstance(key, str) or not key or key in ("plugin", "project"):
        raise ValueError("a key of a notice's details is a name the plugin gave")
    n = tables.notifications.c
    told = await conn.scalar(
        select(n.id)
        .where(
            n.project_id == project_id,
            n.notice_kind == PLUGIN_NOTICE,
            n.details["plugin"].astext == plugin.id,
            n.details[key].astext == str(value),
        )
        .limit(1)
    )
    return told is not None


# Goals and the canvas


async def measure_goals(conn: AsyncConnection, plugin: Plugin, project: str, at: datetime) -> tuple[GoalMeasure, ...]:
    """The active goals of ``project`` that have a metric, by rank then number, each with the value of its signal at
    ``at`` and whether it reached its target, as the Loop measures them."""
    project_id = await enabled_project(conn, plugin, project)
    g, r = tables.goals, tables.goal_revisions
    query = (
        select(g.c.id, g.c.number, r.c.statement, r.c.metric)
        .join_from(g, r, (r.c.goal_id == g.c.id) & (r.c.revision == g.c.revision))
        .where(g.c.project_id == project_id, r.c.status == ACTIVE, r.c.metric.is_not(None))
        .order_by(r.c.rank, g.c.number)
    )
    found = []
    for row in (await conn.execute(query)).all():
        value = await view.measure(conn, project_id, row.metric, at, goal_id=row.id)
        target, comparator = row.metric.get("target"), row.metric.get("comparator")
        hit = None if value is None or comparator not in view.COMPARE else view.COMPARE[comparator](value, target)
        found.append(
            GoalMeasure(
                hub_goals.code(row.number),
                row.statement,
                MappingProxyType(dict(row.metric)),
                hub_goals.metric_text(row.metric),
                value,
                float(target) if isinstance(target, (int, float)) else None,
                hit,
            )
        )
    return tuple(found)


async def read_claim_cards(
    conn: AsyncConnection, plugin: Plugin, project: str, *, ids: Iterable[int] = (), numbers: Iterable[int] = ()
) -> tuple[ClaimCardRef, ...]:
    """The claims of ``project``'s canvases, archived or not, with id in ``ids`` or number (``CL-<n>``) in
    ``numbers``, with their test card and signal keys, by id; those that are not the project's are left out."""
    project_id = await enabled_project(conn, plugin, project)
    by_id = sorted({i for i in ids if isinstance(i, int) and not isinstance(i, bool)})
    by_number = sorted({n for n in numbers if isinstance(n, int) and not isinstance(n, bool)})
    if not by_id and not by_number:
        return ()
    if len(by_id) + len(by_number) > MAX_ROWS:
        raise ValueError(f"a read names at most {MAX_ROWS} claims")
    c = _T["canvas_claims"].c
    rows = await conn.execute(
        select(c.id, c.number, c.cell, c.text, c.test_card, c.signal_keys)
        .where(c.project_id == project_id, c.id.in_(by_id) | c.number.in_(by_number))
        .order_by(c.id)
    )
    return tuple(
        ClaimCardRef(
            r.id,
            r.number,
            r.cell,
            r.text,
            None if r.test_card is None else MappingProxyType(dict(r.test_card)),
            tuple(r.signal_keys or ()),
        )
        for r in rows
    )


# The Loop


async def read_work_items(
    conn: AsyncConnection,
    plugin: Plugin,
    project: str,
    *,
    keys: Iterable[str] | None = None,
    key_prefix: str | None = None,
    since: datetime | None = None,
) -> tuple[WorkItemRef, ...]:
    """The plugin's own work items on ``project``'s Loop, finished or not, by id: with a key in ``keys``, a key that
    starts with ``key_prefix``, made at ``since`` or after, each when given."""
    project_id = await enabled_project(conn, plugin, project)
    own = f"{plugin.id}:"
    i = tables.loop_items.c
    query = select(i.id, i.kind, i.key, i.state, i.created_at).where(
        i.project_id == project_id, i.key.startswith(own, autoescape=True)
    )
    if keys is not None:
        wanted = sorted({key for key in keys if isinstance(key, str)})
        if not wanted:
            return ()
        if len(wanted) > MAX_ROWS:
            raise ValueError(f"a read names at most {MAX_ROWS} keys")
        query = query.where(i.key.in_([own + key for key in wanted]))
    if key_prefix is not None:
        query = query.where(i.key.startswith(own + key_prefix, autoescape=True))
    if since is not None:
        query = query.where(i.created_at >= since)
    rows = await conn.execute(query.order_by(i.id).limit(MAX_ROWS))
    return tuple(WorkItemRef(r.id, r.kind, r.key.removeprefix(own), r.state, r.created_at) for r in rows)


# Agent types and runs


async def read_agent_types(
    conn: AsyncConnection, plugin: Plugin, project: str, *, profile_id: int | None = None
) -> tuple[ProjectAgentTypeRef, ...]:
    """The agent types ``project`` enabled, of every plugin it enables now, by plugin and name: of the agent profile
    ``profile_id`` alone when given."""
    project_id = await enabled_project(conn, plugin, project)
    enabled = {
        row.id: row.name
        for row in (await conn.execute(enabled_query().where(tables.projects.c.id == project_id))).all()
    }
    query = agent_types._rows(project_id)
    if profile_id is not None:
        query = query.where(tables.project_agent_types.c.profile_id == profile_id)
    found = [
        ProjectAgentTypeRef(
            enabled[row.plugin_id],
            row.name,
            row.handle,
            row.profile_id,
            row.disabled_at is None and row.profile_deleted_at is None,
        )
        for row in (await conn.execute(query)).all()
        if row.plugin_id in enabled
    ]
    return tuple(sorted(found, key=lambda t: (t.plugin, t.name)))


async def run_member(conn: AsyncConnection, plugin: Plugin, project: str, run_id: int | None) -> Session | None:
    """The member who dispatched run ``run_id`` of ``project``, of any kind, as a ``Session`` (``machine``, no token
    of its own) that ``project_access`` checks as the hub checks that member: what the agent of the run acts as. None
    for no run, a run of another project or one nobody dispatched. A hub admin among them is no admin here."""
    project_id = await enabled_project(conn, plugin, project)
    if run_id is None:
        return None
    r, u = tables.runs, tables.users
    row = (
        await conn.execute(
            select(r.c.dispatched_by, u.c.login)
            .join_from(r, u, u.c.id == r.c.dispatched_by)
            .where(r.c.id == run_id, r.c.project_id == project_id)
        )
    ).one_or_none()
    if row is None:
        return None
    principal = Principal(row.dispatched_by, row.login, False, None, MACHINE, "")
    return issue_session(row.dispatched_by, row.login, 0, False, principal=principal)


# Signals


async def push_signals(
    conn: AsyncConnection, plugin: Plugin, project: str, points: Sequence[SignalPoint], *, days: int = SIGNAL_DAYS
) -> SignalsPushed:
    """Push ``points`` at once, as ``push_signal`` pushes one: values of keys an admin of ``project`` declared, as the
    project's ingest key named after the plugin, which it makes on first use; one sent again with the same
    ``external_id`` is stored once. 409 when the project has no admin to own the key, or one revoked it."""
    project_id = await enabled_project(conn, plugin, project)
    if len(points) > MAX_POINTS:
        raise ValueError(f"a push takes at most {MAX_POINTS} values")
    with refusing():
        items = [
            SignalIn(
                key=p.key,
                value=p.value,
                text=p.text,
                source=plugin.id,
                measured_at=p.measured_at,
                tenant=p.tenant,
                external_id=p.external_id,
            )  # fmt: skip
            for p in points
        ]
    pusher = await _pusher(conn, plugin, project, project_id)
    with refusing():
        try:
            done = await signals.push(conn, pusher, items, days)
        except signals.SignalError as exc:
            raise Refused(exc.status, exc.message) from None
    return SignalsPushed(done.accepted, done.duplicates, tuple(str(item.get("reason")) for item in done.rejected))


__all__ = [
    "ClaimCardRef",
    "GoalMeasure",
    "ProjectAgentTypeRef",
    "SignalPoint",
    "SignalsPushed",
    "WorkItemRef",
    "admins_notified",
    "measure_goals",
    "notify_admins",
    "push_signals",
    "read_agent_types",
    "read_claim_cards",
    "read_work_items",
    "record_audit",
    "run_member",
    "session_run",
]
