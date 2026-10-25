"""The events and notifications the hub delivers to plugins, at least once (``docs/plugins.md``, section Extension
points).

``emit`` stores, in the caller's transaction, one delivery of an event for each plugin enabled in the event's project
whose installed version takes it and whose grant there lets it read it: a run's state (``run.state``, with
``runs:read``), a message of a group chat (``chat.message``, with ``chats:read``), a step of a plan (``plan.step``, with
``plans:read``), the end of a task run (``task.result``, with ``runs:dispatch``), which ``emit_task_result`` stores for
the plugin of the run's agent type alone (``evo_agents.hub.task_runs``), a rev of a claim of a Lean Canvas and an
approved baseline of one (``canvas.claim``, ``canvas.baseline``, with ``canvas:read``; the core plugin canvas emits
them), a batch of signals (``signal.posted``, with ``signals:read``; ``evo_agents.hub.signals``), an approved version of
an SRS document (``srs.version.approved``, with ``srs:read``; the core plugin srs emits it), and a task's status set or
changed (``tasks.status``, with ``tasks:read``; ``evo_agents.hub.task_status``), and the end of a question a core plugin
asked an agent type of its own (``agent.answer``, which ``emit`` stores for that plugin alone;
``evo_agents.hub.question_runs``). A core plugin takes the end of its own task runs and questions without a grant, as
it hands them without one (``task_dispatch.hand_own_task``, ``core_plugin_api.ask_agent_type``), and the events of its
own (``OWN``) likewise: the image trusts it. An events contribution takes the kinds its ``on`` names, or every kind
when it names none. The same call writes an event of the last five kinds into the hub's own table hub_events too,
which the Loop reads by cursor (``evo_agents.hub.hub_events``), whether a plugin takes it or not. The table
plugin_deliveries is the outbox:
nothing goes out while the transaction is open, and nothing is lost when the hub stops after the commit.
``emit_notification`` does the same for a notification of a project, to each isolated plugin's channel there, with
``notifications:send``: a core plugin's channel is a kind of members' channel instead, which
``evo_agents.hub.server.notifications`` sends as it
sends Telegram's.

A payload carries ids and states, never a text the project's labels may keep from a plugin: a run's id, kind and
states, a message's ids, a step's id and statuses, a task's ids, state, branch and pull requests, a notification's
kind, member and link with the words a restricted project's Telegram message carries (``telegram.what_waits``). A
plugin that needs more asks the hub with its token, under its grant. ``agent.answer`` is the one exception, made on
purpose: it carries the answer's text, to the core plugin that asked alone, which the image trusts, once the hub
checked that the project is not kept from the place the answer goes (``core_plugin_api.restricted``); delivering it
checks that again (``_answer_payload``), and a project kept from it by then gets the state refused and no text.

``deliver`` is the job hub.deliver_plugin_events, every minute. It takes the pending deliveries that are due, each in
a transaction of its own with ``FOR UPDATE SKIP LOCKED``, checks again that the plugin is installed, turned on,
enabled in the project and granted what the kind needs, and fails the delivery at once when it is not; then it calls
the plugin's contribution (``plugin_calls.call``): method ``event`` or ``channel`` of plugin/1, with the delivery's id,
which stays the same on every try. A delivery is delivered once the plugin answered, so a hub that stops between the
answer and the commit delivers it again: at least once. A failed try waits BACKOFF_SECONDS, doubling up to
BACKOFF_MAX_SECONDS, and the MAX_ATTEMPTS-th fails it; a channel that answers ``delivered: false`` fails it at once.
Deliveries that ended are kept KEEP_DAYS days.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import access, hub_events, tables, telegram
from evo_agents.hub.plugin_calls import (
    CoreContext,
    PluginCallError,
    PluginHostClient,
    PluginHostUnavailable,
    asked_for,
    call,
    enabled_plugins,
    enablements,
)
from evo_agents.hub.question_runs import ANSWER_EVENT as ANSWER
from evo_agents.hub.telegram import what_waits
from evo_agents.plugins.protocol import EVENT_KINDS

log = logging.getLogger(__name__)

NOTIFICATION = "notification"
KINDS = (*EVENT_KINDS, NOTIFICATION)  # plugin_deliveries.kind (0038)
STATES = ("pending", "delivered", "failed")  # plugin_deliveries.state (0038)
PERMISSION = {
    "run.state": "runs:read",
    "chat.message": "chats:read",
    "plan.step": "plans:read",
    "task.result": "runs:dispatch",
    "agent.answer": "runs:dispatch",  # only a core plugin asks, and takes it ungranted
    hub_events.CLAIM: "canvas:read",
    hub_events.BASELINE: "canvas:read",
    hub_events.SIGNALS: "signals:read",
    hub_events.SRS_APPROVED: "srs:read",
    hub_events.TASK_STATUS: "tasks:read",
    NOTIFICATION: "notifications:send",
}
# the core plugin each of these kinds belongs to, which takes it ungranted: srs keeps the tasks of an approved version
# in step from its own event (``evo_agents.hub.server.srs.auto_tasks``)
OWN = {hub_events.SRS_APPROVED: "srs", hub_events.TASK_STATUS: "tasks"}
MAX_ATTEMPTS = 8
BACKOFF_SECONDS = 60
BACKOFF_MAX_SECONDS = 3600
BATCH = 500
KEEP_DAYS = 14
TIMEOUT = 30.0
MAX_ERROR_CHARS = 2000


def subscriptions(manifest: dict, tier: str, kind: str) -> list[str]:
    """The names of the contributions of a manifest (as stored) that take ``kind``: its events contributions, or, for
    a notification, an isolated plugin's channels."""
    contributes = manifest.get("contributes") or {}
    if kind == NOTIFICATION:
        return [item["name"] for item in contributes.get("channels") or []] if tier == "isolated" else []
    return [item["name"] for item in contributes.get("events") or [] if kind in item.get("on", EVENT_KINDS)]


