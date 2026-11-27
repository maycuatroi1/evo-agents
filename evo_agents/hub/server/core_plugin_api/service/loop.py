"""Reads and helpers of API 1.3 added while the core plugins of evo-agents moved onto the API (plan plugin-market,
step 5): the audit of what a job or a member did in a project, the run behind a session, notices to a project's
admins with their details, the goals of a project measured, the test cards of its canvas claims, the plugin's own work
items of its Loop, the agent types a project enabled, the member a run acts for and a batch of signal values. Private
to ``core_plugin_api``, which exports each name of ``__all__``."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from types import MappingProxyType
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import goals as hub_goals
from evo_agents.hub import signals
from evo_agents.hub.server import audit as audit_log
from evo_agents.hub.server.core_plugin_api.models.loop import (
    ACTIVE,
    MAX_POINTS,
    PLUGIN_NOTICE,
    ClaimCardRef,
    GoalMeasure,
    ProjectAgentTypeRef,
    SignalPoint,
    SignalsPushed,
    WorkItemRef,
)
from evo_agents.hub.server.core_plugin_api.models.reads import MAX_ROWS
from evo_agents.hub.server.core_plugin_api.models.types import Plugin, Refused, Session
from evo_agents.hub.server.core_plugin_api.repository import loop as rows_of
from evo_agents.hub.server.core_plugin_api.service.ground import AUDIT_WORD, HUB_FAMILIES, MAX_AUDIT_TARGET
from evo_agents.hub.server.core_plugin_api.service.handles import (
    issue_session,
    own_plugin,
    own_session,
    refusing,
    session_principal,
)
from evo_agents.hub.server.core_plugin_api.service.reads import enabled_project
from evo_agents.hub.server.core_plugin_api.service.writes import SIGNAL_DAYS, plugin_pusher
from evo_agents.hub.server.loop import view
from evo_agents.hub.server.notifications.service.notifier import NOTIFIER
from evo_agents.hub.server.security import MACHINE, Principal
from evo_agents.hub.server.signals.models import SignalIn

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
    for user_id in await rows_of.admins(conn, project_id):
        found.append(
            await NOTIFIER.notify(
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
    return await rows_of.notice_told(conn, project_id, PLUGIN_NOTICE, plugin.id, key, str(value))


# Goals and the canvas


async def measure_goals(conn: AsyncConnection, plugin: Plugin, project: str, at: datetime) -> tuple[GoalMeasure, ...]:
    """The active goals of ``project`` that have a metric, by rank then number, each with the value of its signal at
    ``at`` and whether it reached its target, as the Loop measures them."""
    project_id = await enabled_project(conn, plugin, project)
    found = []
    for row in await rows_of.measured_goals(conn, project_id, ACTIVE):
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
    rows = await rows_of.claim_cards(conn, project_id, by_id, by_number)
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
    wanted = None
    if keys is not None:
        wanted = sorted({key for key in keys if isinstance(key, str)})
        if not wanted:
            return ()
        if len(wanted) > MAX_ROWS:
            raise ValueError(f"a read names at most {MAX_ROWS} keys")
    rows = await rows_of.work_items(conn, project_id, own, wanted, key_prefix, since)
    return tuple(WorkItemRef(r.id, r.kind, r.key.removeprefix(own), r.state, r.created_at) for r in rows)


# Agent types and runs


async def read_agent_types(
    conn: AsyncConnection, plugin: Plugin, project: str, *, profile_id: int | None = None
) -> tuple[ProjectAgentTypeRef, ...]:
    """The agent types ``project`` enabled, of every plugin it enables now, by plugin and name: of the agent profile
    ``profile_id`` alone when given."""
    project_id = await enabled_project(conn, plugin, project)
    enabled = await rows_of.enabled_plugins(conn, project_id)
    found = [
        ProjectAgentTypeRef(
            enabled[row.plugin_id],
            row.name,
            row.handle,
            row.profile_id,
            row.disabled_at is None and row.profile_deleted_at is None,
        )
        for row in await rows_of.agent_type_rows(conn, project_id, profile_id)
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
    row = await rows_of.run_dispatcher(conn, project_id, run_id)
    if row is None:
        return None
    principal = Principal(row.dispatched_by, row.login, False, None, MACHINE, "")  # pyright: ignore[reportArgumentType]
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
    pusher = await plugin_pusher(conn, plugin, project, project_id)
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