def _granted(manifest: dict, permissions, kind: str, tier: str | None) -> bool:
    """Whether a plugin of ``tier`` whose project grants it ``permissions`` reads events of ``kind``: a core plugin
    reads the end of its own task runs and questions (``task.result`` and ``agent.answer`` go to the plugin of the
    run's agent type alone) and the events of ``OWN`` that are its own ungranted."""
    if tier == "core" and (kind in ("task.result", ANSWER) or OWN.get(kind) == manifest.get("id")):
        return True
    needed = PERMISSION[kind]
    return needed in (permissions or []) and needed in asked_for(manifest)


async def _subscribers(conn: AsyncConnection, kind: str, project_id: int) -> list[tuple[int, str]]:
    found = []
    for row in await enabled_plugins(conn, project_id):
        if _granted(row.manifest, row.permissions, kind, row.tier):
            found += [(row.id, name) for name in subscriptions(row.manifest, row.tier, kind)]
    return found


async def emit(
    conn: AsyncConnection, kind: str, project_id: int | None, payload: dict, *, plugin_id: int | None = None
) -> int:
    """Store a delivery of the event ``kind`` of project ``project_id`` for each plugin that takes it (plugin
    ``plugin_id`` alone when given), in the caller's transaction; how many. An event of ``hub_events.KINDS`` is also
    written to hub_events, in the same transaction."""
    if project_id is None:
        return 0
    if kind in hub_events.KINDS:
        await hub_events.record(conn, kind, project_id, payload)
    found = [item for item in await _subscribers(conn, kind, project_id) if plugin_id in (None, item[0])]
    if found:
        values = [
            {"plugin_id": taker, "project_id": project_id, "kind": kind, "contribution": name, "payload": payload}
            for taker, name in found
        ]
        await conn.execute(insert(tables.plugin_deliveries), values)
    return len(found)


async def emit_run_state(conn: AsyncConnection, run_id: int, row, old: str, new: str, actor: str) -> int:
    """Store the ``run.state`` event of run ``run_id``, which ``actor`` moved from ``old`` to ``new``: ``row`` is what
    ``run_state.move_run`` got back from its UPDATE (project_id, kind, plan_id and step_key)."""
    change = {"run_id": run_id, "run_kind": row.kind, "from": old, "to": new, "actor": actor}
    return await emit(conn, "run.state", row.project_id, {**change, "plan_id": row.plan_id, "step": row.step_key})


async def emit_task_result(conn: AsyncConnection, project_id: int, plugin_id: int, payload: dict) -> int:
    """Store the ``task.result`` event of a task run that ended, ``payload`` (``task_runs``), for plugin ``plugin_id``,
    the plugin of the run's agent type, alone."""
    return await emit(conn, "task.result", project_id, payload, plugin_id=plugin_id)


async def emit_notification(conn: AsyncConnection, notification_id: int, project_id: int | None) -> int:
    """Store a delivery of notification ``notification_id`` for each isolated plugin's channel in its project."""
    if project_id is None:
        return 0
    found = await _subscribers(conn, NOTIFICATION, project_id)
    if not found:
        return 0
    n, users, projects = tables.notifications, tables.users, tables.projects
    row = (
        await conn.execute(
            select(n, users.c.login, projects.c.name.label("project"))
            .join_from(n, users, users.c.id == n.c.user_id)
            .join(projects, projects.c.id == n.c.project_id)
            .where(n.c.id == notification_id)
        )
    ).one()
    payload = {
        "id": notification_id,
        "kind": row.kind,
        "notice_kind": row.notice_kind,
        "login": row.login,
        "project": row.project,
        "text": what_waits(row.kind, row.notice_kind),
        "link": row.link,
        "run_id": row.run_id,
        "decision_id": row.decision_id,
        "proposal_id": row.proposal_id,
    }
    values = [
        {
            "plugin_id": plugin_id,
            "project_id": project_id,
            "kind": NOTIFICATION,
            "contribution": name,
            "payload": payload,
        }
        for plugin_id, name in found
    ]
    await conn.execute(insert(tables.plugin_deliveries), values)
    return len(found)


# Delivering


def backoff(attempts: int) -> timedelta:
    """How long a delivery waits after its ``attempts``-th failed try."""
    return timedelta(seconds=min(BACKOFF_SECONDS * 2 ** max(0, attempts - 1), BACKOFF_MAX_SECONDS))


def _due(batch: int):
    d = tables.plugin_deliveries
    return (
        select(d.c.id)
        .where(d.c.state == "pending", d.c.next_at <= func.now())
        .order_by(d.c.next_at, d.c.id)
        .limit(batch)
    )


def _lock_due(delivery_id: int):
    """Delivery ``delivery_id`` while it is pending and due, locked, with its plugin's installed version and what its
    project grants it now; no row when another pass holds it."""
    d, p, v, pp, projects = (
        tables.plugin_deliveries,
        tables.plugins,
        tables.plugin_versions,
        enablements(),  # project_plugins, and the features of the hub that are on (canvas, srs, tasks)
        tables.projects,
    )
    return (
        select(
            d.c.id,
            d.c.project_id,
            d.c.kind,
            d.c.contribution,
            d.c.payload,
            d.c.attempts,
            p.c.name,
            p.c.installed_version,
            p.c.disabled_reason,
            v.c.manifest,
            v.c.tier,
            pp.c.plugin_id.label("enabled"),
            pp.c.permissions,
            projects.c.name.label("project"),
        )
        .select_from(
            d.join(p, p.c.id == d.c.plugin_id)
            .join(projects, projects.c.id == d.c.project_id)
            .outerjoin(v, (v.c.plugin_id == p.c.id) & (v.c.version == p.c.installed_version))
            .outerjoin(pp, (pp.c.plugin_id == d.c.plugin_id) & (pp.c.project_id == d.c.project_id))
        )
        .where(d.c.id == delivery_id, d.c.state == "pending", d.c.next_at <= func.now())
        .with_for_update(of=d, skip_locked=True)
    )


def _set(delivery_id: int, **values):
    d = tables.plugin_deliveries
    return update(d).where(d.c.id == delivery_id).values(**values)


def _refusal(row) -> str | None:
    """Why the plugin of delivery ``row`` may not get it now, or None."""
    if row.installed_version is None:
        return f"plugin {row.name} is not installed any more"
    if row.disabled_reason:
        return f"plugin {row.name} is turned off: {row.disabled_reason}"
    if row.enabled is None:
        return f"project {row.project} does not enable plugin {row.name} any more"
    if not _granted(row.manifest, row.permissions, row.kind, row.tier):
        return f"plugin {row.name} is not granted {PERMISSION[row.kind]} in project {row.project}"
    if row.contribution not in subscriptions(row.manifest, row.tier, row.kind):
        return f"plugin {row.name} {row.installed_version} no longer takes {row.kind} in {row.contribution}"
    return None


def _params(row, payload: dict) -> tuple[str, dict]:
    if row.kind == NOTIFICATION:
        params = {"channel": row.contribution, "project": row.project, "notification": payload}
        return "channels", {**params, "delivery": row.id}
    params = {"event": row.contribution, "project": row.project, "payload": payload, "kind": row.kind}
    return "events", {**params, "delivery": row.id}


async def _answer_payload(conn: AsyncConnection, row) -> dict:
    """The payload of delivery ``row`` as it goes now: an ``agent.answer`` with a text, the state refused and no text
    when the project is kept now from the clearance its question named (``telegram.restricted_for``, failing closed:
    a question gone, or a clearance it cannot read, is kept)."""
    payload = row.payload
    if row.kind != ANSWER or not isinstance(payload, dict) or payload.get("text") is None:
        return payload
    q, p, ps = tables.agent_questions, tables.projects, tables.project_sinks
    hub_sink = (ps.c.project_id == p.c.id) & (ps.c.kind == access.HUB_KIND)
    query = (
        select(q.c.cleared, p.c.levels, p.c.locations, ps.c.clearance)
        .select_from(q.join(p, p.c.id == q.c.project_id).outerjoin(ps, hub_sink))
        .where(q.c.id == payload.get("question_id"), q.c.project_id == row.project_id)
    )
    found = (await conn.execute(query)).one_or_none()
    cleared = found.cleared if found is not None and isinstance(found.cleared, dict) else None
    if cleared is not None and isinstance(cleared.get("level"), str):
        level, location, below = cleared["level"], cleared.get("location"), cleared.get("below") is True
        if not telegram.restricted_for(found.clearance, found.levels, found.locations, level, location, below=below):
            return payload
    return {**payload, "state": "refused", "text": None}


async def _deliver_one(conn: AsyncConnection, delivery_id: int, engine, config, host) -> str | None:
    row = (await conn.execute(_lock_due(delivery_id))).one_or_none()
    if row is None:
        return None
    refused = _refusal(row)
    if refused is not None:
        await conn.execute(_set(delivery_id, state="failed", attempts=row.attempts + 1, last_error=refused))
        log.info("plugin delivery refused", extra={"delivery_id": delivery_id, "why": refused})
        return "failed"
    point, params = _params(row, await _answer_payload(conn, row))
    item = next(item for item in row.manifest["contributes"][point] if item["name"] == row.contribution)
    context = CoreContext(row.name, engine, config, row.project, row.project_id)
    try:
        result = await call(host, row.name, row.tier, point, item, params, context, TIMEOUT)
    except (PluginCallError, PluginHostUnavailable) as exc:
        error = " ".join(f"{type(exc).__name__}: {exc}".split())[:MAX_ERROR_CHARS]
        if row.attempts + 1 >= MAX_ATTEMPTS:
            await conn.execute(_set(delivery_id, state="failed", attempts=row.attempts + 1, last_error=error))
            log.warning("plugin delivery failed", extra={"delivery_id": delivery_id, "contribution": row.name})
            return "failed"
        wait = backoff(row.attempts + 1)
        await conn.execute(_set(delivery_id, attempts=row.attempts + 1, next_at=func.now() + wait, last_error=error))
        return "retried"
    if point == "channels" and not result["delivered"]:
        said = f"plugin {row.name}'s channel {row.contribution} did not deliver it"
        await conn.execute(_set(delivery_id, state="failed", attempts=row.attempts + 1, last_error=said))
        return "failed"
    await conn.execute(_set(delivery_id, state="delivered", attempts=row.attempts + 1, delivered_at=func.now()))
    return "delivered"


async def deliver(engine: AsyncEngine, *, config=None, host=None, batch: int = BATCH) -> dict:
    """One pass of the job hub.deliver_plugin_events: each pending delivery that is due, tried once in a transaction
    of its own; how many were delivered, are to be tried again, and failed. Without ``host`` it opens a client to the
    plugin host of ``config``."""
    report = {"delivered": 0, "retried": 0, "failed": 0}
    own = host is None and config is not None
    if own:
        host = PluginHostClient(config.plugin_host_url, config.plugin_host_token)
    try:
        async with engine.begin() as conn:
            due = (await conn.execute(_due(batch))).scalars().all()
        for delivery_id in due:
            async with engine.begin() as conn:
                outcome = await _deliver_one(conn, delivery_id, engine, config, host)
            if outcome is not None:
                report[outcome] += 1
        async with engine.begin() as conn:
            d = tables.plugin_deliveries
            ended = (d.c.state != "pending") & (d.c.created_at < func.now() - timedelta(days=KEEP_DAYS))
            report["pruned"] = (await conn.execute(delete(d).where(ended))).rowcount
    finally:
        if own:
            await host.aclose()
    if report["retried"] or report["failed"]:
        log.warning("plugin deliveries", extra=report)
    elif report["delivered"]:
        log.info("plugin deliveries", extra=report)
    return report
